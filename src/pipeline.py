"""process_incoming_job() + parse_rating + normalize + hard_filter. См. PIPELINE.md."""

from __future__ import annotations

import asyncio
import collections
import json
import logging
import re
import time
from collections.abc import Callable
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

import msgspec

from src import config, db, llm, log, notifier
from src.models import BotSettings, Job, WebhookBody

PIPELINE_BACKGROUND_TIMEOUT = 120

# Лимит одновременных pipeline-задач внутри одного batch'а — защита от
# webhook'а с тысячами проектов (OOM). См. ARCHITECTURE.md §5.3.
BATCH_FANOUT_LIMIT = config.BATCH_FANOUT_LIMIT


class PipelineResult(StrEnum):
    DELIVERED = "delivered"
    QUEUED_PAUSED = "queued_paused"
    FILTERED_HARD = "filtered_hard"
    FILTERED_PRE = "filtered_pre"
    FILTERED_ANALYSIS = "filtered_analysis"
    SKIPPED_DUPLICATE = "skipped_duplicate"
    LLM_FAILED = "llm_failed"


TERMINAL_STATES: frozenset[str] = frozenset({"filtered", "delivered", "analyzed", "failed"})


# --------------------------------------------------------------------------- #
# Парсеры (PIPELINE.md §6)
# --------------------------------------------------------------------------- #
RATING_RE = re.compile(r"РЕЙТИНГ:\s*([0-9]+(?:[.,][0-9]+)?)", re.IGNORECASE)
HOURLY_BUDGET_RE = re.compile(r"\$?(\d+(?:\.\d+)?)(?:\s*-\s*\$?(\d+(?:\.\d+)?))?")
FIXED_BUDGET_RE = re.compile(r"\$?(\d+(?:[\.,]\d+)?)")


def parse_rating_float(text: str) -> float:
    """Парсит РЕЙТИНГ: N из ai_analysis как float [0.0..10.0].

    Используется для сравнения с порогами analysis_threshold /
    loud_notification_threshold — иначе округление до int делает порог
    нестрогим (4.8 → 5 → проходит порог=5 несмотря на «реальные» < 5).
    """
    if not text:
        return 0.0
    m = RATING_RE.search(text)
    if not m:
        return 0.0
    val = float(m.group(1).replace(",", "."))
    return max(0.0, min(10.0, val))


def parse_rating(text: str) -> int:
    """Округлённый int [0..10] — для записи в smallint колонку upwork_jobs.rating."""
    return round(parse_rating_float(text))


def parse_pre_rating(text: str) -> int | None:
    """Pre-screen: None если ответ непарсимый."""
    if not text:
        return None
    m = re.search(r"-?\d+", text)
    if not m:
        return None
    val = int(m.group(0))
    return val if 0 <= val <= 10 else None


def _try_parse_json(text: str) -> dict[str, Any] | None:
    """Извлекает первый {...}-объект из ответа (в т.ч. внутри ```-обёрток) и
    нормализует поля. None если валидного JSON с полем rating нет."""
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start : end + 1])
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict) or "rating" not in data:
        return None
    try:
        rating = max(0.0, min(10.0, float(data["rating"])))
    except (TypeError, ValueError):
        return None
    verdict = data.get("verdict")
    if verdict not in ("брать", "скип"):
        verdict = "брать" if rating >= 7 else "скип"
    risks_raw = data.get("risks") or []
    if isinstance(risks_raw, str):
        risks = [risks_raw]
    elif isinstance(risks_raw, list):
        risks = [str(r) for r in risks_raw if r]
    else:
        risks = []
    return {
        "rating": rating,
        "summary": str(data.get("summary") or ""),
        "stack_match": str(data.get("stack_match") or ""),
        "risks": risks,
        "verdict": verdict,
        "reason": str(data.get("reason") or ""),
        "legacy_text": None,
    }


def parse_analysis(text: str | None) -> dict[str, Any] | None:
    """Парсит ответ дорогой нейронки.

    Сначала пробует JSON (новый формат). При неудаче — legacy-режим: вытаскивает
    `РЕЙТИНГ: N` из прозы (на случай, если модель проигнорировала JSON-инструкцию).
    Возвращает нормализованный dict с ключами rating/summary/stack_match/risks/
    verdict/reason/legacy_text, либо None если рейтинг извлечь не удалось.

    `legacy_text` != None означает прозаический ответ — его и показываем как есть;
    при JSON он None, и карточку собирает notifier.render_analysis_card().
    """
    if not text:
        return None
    parsed = _try_parse_json(text)
    if parsed is not None:
        return parsed
    if RATING_RE.search(text):
        rating = parse_rating_float(text)
        return {
            "rating": rating,
            "summary": "",
            "stack_match": "",
            "risks": [],
            "verdict": "брать" if rating >= 7 else "скип",
            "reason": "",
            "legacy_text": text,
        }
    return None


