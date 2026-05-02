"""OpenRouter client (aiohttp) + retry + fallback + семафор + prompt caching. См. LLM.md."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import aiohttp

from src import config, db, log
from src.models import Job

if TYPE_CHECKING:
    from aiohttp import ClientSession


OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
HTTP_REFERER = config.OPENROUTER_HTTP_REFERER  # ARCHITECTURE.md §4 — настраивается env
X_TITLE = config.OPENROUTER_X_TITLE


# Глобальный семафор concurrency (LLM.md §1).
# Берётся per-call внутри `_call`, а НЕ вокруг `_with_fallback` — иначе один слот
# залипает на primary+fallback (до 240 с при двойном таймауте).
llm_sem = asyncio.Semaphore(config.LLM_CONCURRENCY)


# Глобальный aiohttp.ClientSession; задаётся при старте приложения (ARCHITECTURE.md §5.2).
_session: ClientSession | None = None


# --------------------------------------------------------------------------- #
# Circuit breaker (LLM.md §2)
# Считаем подряд-failure'ы внутри окна WINDOW_S; после THRESHOLD trip'аем на TRIP_S.
# Любая успешная отдача 200 — сбрасывает счётчик (half-open сразу закрывается).
# --------------------------------------------------------------------------- #
_cb_failures: list[float] = []  # timestamps подряд-failure'ов
_cb_open_until: float = 0.0


def _cb_should_short_circuit() -> bool:
    return config.LLM_CIRCUIT_THRESHOLD > 0 and time.monotonic() < _cb_open_until


def _cb_record_success() -> None:
    global _cb_open_until
    _cb_failures.clear()
    _cb_open_until = 0.0


def _cb_record_failure() -> None:
    global _cb_open_until
    if config.LLM_CIRCUIT_THRESHOLD <= 0:
        return
    now = time.monotonic()
    cutoff = now - config.LLM_CIRCUIT_WINDOW_S
    # Дроп старых записей (вне окна — не считаются «подряд» за период)
    while _cb_failures and _cb_failures[0] < cutoff:
        _cb_failures.pop(0)
    _cb_failures.append(now)
    if len(_cb_failures) >= config.LLM_CIRCUIT_THRESHOLD:
        _cb_open_until = now + config.LLM_CIRCUIT_TRIP_S
        _cb_failures.clear()


def set_session(session: ClientSession) -> None:
    global _session
    _session = session


# --------------------------------------------------------------------------- #
# Payload builder (LLM.md §2)
# --------------------------------------------------------------------------- #
def _year_or_default(d: Any) -> str:
    return str(d.year) if d is not None and hasattr(d, "year") else "нет"


# (label, getter) — порядок задаёт порядок полей в payload (важен для prompt caching!).
# Pre-screen видит 13 полей, analysis — 15 (LLM.md §2).
_BASE_FIELDS: list[tuple[str, Callable[[Job], Any]]] = [
    ("название вакансии", lambda j: j.job_title or ""),
    ("описание вакансии", lambda j: j.job_description or ""),
    ("вопросы клиента", lambda j: j.questions or "нет"),
    ("тип бюджета", lambda j: j.budget_type or ""),
    ("сумма бюджета", lambda j: j.budget or ""),
    ("тип занятости", lambda j: j.job_type or ""),
    ("категория клиента (rank)", lambda j: j.client_rank or ""),
    ("страна клиента", lambda j: j.client_country or ""),
    ("рейтинг клиента", lambda j: j.client_rating if j.client_rating is not None else "нет"),
    ("всего потрачено клиентом, $", lambda j: j.client_total_spent or 0),
    ("количество наймов", lambda j: j.client_total_hires or 0),
    (
        "средняя ставка которую платит клиент, $/час",
        lambda j: j.client_avg_rate if j.client_avg_rate is not None else "нет",
    ),
    ("год регистрации клиента на Upwork", lambda j: _year_or_default(j.client_registered_at)),
]

_FULL_EXTRA_FIELDS: list[tuple[str, Callable[[Job], Any]]] = [
    ("дата публикации вакансии", lambda j: j.published_date or "нет"),
    ("отзывы о клиенте от других фрилансеров", lambda j: j.client_reviews or "нет"),
]


def _build_payload(job: Job, *, full: bool) -> str:
    """Стабильный template — в system, уникальные данные — здесь.

    full=False → 13 базовых полей для pre-screen.
    full=True  → +дата публикации +client_reviews для analysis (15 полей).
    """
    fields = _BASE_FIELDS + _FULL_EXTRA_FIELDS if full else _BASE_FIELDS
    return "\n\n".join(f"[{label}]\n{getter(job)}" for label, getter in fields)


def build_prescreen_payload(job: Job) -> str:
    return _build_payload(job, full=False)


def build_analysis_payload(job: Job) -> str:
    """15 полей = pre-screen 13 + дата публикации + полные client_reviews."""
    return _build_payload(job, full=True)


# --------------------------------------------------------------------------- #
# Сборка messages — учитывает Anthropic prompt caching (LLM.md §3)
# --------------------------------------------------------------------------- #
def _build_messages(template: str, job_payload: str, model: str) -> list[dict]:
    """Стабильный template — в system, уникальное — в user."""
    if model.startswith("anthropic/"):
        return [
            {
                "role": "system",
                "content": [
                    {
                        "type": "text",
                        "text": template,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
            },
            {"role": "user", "content": job_payload},
        ]
    return [
        {"role": "system", "content": template},
        {"role": "user", "content": job_payload},
    ]


# --------------------------------------------------------------------------- #
# Низкоуровневый _call (LLM.md §2)
# --------------------------------------------------------------------------- #
async def _call(
    session: ClientSession,
    api_key: str,
    model: str,
    template: str,
    job_payload: str,
    timeout_s: int,
) -> str | None:
    if _cb_should_short_circuit():
        await log.emit(
            "openrouter_circuit_open",
            level=logging.WARNING,
            model=model,
        )
        return None

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    # Опциональные заголовки (LLM.md §1) — пустые не отправляем
    if HTTP_REFERER:
        headers["HTTP-Referer"] = HTTP_REFERER
    if X_TITLE:
        headers["X-Title"] = X_TITLE
    body = {
        "model": model,
        "messages": _build_messages(template, job_payload, model),
        "temperature": 0.3,
    }
    try:
        async with llm_sem, session.post(
            OPENROUTER_URL,
            headers=headers,
            json=body,
            timeout=aiohttp.ClientTimeout(total=timeout_s),
        ) as resp:
            if resp.status >= 400:
                err_text = ""
                with contextlib.suppress(Exception):
                    err_text = (await resp.text())[:200]
                await log.emit(
                    "openrouter_http_error",
                    level=logging.WARNING,
                    status=resp.status,
                    model=model,
                    body=err_text,
                )
                _cb_record_failure()
                return None
            data = await resp.json()
            usage = data.get("usage", {}) or {}
            await log.emit(
                "llm_call",
                model=model,
                tokens_in=usage.get("prompt_tokens"),
                tokens_cached=usage.get("prompt_cache_hit_tokens", 0),
                tokens_out=usage.get("completion_tokens"),
            )
            _cb_record_success()
            return data["choices"][0]["message"]["content"]
    except (TimeoutError, aiohttp.ClientError) as e:
        await log.emit(
            "openrouter_exception",
            level=logging.WARNING,
            model=model,
            err=str(e),
        )
        _cb_record_failure()
        return None


async def _with_fallback(
    session: ClientSession,
    api_key: str,
    primary: str,
    fallback: str,
    template: str,
    job_payload: str,
    timeout_s: int,
) -> str | None:
    """Семафор `llm_sem` теперь живёт внутри `_call` — primary и fallback берут
    отдельные слоты, поэтому слот не залипает на 2*timeout_s при деградации."""
    result = await _call(session, api_key, primary, template, job_payload, timeout_s)
    if result:
        return result
    await log.emit(
        "llm_fallback",
        level=logging.WARNING,
        from_model=primary,
        to_model=fallback,
    )
    return await _call(session, api_key, fallback, template, job_payload, timeout_s)


# --------------------------------------------------------------------------- #
# Высокоуровневые pre_screen / analyze (LLM.md §2)
# --------------------------------------------------------------------------- #
def _resolve_session(session: ClientSession | None) -> ClientSession:
    """Возвращает session аргумент или глобальный _session, инициализированный в main."""
    sess = session if session is not None else _session
    if sess is None:
        raise RuntimeError("llm.set_session(http_session) must be called before LLM calls")
    return sess


async def pre_screen(session: ClientSession | None, job: Job) -> int | None:
    """Возвращает int (0..10) или None если LLM упал / ответ непарсимый."""
    from src.pipeline import parse_pre_rating

    sess = _resolve_session(session)
    s = await db.get_settings_cached()
    key = await db.get_openrouter_key()
    template = await db.get_prompt_cached("pre_screen")
    payload = build_prescreen_payload(job)
    text = await _with_fallback(
        sess,
        key,
        s.prescreen_model,
        s.prescreen_fallback_model,
        template,
        payload,
        timeout_s=60,
    )
    if text is None:
        return None
    return parse_pre_rating(text)


async def analyze(session: ClientSession | None, job: Job) -> str | None:
    sess = _resolve_session(session)
    s = await db.get_settings_cached()
    key = await db.get_openrouter_key()
    template = await db.get_prompt_cached("analysis")
    payload = build_analysis_payload(job)
    return await _with_fallback(
        sess,
        key,
        s.analysis_model,
        s.analysis_fallback_model,
        template,
        payload,
        timeout_s=120,
    )


async def validate_model(session: ClientSession, api_key: str, model: str) -> tuple[bool, str]:
    """Канарейка: prompt='ping', max_tokens=5 (LLM.md §2)."""
    body = {
        "model": model,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 5,
    }
    headers = {"Authorization": f"Bearer {api_key}"}
    try:
        async with session.post(
            OPENROUTER_URL,
            headers=headers,
            json=body,
            timeout=aiohttp.ClientTimeout(total=10),
        ) as r:
            if r.status == 200:
                return True, "ok"
            if r.status == 401:
                return False, "API ключ недействителен"
            if r.status == 404:
                return False, "Модель не найдена на OpenRouter"
            return False, f"HTTP {r.status}"
    except (TimeoutError, aiohttp.ClientError) as e:
        return False, f"network error: {e}"
