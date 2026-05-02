"""asyncpg pool + helper-функции CRUD. См. ../DATABASE.md и PIPELINE.md."""

from __future__ import annotations

import time
from enum import StrEnum
from typing import TYPE_CHECKING, Any

import msgspec

from src.models import BotSettings, Job

if TYPE_CHECKING:
    from asyncpg import Pool

# --------------------------------------------------------------------------- #
# Глобальный пул и in-memory кэши (см. ARCHITECTURE.md §5.2)
# --------------------------------------------------------------------------- #
_pool: Pool | None = None

_settings_cache: tuple[Pool, BotSettings, float] | None = None
_SETTINGS_TTL = 10.0

_prompt_cache: dict[str, tuple[Pool, str, float]] = {}
_PROMPT_TTL = 60.0

_secret_cache: dict[str, tuple[Pool, str, float]] = {}
_SECRET_TTL = 60.0

_count_cache: dict[tuple[int, str], tuple[int, float]] = {}
_COUNT_TTL = 10.0


# --------------------------------------------------------------------------- #
# Whitelist для динамических колонок — защита от SQL-инъекции (DATABASE.md §2)
# --------------------------------------------------------------------------- #
SETTING_FIELDS: frozenset[str] = frozenset(
    {
        "is_paused",
        "is_paused_menu",
        "pre_screen_threshold",
        "analysis_threshold",
        "loud_notification_threshold",
        "hard_min_client_spent",
        "hard_min_client_rating",
        "hard_min_hires_for_rating",
        "hard_min_budget_hourly",
        "hard_min_budget_fixed",
        "hard_reject_no_hires",
        "hard_max_vacancy_age_h",
    }
)

MODEL_COLUMNS: frozenset[str] = frozenset(
    {
        "prescreen_model",
        "analysis_model",
        "prescreen_fallback_model",
        "analysis_fallback_model",
    }
)


class LogFilter(StrEnum):
    """Контролируемые WHERE-фрагменты для bot_events (UI Логи, BOT.md §11)."""

    ALL = "TRUE"
    ERRORS = "level >= 1"


def _check_field(field: str, allowed: frozenset[str]) -> str:
    if field not in allowed:
        raise ValueError(f"unknown field: {field!r}")
    return field


def _conn() -> Pool:
    """Возвращает инициализированный пул. Raises если init() ещё не вызывался."""
    if _pool is None:
        raise RuntimeError("db.init(pool) must be called before db operations")
    return _pool


# --------------------------------------------------------------------------- #
# Init
# --------------------------------------------------------------------------- #
async def init(pool: Pool) -> None:
    """Сохранить пул для использования в helper-функциях."""
    global _pool
    _pool = pool


# --------------------------------------------------------------------------- #
# webhook_inbox / normalize_failures
# --------------------------------------------------------------------------- #
async def try_register_request(request_id: bytes) -> bool:
    """INSERT в webhook_inbox с ON CONFLICT DO NOTHING. True — новая запись."""
    inserted = await _conn().fetchval(
        """
        INSERT INTO webhook_inbox (request_id) VALUES ($1)
        ON CONFLICT (request_id) DO NOTHING
        RETURNING true
        """,
        request_id,
    )
    return bool(inserted)


async def save_normalize_failure(request_id: bytes, raw_payload: bytes, error: str) -> None:
    """Сохраняет битый webhook-payload в normalize_failures (DATABASE.md §7).

    raw_payload — bytes которые сломали msgspec-парсинг (PIPELINE.md §2). Колонка
    `raw_payload` имеет тип jsonb, поэтому если bytes — валидный JSON, сохраняем
    как-есть; иначе заворачиваем в `{"raw": "<decoded>"}` чтобы Postgres не упал.
    """
    decoded = raw_payload.decode("utf-8", errors="replace")
    try:
        msgspec.json.decode(raw_payload)
        payload_json = decoded
    except (msgspec.DecodeError, msgspec.ValidationError):
        payload_json = msgspec.json.encode({"raw": decoded}).decode()
    await _conn().execute(
        """
        INSERT INTO normalize_failures (request_id, raw_payload, error)
        VALUES ($1, $2::jsonb, $3)
        """,
        request_id,
        payload_json,
        error[:500],
    )