def parse_hourly_budget_max(s: str | None) -> float | None:
    """Из '$5-$15' возвращает 15.0. Из '$30' возвращает 30.0."""
    if not s:
        return None
    m = HOURLY_BUDGET_RE.search(s)
    if not m:
        return None
    return float(m.group(2) or m.group(1))


def parse_fixed_budget(s: str | None) -> float | None:
    """Из '$500' или 'Fixed-price 250' возвращает число."""
    if not s:
        return None
    m = FIXED_BUDGET_RE.search(s.replace(",", ""))
    return float(m.group(1)) if m else None


# --------------------------------------------------------------------------- #
# hard_filter — rule-based отсев ДО LLM (PIPELINE.md §5)
# --------------------------------------------------------------------------- #
# Каждое правило — функция (job, settings) → причина-отказа или None.
# Порядок в списке = порядок проверки. Добавление нового правила = одна функция.
HardRule = Callable[[Job, BotSettings], str | None]


def _rule_low_spent(job: Job, s: BotSettings) -> str | None:
    if s.hard_min_client_spent <= 0:
        return None
    spent = job.client_total_spent or 0
    return f"low_spent:${spent:.0f}" if spent < s.hard_min_client_spent else None


def _rule_low_rating(job: Job, s: BotSettings) -> str | None:
    if s.hard_min_client_rating <= 0:
        return None
    hires = job.client_total_hires or 0
    rating = job.client_rating or 0
    if hires >= s.hard_min_hires_for_rating and rating < s.hard_min_client_rating:
        return f"low_rating:{rating:.1f}"
    return None


def _rule_low_hourly(job: Job, s: BotSettings) -> str | None:
    if s.hard_min_budget_hourly <= 0 or job.budget_type != "Hourly":
        return None
    mx = parse_hourly_budget_max(job.budget)
    return f"low_hourly:${mx:.0f}" if mx is not None and mx < s.hard_min_budget_hourly else None


def _rule_low_fixed(job: Job, s: BotSettings) -> str | None:
    if s.hard_min_budget_fixed <= 0 or job.budget_type != "Fixed":
        return None
    bg = parse_fixed_budget(job.budget)
    return f"low_fixed:${bg:.0f}" if bg is not None and bg < s.hard_min_budget_fixed else None


def _rule_no_hires(job: Job, s: BotSettings) -> str | None:
    return "no_hires" if s.hard_reject_no_hires and (job.client_total_hires or 0) == 0 else None


def _rule_stale(job: Job, s: BotSettings) -> str | None:
    if s.hard_max_vacancy_age_h <= 0 or not job.published_date:
        return None
    age_h = (datetime.now(UTC) - job.published_date).total_seconds() / 3600
    return f"stale:{age_h:.0f}h" if age_h > s.hard_max_vacancy_age_h else None


HARD_RULES: list[HardRule] = [
    _rule_low_spent,
    _rule_low_rating,
    _rule_low_hourly,
    _rule_low_fixed,
    _rule_no_hires,
    _rule_stale,
]


def hard_filter(job: Job, settings: BotSettings) -> str | None:
    """Возвращает короткую причину отказа или None если прошёл."""
    for rule in HARD_RULES:
        reason = rule(job, settings)
        if reason is not None:
            return reason
    return None


# --------------------------------------------------------------------------- #
# normalize_payload (PIPELINE.md §3)
# --------------------------------------------------------------------------- #
def normalize_payload(raw: Any) -> Job:
    """Строит Job из словаря или msgspec.Struct. Валидирует обязательные поля."""

    def get(name: str, default: Any = None) -> Any:
        if isinstance(raw, dict):
            return raw.get(name, default)
        return getattr(raw, name, default)

    upwork_job_id = get("upwork_job_id")
    if not upwork_job_id:
        raise ValueError("upwork_job_id is required")

    return Job(
        upwork_job_id=upwork_job_id,
        job_title=get("job_title", "") or "",
        job_description=get("job_description", "") or "",
        upwork_url=get("upwork_url", "") or "",
        published_date=get("published_date"),
        questions=get("questions"),
        job_type=get("job_type"),
        budget_type=get("budget_type"),
        budget=get("budget"),
        client_country=get("client_country"),
        client_rank=get("client_rank"),
        client_total_spent=get("client_total_spent"),
        client_total_hires=get("client_total_hires"),
        client_avg_rate=get("client_avg_rate"),
        client_rating=get("client_rating"),
        client_registered_at=get("client_registered_at"),
        client_reviews=get("client_reviews"),
    )


