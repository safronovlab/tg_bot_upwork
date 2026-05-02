"""process_incoming_job() + parse_rating + normalize + hard_filter. См. PIPELINE.md."""

from __future__ import annotations

import asyncio
import collections
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


def parse_rating(text: str) -> int:
    """Парсит РЕЙТИНГ: N из ai_analysis, clamped to 0..10."""
    if not text:
        return 0
    m = RATING_RE.search(text)
    if not m:
        return 0
    val = float(m.group(1).replace(",", "."))
    return max(0, min(10, round(val)))


def parse_pre_rating(text: str) -> int | None:
    """Pre-screen: None если ответ непарсимый."""
    if not text:
        return None
    m = re.search(r"-?\d+", text)
    if not m:
        return None
    val = int(m.group(0))
    return val if 0 <= val <= 10 else None


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


async def _stage_save_and_emit_received(job: Job) -> PipelineResult | None:
    """Save first, then process — упсёрт ДО любого LLM (PIPELINE.md §7.1.2)."""
    inserted, current_state = await db.upsert_and_get_state(job)
    if not inserted and current_state in TERMINAL_STATES:
        return PipelineResult.SKIPPED_DUPLICATE
    await log.emit(
        "job_received",
        upwork_job_id=job.upwork_job_id,
        job_title=_title80(job),
        client_country=job.client_country,
    )
    return None


async def _stage_hard_filter(job: Job, settings: BotSettings) -> PipelineResult | None:
    reason = hard_filter(job, settings)
    if reason is None:
        return None
    await _emit_finished(job, "filtered_hard", reason=reason)
    await db.delete_job(job.upwork_job_id)
    return PipelineResult.FILTERED_HARD


async def _stage_pre_screen(job: Job, settings: BotSettings) -> PipelineResult | None:
    pre = await llm.pre_screen(None, job)
    if pre is None:
        await db.mark_failed(job.upwork_job_id, "pre_screen_no_response")
        return PipelineResult.LLM_FAILED
    if pre < settings.pre_screen_threshold:
        await _emit_finished(job, "filtered_pre", pre_rating=pre)
        await db.delete_job(job.upwork_job_id)
        return PipelineResult.FILTERED_PRE
    await db.set_pre_rating_and_state(job.upwork_job_id, pre, "pre_screened")
    return None


async def _stage_analyze(job: Job, settings: BotSettings) -> tuple[str, int] | PipelineResult:
    """Returns (analysis, rating) на успехе или PipelineResult — конечный."""
    analysis = await llm.analyze(None, job)
    if not analysis or len(analysis) < 50:
        await db.bump_attempts(job.upwork_job_id, "analysis_short_or_empty")
        return PipelineResult.LLM_FAILED
    rating = parse_rating(analysis)
    if rating < settings.analysis_threshold:
        await _emit_finished(job, "filtered_analysis", rating=rating)
        await db.delete_job(job.upwork_job_id)
        return PipelineResult.FILTERED_ANALYSIS
    return analysis, rating


async def _stage_dispatch(
    job: Job, settings: BotSettings, analysis: str, rating: int
) -> PipelineResult:
    """Pause-aware финальный шаг: очередь или send→mark_sent."""
    # Ручная пауза имеет приоритет (PIPELINE.md §4)
    if settings.is_paused:
        await db.set_analysis_state_queued(job.upwork_job_id, analysis, rating, "manual")
        await _emit_finished(job, "queued_manual", rating=rating)
        return PipelineResult.QUEUED_PAUSED

    if settings.is_paused_menu:
        await db.set_analysis_state_queued(job.upwork_job_id, analysis, rating, "menu")
        await _emit_finished(job, "queued_menu", rating=rating)
        return PipelineResult.QUEUED_PAUSED

    await db.set_analysis_and_state(job.upwork_job_id, analysis, rating, "delivered")
    silent = rating < settings.loud_notification_threshold
    await notifier.send_job(job, analysis, silent=silent)
    await db.mark_sent(job.upwork_job_id)
    await _emit_finished(job, "delivered", rating=rating, silent=silent)
    return PipelineResult.DELIVERED


async def process_incoming_job(job: Job, settings: BotSettings) -> PipelineResult:
    """Полный цикл обработки одной вакансии. Стадии — 5 helper'ов выше."""
    early = await _stage_save_and_emit_received(job)
    if early is not None:
        return early

    early = await _stage_hard_filter(job, settings)
    if early is not None:
        return early

    early = await _stage_pre_screen(job, settings)
    if early is not None:
        return early

    analyzed = await _stage_analyze(job, settings)
    if isinstance(analyzed, PipelineResult):
        return analyzed
    analysis, rating = analyzed

    return await _stage_dispatch(job, settings, analysis, rating)


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