async def mark_request_processed(request_id: bytes) -> None:
    await _conn().execute(
        "UPDATE webhook_inbox SET processed_at = now() WHERE request_id = $1",
        request_id,
    )


# --------------------------------------------------------------------------- #
# upwork_jobs — основная таблица
# --------------------------------------------------------------------------- #
async def upsert_and_get_state(job: Job) -> tuple[bool, str]:
    """INSERT вакансии или вернуть текущее состояние существующей.

    Returns (inserted, current_state).
    """
    row = await _conn().fetchrow(
        """
        INSERT INTO upwork_jobs (
            upwork_job_id, job_title, job_description, upwork_url,
            published_date, questions, job_type, budget_type, budget,
            client_country, client_rank, client_total_spent, client_total_hires,
            client_avg_rate, client_rating, client_registered_at, client_reviews
        ) VALUES (
            $1, $2, $3, $4, $5, $6, $7, $8, $9,
            $10, $11, $12, $13, $14, $15, $16, $17
        )
        ON CONFLICT (upwork_job_id) DO UPDATE SET upwork_job_id = EXCLUDED.upwork_job_id
        RETURNING (xmax = 0) AS inserted, processing_state
        """,
        job.upwork_job_id,
        job.job_title,
        job.job_description,
        job.upwork_url,
        job.published_date,
        job.questions,
        job.job_type,
        job.budget_type,
        job.budget,
        job.client_country,
        job.client_rank,
        job.client_total_spent,
        job.client_total_hires,
        job.client_avg_rate,
        job.client_rating,
        job.client_registered_at,
        job.client_reviews,
    )
    if row is None:
        return (False, "pending")
    return bool(row["inserted"]), str(row["processing_state"])


# Whitelisted колонки для _update_job — все state-перехода идут через них.
_JOB_UPDATABLE_FIELDS: frozenset[str] = frozenset(
    {
        "processing_state",
        "pre_rating",
        "rating",
        "ai_analysis",
        "last_error",
        "queued_reason",
        "is_sent",
        "is_favorite",
    }
)


async def _update_job(upwork_job_id: str, *, attempts_inc: bool = False, **fields: Any) -> None:
    """Атомарный UPDATE upwork_jobs SET ... WHERE upwork_job_id = $1.

    Поля валидируются по whitelist'у. attempts_inc=True добавляет attempts = attempts + 1.
    Используется state-transition-функциями ниже — один SQL вместо N (PIPELINE.md §4).
    """
    for k in fields:
        _check_field(k, _JOB_UPDATABLE_FIELDS)

    set_parts: list[str] = []
    values: list[Any] = []
    for i, (k, v) in enumerate(fields.items(), start=2):
        set_parts.append(f"{k} = ${i}")
        values.append(v)
    if attempts_inc:
        set_parts.append("attempts = attempts + 1")
    if not set_parts:
        return

    sql = "UPDATE upwork_jobs SET " + ", ".join(set_parts) + " WHERE upwork_job_id = $1"
    await _conn().execute(sql, upwork_job_id, *values)


async def delete_job(upwork_job_id: str) -> None:
    await _conn().execute("DELETE FROM upwork_jobs WHERE upwork_job_id = $1", upwork_job_id)


async def mark_failed(upwork_job_id: str, last_error: str) -> None:
    await _update_job(
        upwork_job_id,
        attempts_inc=True,
        processing_state="failed",
        last_error=last_error[:200],
    )


async def bump_attempts(upwork_job_id: str, last_error: str) -> None:
    await _update_job(
        upwork_job_id,
        attempts_inc=True,
        last_error=last_error[:200],
    )