# --------------------------------------------------------------------------- #
# process_incoming_job — пять чистых стадий + оркестратор (PIPELINE.md §4)
# --------------------------------------------------------------------------- #
def _title80(job: Job) -> str:
    return (job.job_title or "")[:80]


async def _emit_finished(job: Job, result: str, **ctx: Any) -> None:
    """Унифицированная запись pipeline_finished — гарантия видимости в Логах
    даже если строка удалена из БД (PIPELINE.md §7.5)."""
    await log.emit(
        "pipeline_finished",
        upwork_job_id=job.upwork_job_id,
        result=result,
        job_title=_title80(job),
        **ctx,
    )


async def _stage_save(job: Job, pre: int) -> PipelineResult | None:
    """Первая запись в БД — ПОСЛЕ прохода дешёвой (pre_rating >= порога).

    Дедуп: упсёрт по UNIQUE(upwork_job_id). Существующая запись (inserted=False)
    = дубль (Vollna шлёт ту же вакансию из-за пересечения фильтров) → пропуск,
    дорогая нейронка повторно НЕ запускается, в TG вторично НЕ уходит. Атомарный
    INSERT…ON CONFLICT защищает и от одновременных батчей, и от повторной присылки.
    """
    inserted, _ = await db.upsert_and_get_state(job)
    if not inserted:
        return PipelineResult.SKIPPED_DUPLICATE
    await db.set_pre_rating_and_state(job.upwork_job_id, pre, "pre_screened")
    await log.emit(
        "job_received",
        upwork_job_id=job.upwork_job_id,
        job_title=_title80(job),
        client_country=job.client_country,
    )
    return None


async def _stage_hard_filter(job: Job, settings: BotSettings) -> PipelineResult | None:
    """Rule-based отсев ДО записи в БД (ничего ещё не сохранено — удалять нечего)."""
    reason = hard_filter(job, settings)
    if reason is None:
        return None
    await _emit_finished(job, "filtered_hard", reason=reason)
    return PipelineResult.FILTERED_HARD


async def _stage_pre_screen(job: Job, settings: BotSettings) -> int | PipelineResult:
    """Дешёвая нейронка ДО записи в БД. Возвращает pre_rating (int) на проходе,
    либо терминальный PipelineResult. В БД ничего не пишем: вакансии с
    pre_rating < порога (и упавшие) базу НЕ касаются вообще."""
    pre = await llm.pre_screen(None, job)
    if pre is None:
        await _emit_finished(job, "llm_failed", stage="pre_screen")
        return PipelineResult.LLM_FAILED
    if pre < settings.pre_screen_threshold:
        await _emit_finished(job, "filtered_pre", pre_rating=pre)
        return PipelineResult.FILTERED_PRE
    return pre


async def _stage_analyze(job: Job, settings: BotSettings) -> tuple[str, float] | PipelineResult:
    """Returns (card_text, rating_float) на успехе или PipelineResult — конечный.

    Дорогая нейронка возвращает JSON; мы парсим рейтинг (float, чтобы dispatch
    точно сравнил с порогами без ошибок округления) и собираем текст карточки.
    `card_text` сохраняется как ai_analysis и идёт в Telegram.
    """
    raw = await llm.analyze(None, job)
    if not raw:
        await db.bump_attempts(job.upwork_job_id, "analysis_short_or_empty")
        return PipelineResult.LLM_FAILED
    parsed = parse_analysis(raw)
    if parsed is None:
        await db.bump_attempts(job.upwork_job_id, "analysis_unparseable")
        return PipelineResult.LLM_FAILED
    rating_float = float(parsed["rating"])
    legacy = parsed.get("legacy_text")
    card = legacy if legacy is not None else notifier.render_analysis_card(parsed, job)
    if rating_float < settings.analysis_threshold:
        await _emit_finished(job, "filtered_analysis", rating=round(rating_float))
        await db.delete_job(job.upwork_job_id)
        return PipelineResult.FILTERED_ANALYSIS
    return card, rating_float