async def set_pre_rating_and_state(upwork_job_id: str, pre_rating: int, state: str) -> None:
    await _update_job(upwork_job_id, pre_rating=pre_rating, processing_state=state)


async def set_analysis_and_state(
    upwork_job_id: str, ai_analysis: str, rating: int, state: str
) -> None:
    await _update_job(
        upwork_job_id,
        ai_analysis=ai_analysis,
        rating=rating,
        processing_state=state,
    )


async def set_analysis_state_queued(
    upwork_job_id: str, ai_analysis: str, rating: int, queued_reason: str
) -> None:
    await _update_job(
        upwork_job_id,
        ai_analysis=ai_analysis,
        rating=rating,
        processing_state="analyzed",
        queued_reason=queued_reason,
    )


async def mark_sent(upwork_job_id: str) -> None:
    await _update_job(upwork_job_id, is_sent=True, processing_state="delivered")


async def set_favorite(upwork_job_id: str, value: bool) -> None:
    """Inline-кнопка `Избранное` на карточке вакансии (BOT.md §9)."""
    await _update_job(upwork_job_id, is_favorite=value)


async def get_analysis(upwork_job_id: str) -> str:
    """Inline `Анализ` — показать сохранённый ai_analysis (BOT.md §9)."""
    val = await _conn().fetchval(
        "SELECT ai_analysis FROM upwork_jobs WHERE upwork_job_id = $1",
        upwork_job_id,
    )
    return val or ""


async def get_card(upwork_job_id: str) -> tuple[str, str]:
    """Inline `Карточка` — (job_title, upwork_url). Empty strings если не найдено."""
    row = await _conn().fetchrow(
        "SELECT job_title, upwork_url FROM upwork_jobs WHERE upwork_job_id = $1",
        upwork_job_id,
    )
    if row is None:
        return ("", "")
    return (row["job_title"] or "", row["upwork_url"] or "")


async def get_job_full(upwork_job_id: str) -> dict | None:
    """Полная карточка для view-toggle: title/description/questions/url/analysis."""
    row = await _conn().fetchrow(
        """
        SELECT upwork_job_id, job_title, job_description, questions,
               upwork_url, ai_analysis, is_favorite
        FROM upwork_jobs WHERE upwork_job_id = $1
        """,
        upwork_job_id,
    )
    return dict(row) if row is not None else None


async def list_favorites() -> list[dict]:
    """Все избранные вакансии для submenu (BOT.md §9, обновлённый)."""
    rows = await _conn().fetch(
        """
        SELECT upwork_job_id, job_title, job_description, questions,
               upwork_url, ai_analysis
        FROM upwork_jobs
        WHERE is_favorite = true
        ORDER BY rating DESC NULLS LAST, created_at DESC
        """,
    )
    return [dict(r) for r in rows]


async def clear_all_favorites() -> int:
    """Снимает is_favorite=true со всех — кнопка `Очистить всё` в подменю."""
    rows = await _conn().fetch(
        "UPDATE upwork_jobs SET is_favorite = false WHERE is_favorite = true RETURNING 1",
    )
    return len(rows)


# --------------------------------------------------------------------------- #
# bot_settings + кэш (DATABASE.md §2)
# --------------------------------------------------------------------------- #
def _row_to_bot_settings(row: dict) -> BotSettings:
    return BotSettings(
        is_paused=row["is_paused"],
        is_paused_menu=row["is_paused_menu"],
        pre_screen_threshold=row["pre_screen_threshold"],
        analysis_threshold=row["analysis_threshold"],
        hard_min_client_spent=float(row["hard_min_client_spent"]),
        hard_min_client_rating=float(row["hard_min_client_rating"]),
        hard_min_hires_for_rating=row["hard_min_hires_for_rating"],
        hard_min_budget_hourly=float(row["hard_min_budget_hourly"]),
        hard_min_budget_fixed=float(row["hard_min_budget_fixed"]),
        hard_reject_no_hires=row["hard_reject_no_hires"],
        hard_max_vacancy_age_h=row["hard_max_vacancy_age_h"],
        prescreen_model=row["prescreen_model"],
        analysis_model=row["analysis_model"],
        prescreen_fallback_model=row["prescreen_fallback_model"],
        analysis_fallback_model=row["analysis_fallback_model"],
        loud_notification_threshold=row["loud_notification_threshold"],
    )


async def get_settings_cached() -> BotSettings:
    """TTL-кэшированное чтение всей строки bot_settings (один read на batch)."""
    global _settings_cache
    now = time.monotonic()
    if _settings_cache is not None:
        cached_pool, value, ts = _settings_cache
        if cached_pool is _pool and (now - ts) < _SETTINGS_TTL:
            return value
    row = await _conn().fetchrow("SELECT * FROM bot_settings WHERE id = 1")
    value = _row_to_bot_settings(dict(row)) if row is not None else BotSettings()
    _settings_cache = (_pool, value, now)
    return value


async def get_settings_full(pool: Pool | None = None) -> BotSettings:
    """То же что get_settings_cached, но всегда свежий read."""
    p = pool if pool is not None else _conn()
    row = await p.fetchrow("SELECT * FROM bot_settings WHERE id = 1")
    if row is None:
        return BotSettings()
    return _row_to_bot_settings(dict(row))


async def invalidate_settings_cache() -> None:
    global _settings_cache
    _settings_cache = None


async def get_setting(field: str) -> Any:
    _check_field(field, SETTING_FIELDS)
    return await _conn().fetchval(f"SELECT {field} FROM bot_settings WHERE id = 1")


async def set_setting(field: str, value: Any) -> None:
    _check_field(field, SETTING_FIELDS)
    await _conn().execute(
        f"UPDATE bot_settings SET {field} = $1, updated_at = now() WHERE id = 1",
        value,
    )


async def set_paused_menu(value: bool) -> None:
    """Авто-пауза при входе в меню (BOT.md §2).

    КРИТИЧНО: инвалидируем settings-cache, иначе pipeline ещё до 10s после смены
    видит старое значение и доставляет вакансии вместо очереди (или наоборот).
    """
    await _conn().execute(
        "UPDATE bot_settings SET is_paused_menu = $1, updated_at = now() WHERE id = 1",
        value,
    )
    await invalidate_settings_cache()


# --------------------------------------------------------------------------- #
# ai_prompts + кэш (DATABASE.md §3, §4)
# --------------------------------------------------------------------------- #
async def get_prompt(slot: str) -> str:
    val = await _conn().fetchval("SELECT content FROM ai_prompts WHERE slot = $1", slot)
    return val or ""


async def get_prompt_cached(slot: str) -> str:
    now = time.monotonic()
    cached = _prompt_cache.get(slot)
    if cached is not None:
        cached_pool, value, ts = cached
        if cached_pool is _pool and (now - ts) < _PROMPT_TTL:
            return value
    val = await _conn().fetchval("SELECT content FROM ai_prompts WHERE slot = $1", slot)
    val = val or ""
    _prompt_cache[slot] = (_pool, val, now)
    return val


async def invalidate_prompt_cache(slot: str | None = None) -> None:
    if slot is None:
        _prompt_cache.clear()
    else:
        _prompt_cache.pop(slot, None)


async def insert_prompt_history(slot: str, content_before: str, edited_by: int | None) -> None:
    await _conn().execute(
        """
        INSERT INTO prompts_history (slot, content_before, edited_by)
        VALUES ($1, $2, $3)
        """,
        slot,
        content_before,
        edited_by,
    )


async def update_prompt(slot: str, content: str) -> None:
    await _conn().execute(
        """
        UPDATE ai_prompts
        SET content = $2, updated_at = now()
        WHERE slot = $1
        """,
        slot,
        content,
    )