async def _stage_dispatch(
    job: Job, settings: BotSettings, analysis: str, rating_float: float
) -> PipelineResult:
    """Pause-aware финальный шаг: очередь или send→mark_sent.

    Перечитываем `settings` ИЗ БД (через cached-getter с актуальным TTL):
    между snapshot'ом из `process_incoming_job` и сейчас прошёл LLM-analysis
    (5-15 с) — пользователь мог войти в подменю (is_paused_menu=True) или
    нажать «Остановить» (is_paused=True). Без re-read эти изменения
    игнорируются и вакансия прорывается в TG. См. BOT.md §2 + §10.

    Принимаем `rating_float` для точного сравнения с loud_threshold;
    в БД (smallint колонка) кладём округлённое.
    """
    fresh = await db.get_settings_cached()
    rating = round(rating_float)

    # Ручная пауза имеет приоритет (PIPELINE.md §4).
    # Доп. фильтр: ночью копим в Отчёт только rating >= PAUSED_MIN_RATING,
    # «мелочь» < порога просто отбрасываем (день: analysis_threshold ~5,
    # ночь: PAUSED_MIN_RATING ~7).
    if fresh.is_paused:
        if rating_float < config.PAUSED_MIN_RATING:
            await _emit_finished(
                job,
                "filtered_paused",
                rating=rating,
                threshold=config.PAUSED_MIN_RATING,
            )
            await db.delete_job(job.upwork_job_id)
            return PipelineResult.FILTERED_ANALYSIS
        await db.set_analysis_state_queued(job.upwork_job_id, analysis, rating, "manual")
        await _emit_finished(job, "queued_manual", rating=rating)
        return PipelineResult.QUEUED_PAUSED

    if fresh.is_paused_menu:
        await db.set_analysis_state_queued(job.upwork_job_id, analysis, rating, "menu")
        await _emit_finished(job, "queued_menu", rating=rating)
        return PipelineResult.QUEUED_PAUSED

    await db.set_analysis_and_state(job.upwork_job_id, analysis, rating, "delivered")
    silent = rating_float < fresh.loud_notification_threshold
    await notifier.send_job(job, analysis, silent=silent)
    await db.mark_sent(job.upwork_job_id)
    await _emit_finished(job, "delivered", rating=rating, silent=silent)
    return PipelineResult.DELIVERED


async def process_incoming_job(job: Job, settings: BotSettings) -> PipelineResult:
    """Полный цикл обработки одной вакансии.

    Порядок: hard-фильтр → дешёвая нейронка → ЗАПИСЬ+дедуп → дорогая → dispatch.
    В БД пишем только после прохода дешёвой (pre_rating >= порога): вакансии ниже
    порога дешёвой базу не касаются вообще. Дорогая удаляет строку, если её
    рейтинг < analysis_threshold (записанные 5-6 живут кратко и удаляются)."""
    early = await _stage_hard_filter(job, settings)
    if early is not None:
        return early

    pre = await _stage_pre_screen(job, settings)
    if isinstance(pre, PipelineResult):
        return pre

    early = await _stage_save(job, pre)
    if early is not None:
        return early

    analyzed = await _stage_analyze(job, settings)
    if isinstance(analyzed, PipelineResult):
        return analyzed
    analysis, rating_float = analyzed

    return await _stage_dispatch(job, settings, analysis, rating_float)


# --------------------------------------------------------------------------- #
# safe_process_one + batch processor (PIPELINE.md §3)
# --------------------------------------------------------------------------- #
async def safe_process_one(
    raw_project: Any, settings: BotSettings, request_id: bytes
) -> PipelineResult:
    try:
        job = normalize_payload(raw_project)
    except Exception as e:
        await db.save_normalize_failure(request_id, _safe_bytes(raw_project), str(e))
        await log.emit(
            "normalize_failed",
            level=logging.ERROR,
            request_id=request_id.hex(),
            error=str(e)[:200],
        )
        return PipelineResult.LLM_FAILED
    return await process_incoming_job(job, settings)


def _safe_bytes(raw: Any) -> bytes:
    """Сериализовать входной project в bytes для save_normalize_failure."""
    if isinstance(raw, (bytes, bytearray)):
        return bytes(raw)
    try:
        return msgspec.json.encode(raw)
    except Exception:
        return repr(raw).encode("utf-8", errors="replace")


async def _process_batch_async(payload: WebhookBody, request_id: bytes) -> None:
    """Фоновая обработка batch'а из webhook (PIPELINE.md §3).

    Подчёркивание в имени намеренно: функция логически приватна для pipeline,
    но импортируется http_app.py — это спецификация (PIPELINE.md §2)."""
    started_at = time.monotonic()
    settings = await db.get_settings_cached()
    projects = list(payload.body.projects)
    n = len(projects)

    sem = asyncio.Semaphore(BATCH_FANOUT_LIMIT)

    async def _run_bounded(project: Any) -> PipelineResult:
        async with sem:
            return await asyncio.wait_for(
                safe_process_one(project, settings, request_id),
                timeout=PIPELINE_BACKGROUND_TIMEOUT,
            )

    tasks = [asyncio.create_task(_run_bounded(project)) for project in projects]
    results = await asyncio.gather(*tasks, return_exceptions=True) if tasks else []

    counts = collections.Counter(
        r.value if isinstance(r, PipelineResult) else "exception" for r in results
    )
    await db.mark_request_processed(request_id)
    await log.emit(
        "batch_finished",
        request_id=request_id.hex(),
        n=n,
        duration_ms=int((time.monotonic() - started_at) * 1000),
        **dict(counts),
    )