# --------------------------------------------------------------------------- #
# Models setters (4 колонки в bot_settings — whitelisted)
# --------------------------------------------------------------------------- #
async def get_model(column: str) -> str:
    _check_field(column, MODEL_COLUMNS)
    val = await _conn().fetchval(f"SELECT {column} FROM bot_settings WHERE id = 1")
    return val or ""


async def set_model(column: str, value: str) -> None:
    _check_field(column, MODEL_COLUMNS)
    await _conn().execute(
        f"UPDATE bot_settings SET {column} = $1, updated_at = now() WHERE id = 1",
        value,
    )


# --------------------------------------------------------------------------- #
# secrets + кэш (DATABASE.md §6)
# --------------------------------------------------------------------------- #
async def get_openrouter_key() -> str:
    """Приоритет: secrets (БД) → OPENROUTER_API_KEY env (ARCHITECTURE.md §4.1)."""
    now = time.monotonic()
    cached = _secret_cache.get("openrouter_api_key")
    if cached is not None:
        cached_pool, value, ts = cached
        if cached_pool is _pool and (now - ts) < _SECRET_TTL:
            return value
    val = await _conn().fetchval("SELECT value FROM secrets WHERE name = 'openrouter_api_key'")
    if val is None:
        from src import config

        val = config.OPENROUTER_API_KEY
    _secret_cache["openrouter_api_key"] = (_pool, val, now)
    return val


async def invalidate_secrets_cache() -> None:
    _secret_cache.clear()


async def set_secret(name: str, value: str, updated_by: int | None) -> None:
    await _conn().execute(
        """
        INSERT INTO secrets (name, value, updated_by, updated_at)
        VALUES ($1, $2, $3, now())
        ON CONFLICT (name) DO UPDATE SET
            value = EXCLUDED.value,
            updated_by = EXCLUDED.updated_by,
            updated_at = now()
        """,
        name,
        value,
        updated_by,
    )


# --------------------------------------------------------------------------- #
# bot_events (DATABASE.md §8)
# --------------------------------------------------------------------------- #
async def insert_event(level: int, event: str, data: dict | None) -> None:
    payload = msgspec.json.encode(data or {}).decode()
    await _conn().execute(
        "INSERT INTO bot_events (level, event, data) VALUES ($1, $2, $3::jsonb)",
        level,
        event,
        payload,
    )


async def fetch_events(
    log_filter: LogFilter = LogFilter.ALL, offset: int = 0, limit: int = 10
) -> list[dict]:
    sql = (
        "SELECT ts, level, event, data FROM bot_events "
        f"WHERE {log_filter.value} "
        "ORDER BY ts DESC OFFSET $1 LIMIT $2"
    )
    rows = await _conn().fetch(sql, offset, limit)
    return [dict(r) for r in rows]


async def count_events(log_filter: LogFilter = LogFilter.ALL) -> int:
    sql = f"SELECT COUNT(*) FROM bot_events WHERE {log_filter.value}"
    return int(await _conn().fetchval(sql) or 0)


async def clear_all_events() -> int:
    """TRUNCATE bot_events — кнопка `Очистить все логи` в подменю Логов."""
    n = await count_events()
    await _conn().execute("TRUNCATE bot_events RESTART IDENTITY")
    return n


async def get_recent_changes(event: str, field: str, limit: int = 3) -> list[dict]:
    """Последние N изменений конкретного поля для блока «Последние изменения» (BOT.md §8).

    Используется на карточках настроек: порог, модель, ключ, промт.
    """
    rows = await _conn().fetch(
        """
        SELECT ts, data
        FROM bot_events
        WHERE event = $1 AND data->>'field' = $2
        ORDER BY ts DESC
        LIMIT $3
        """,
        event,
        field,
        limit,
    )
    return [dict(r) for r in rows]


# --------------------------------------------------------------------------- #
# Очереди — Отчёт (manual) / Синхронизация (menu). См. PIPELINE.md §4, BOT.md §10.
# --------------------------------------------------------------------------- #
async def peek_queued_by_reason(reason: str) -> list[dict]:
    rows = await _conn().fetch(
        """
        SELECT id, upwork_job_id, job_title, rating, client_country, budget,
               ai_analysis, upwork_url
        FROM upwork_jobs
        WHERE is_sent = false AND queued_reason = $1 AND ai_analysis IS NOT NULL
        ORDER BY rating DESC NULLS LAST, created_at DESC
        """,
        reason,
    )
    return [dict(r) for r in rows]


async def drain_queued_by_reason(
    reason: str, limit: int | None = None, order_by_rating: bool = False
) -> list[dict]:
    """UPDATE ... SET is_sent=true ... RETURNING — атомарная выгрузка очереди.

    `order_by_rating` сейчас не меняет порядок — он уже rating DESC, created_at DESC.
    Параметр оставлен для совместимости callers (BOT.md §10 «Выгрузить топ-5»).
    """
    del order_by_rating  # уже встроено в ORDER BY ниже
    limit_clause = "LIMIT $2" if limit is not None else ""
    args: tuple = (reason,) if limit is None else (reason, limit)
    sql = f"""
        UPDATE upwork_jobs SET is_sent = true, queued_reason = NULL
        WHERE id IN (
          SELECT id FROM upwork_jobs
          WHERE is_sent = false AND queued_reason = $1 AND ai_analysis IS NOT NULL
          ORDER BY rating DESC NULLS LAST, created_at DESC
          {limit_clause}
          FOR UPDATE SKIP LOCKED
        )
        RETURNING id, ai_analysis, upwork_url, upwork_job_id, job_title, rating
        """
    rows = await _conn().fetch(sql, *args)
    return [dict(r) for r in rows]


async def mark_queued_as_sent(reason: str) -> int:
    """Помечает все очередные строки как отправленные. Возвращает кол-во."""
    rows = await _conn().fetch(
        """
        UPDATE upwork_jobs SET is_sent = true, queued_reason = NULL
        WHERE is_sent = false AND queued_reason = $1 AND ai_analysis IS NOT NULL
        RETURNING 1
        """,
        reason,
    )
    return len(rows)


# --------------------------------------------------------------------------- #
# Cached counters — для главного меню (BOT.md §1)
# --------------------------------------------------------------------------- #
def _count_get(pool: Pool, key: str) -> int | None:
    cached = _count_cache.get((id(pool), key))
    if cached is None:
        return None
    val, ts = cached
    if (time.monotonic() - ts) >= _COUNT_TTL:
        _count_cache.pop((id(pool), key), None)
        return None
    return val


def _count_put(pool: Pool, key: str, val: int) -> None:
    _count_cache[(id(pool), key)] = (val, time.monotonic())


async def count_queued_by_reason_cached(pool: Pool, reason: str) -> int:
    key = f"queued:{reason}"
    cached = _count_get(pool, key)
    if cached is not None:
        return cached
    val = await pool.fetchval(
        """
        SELECT COUNT(*) FROM upwork_jobs
        WHERE is_sent = false AND queued_reason = $1 AND ai_analysis IS NOT NULL
        """,
        reason,
    )
    val = int(val or 0)
    _count_put(pool, key, val)
    return val


async def count_favorites_cached(pool: Pool) -> int:
    key = "favorites"
    cached = _count_get(pool, key)
    if cached is not None:
        return cached
    val = await pool.fetchval("SELECT COUNT(*) FROM upwork_jobs WHERE is_favorite = true")
    val = int(val or 0)
    _count_put(pool, key, val)
    return val


# --------------------------------------------------------------------------- #
# Прочее
# --------------------------------------------------------------------------- #
async def truncate_jobs() -> None:
    await _conn().execute("TRUNCATE TABLE upwork_jobs RESTART IDENTITY")
