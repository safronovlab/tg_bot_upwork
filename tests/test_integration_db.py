"""Integration tests против реальной Postgres через testcontainers.

Покрывает:
  - применение schema.sql + миграций (DATABASE.md §9)
  - whitelisted UPDATE через `_update_job` (PIPELINE.md §4)
  - upsert_and_get_state с RETURNING (xmax = 0) — asyncpg-специфика
  - drain_queued_by_reason с FOR UPDATE SKIP LOCKED
  - mark_queued_as_sent + RETURNING подсчёт
  - все CHECK constraints из schema.sql
  - триггер touch_updated_at
  - FK CASCADE webhook_inbox → normalize_failures
  - bootstrap_done логика migrations.init_schema

Требует Docker. На CI использовать как отдельный job с docker-in-docker
или с docker-host через bind mount.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator
from typing import Any

import asyncpg
import pytest
from src import db, migrations
from src.models import Job
from testcontainers.postgres import PostgresContainer

# Маркируем все тесты в файле — позволяет в CI запустить отдельно через `pytest -m integration`
pytestmark = pytest.mark.integration


# --------------------------------------------------------------------------- #
# Postgres container — один на сессию, дешевле чем поднимать на каждый тест
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="session")
def postgres_url() -> Any:
    """Поднимает Postgres 16 в Docker, возвращает asyncpg-URL."""
    with PostgresContainer("postgres:16-alpine", driver=None) as pg:
        # testcontainers возвращает sync URL (postgresql+psycopg2://...) — приводим к asyncpg.
        host = pg.get_container_host_ip()
        port = pg.get_exposed_port(5432)
        url = f"postgresql://{pg.username}:{pg.password}@{host}:{port}/{pg.dbname}"
        yield url


@pytest.fixture
async def real_pool(postgres_url: str) -> AsyncIterator[asyncpg.Pool]:
    """Чистый пул с применённой schema.sql на каждый тест."""
    pool = await asyncpg.create_pool(postgres_url, min_size=1, max_size=4, command_timeout=10)
    # Каждый тест — пустая БД: дропаем всё и применяем schema заново.
    async with pool.acquire() as conn:
        await conn.execute(
            """
            DROP SCHEMA public CASCADE;
            CREATE SCHEMA public;
            """
        )
    try:
        await migrations.init_schema(pool)
        yield pool
    finally:
        await pool.close()


@pytest.fixture
def use_real_pool(real_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch) -> asyncpg.Pool:
    """Подсунуть real_pool в module-global db._pool (см. ARCHITECTURE.md §5.2)."""
    monkeypatch.setattr(db, "_pool", real_pool, raising=False)
    return real_pool


def _make_job(upwork_job_id: str = "~01abc", **overrides: Any) -> Job:
    base = {
        "upwork_job_id": upwork_job_id,
        "job_title": "Senior Python dev",
        "job_description": "Build us a pipeline " * 10,
        "upwork_url": f"https://www.upwork.com/jobs/{upwork_job_id}",
        "published_date": None,
        "questions": None,
        "job_type": "Full time",
        "budget_type": "Hourly",
        "budget": "$30-$60",
        "client_country": "US",
        "client_rank": "Plus",
        "client_total_spent": 5000.0,
        "client_total_hires": 12,
        "client_avg_rate": 35.0,
        "client_rating": 4.8,
        "client_registered_at": None,
        "client_reviews": None,
    }
    base.update(overrides)
    return Job(**base)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Schema.sql и миграции (DATABASE.md §9)
# --------------------------------------------------------------------------- #
class TestSchemaApplies:
    async def test_all_tables_exist(self, real_pool: asyncpg.Pool) -> None:
        rows = await real_pool.fetch(
            "SELECT table_name FROM information_schema.tables WHERE table_schema='public'"
        )
        names = {r["table_name"] for r in rows}
        for t in [
            "upwork_jobs",
            "bot_settings",
            "ai_prompts",
            "prompts_history",
            "webhook_inbox",
            "secrets",
            "normalize_failures",
            "bot_events",
            "schema_version",
        ]:
            assert t in names, f"missing table: {t}"

    async def test_enums_created(self, real_pool: asyncpg.Pool) -> None:
        proc_state = await real_pool.fetch(
            "SELECT enumlabel FROM pg_enum "
            "WHERE enumtypid = 'proc_state'::regtype ORDER BY enumsortorder"
        )
        assert {r["enumlabel"] for r in proc_state} == {
            "pending",
            "pre_screened",
            "analyzed",
            "delivered",
            "filtered",
            "failed",
        }

    async def test_bot_settings_singleton_seeded(self, real_pool: asyncpg.Pool) -> None:
        n = await real_pool.fetchval("SELECT COUNT(*) FROM bot_settings")
        assert n == 1
        row = await real_pool.fetchrow("SELECT id, is_paused, prescreen_model FROM bot_settings")
        assert row["id"] == 1
        assert row["is_paused"] is False
        assert "/" in row["prescreen_model"]  # vendor/model

    async def test_check_constraint_id_eq_1(self, real_pool: asyncpg.Pool) -> None:
        with pytest.raises(asyncpg.CheckViolationError):
            await real_pool.execute("INSERT INTO bot_settings (id) VALUES (2)")

    async def test_check_constraint_thresholds_range(self, real_pool: asyncpg.Pool) -> None:
        with pytest.raises(asyncpg.CheckViolationError):
            await real_pool.execute(
                "UPDATE bot_settings SET pre_screen_threshold = 11 WHERE id = 1"
            )

    async def test_check_constraint_queued_reason(self, real_pool: asyncpg.Pool) -> None:
        with pytest.raises(asyncpg.CheckViolationError):
            await real_pool.execute(
                "INSERT INTO upwork_jobs (upwork_job_id, queued_reason) VALUES ('x', 'invalid')"
            )

    async def test_trigger_touch_updated_at(
        self, real_pool: asyncpg.Pool, use_real_pool: asyncpg.Pool
    ) -> None:
        await db.upsert_and_get_state(_make_job("~01trigger"))
        before = await real_pool.fetchval(
            "SELECT updated_at FROM upwork_jobs WHERE upwork_job_id = '~01trigger'"
        )
        await asyncio.sleep(0.01)
        await db.set_pre_rating_and_state("~01trigger", 5, "pre_screened")
        after = await real_pool.fetchval(
            "SELECT updated_at FROM upwork_jobs WHERE upwork_job_id = '~01trigger'"
        )
        assert after > before  # триггер touch_updated_at сработал


class TestMigrationsBootstrap:
    async def test_idempotent_re_run(self, real_pool: asyncpg.Pool) -> None:
        """Повторный init_schema на уже инициализированной БД не должен падать."""
        await migrations.init_schema(real_pool)  # уже применили в фикстуре, повторяем
        await migrations.init_schema(real_pool)


# --------------------------------------------------------------------------- #
# Idempotency на webhook (DATABASE.md §5)
# --------------------------------------------------------------------------- #
class TestWebhookIdempotency:
    async def test_first_insert_returns_true(self, use_real_pool: asyncpg.Pool) -> None:
        rid = hashlib.sha256(b"payload-1").digest()
        assert await db.try_register_request(rid) is True

    async def test_duplicate_returns_false(self, use_real_pool: asyncpg.Pool) -> None:
        rid = hashlib.sha256(b"payload-2").digest()
        assert await db.try_register_request(rid) is True
        assert await db.try_register_request(rid) is False

    async def test_normalize_failure_cascades_on_inbox_delete(
        self, real_pool: asyncpg.Pool, use_real_pool: asyncpg.Pool
    ) -> None:
        rid = hashlib.sha256(b"bad-json").digest()
        await db.try_register_request(rid)
        await db.save_normalize_failure(rid, b"{not json", "ValidationError")
        # FK CASCADE — DELETE из inbox должен удалить и normalize_failures
        await real_pool.execute("DELETE FROM webhook_inbox WHERE request_id = $1", rid)
        n = await real_pool.fetchval(
            "SELECT COUNT(*) FROM normalize_failures WHERE request_id = $1", rid
        )
        assert n == 0


# --------------------------------------------------------------------------- #
# upsert_and_get_state — RETURNING (xmax = 0) (PIPELINE.md §4)
# --------------------------------------------------------------------------- #
class TestUpsertAndGetState:
    async def test_first_insert_returns_inserted_true(self, use_real_pool: asyncpg.Pool) -> None:
        inserted, state = await db.upsert_and_get_state(_make_job("~01first"))
        assert inserted is True
        assert state == "pending"

    async def test_duplicate_returns_inserted_false(self, use_real_pool: asyncpg.Pool) -> None:
        await db.upsert_and_get_state(_make_job("~01dup"))
        inserted, state = await db.upsert_and_get_state(_make_job("~01dup"))
        assert inserted is False
        assert state == "pending"

    async def test_duplicate_in_terminal_state(self, use_real_pool: asyncpg.Pool) -> None:
        await db.upsert_and_get_state(_make_job("~01term"))
        await db.set_analysis_and_state("~01term", "Анализ", 9, "delivered")
        inserted, state = await db.upsert_and_get_state(_make_job("~01term"))
        assert inserted is False
        assert state == "delivered"


# --------------------------------------------------------------------------- #
# State transitions — все через _update_job (PIPELINE.md §4)
# --------------------------------------------------------------------------- #
class TestStateTransitions:
    async def test_set_pre_rating_and_state(
        self, real_pool: asyncpg.Pool, use_real_pool: asyncpg.Pool
    ) -> None:
        await db.upsert_and_get_state(_make_job("~01pre"))
        await db.set_pre_rating_and_state("~01pre", 7, "pre_screened")
        row = await real_pool.fetchrow(
            "SELECT pre_rating, processing_state FROM upwork_jobs WHERE upwork_job_id = '~01pre'"
        )
        assert row["pre_rating"] == 7
        assert row["processing_state"] == "pre_screened"

    async def test_set_analysis_and_state(
        self, real_pool: asyncpg.Pool, use_real_pool: asyncpg.Pool
    ) -> None:
        await db.upsert_and_get_state(_make_job("~01an"))
        await db.set_analysis_and_state("~01an", "Анализ\nРЕЙТИНГ: 8", 8, "delivered")
        row = await real_pool.fetchrow(
            "SELECT ai_analysis, rating, processing_state "
            "FROM upwork_jobs WHERE upwork_job_id = '~01an'"
        )
        assert row["rating"] == 8
        assert row["processing_state"] == "delivered"
        assert "РЕЙТИНГ" in row["ai_analysis"]

    async def test_mark_failed_increments_attempts(
        self, real_pool: asyncpg.Pool, use_real_pool: asyncpg.Pool
    ) -> None:
        await db.upsert_and_get_state(_make_job("~01fail"))
        await db.mark_failed("~01fail", "test_error")
        row = await real_pool.fetchrow(
            "SELECT processing_state, attempts, last_error "
            "FROM upwork_jobs WHERE upwork_job_id = '~01fail'"
        )
        assert row["processing_state"] == "failed"
        assert row["attempts"] == 1
        assert row["last_error"] == "test_error"

    async def test_bump_attempts_keeps_state(
        self, real_pool: asyncpg.Pool, use_real_pool: asyncpg.Pool
    ) -> None:
        await db.upsert_and_get_state(_make_job("~01bump"))
        await db.bump_attempts("~01bump", "first_try")
        await db.bump_attempts("~01bump", "second_try")
        row = await real_pool.fetchrow(
            "SELECT processing_state, attempts FROM upwork_jobs WHERE upwork_job_id = '~01bump'"
        )
        assert row["processing_state"] == "pending"  # bump не меняет state
        assert row["attempts"] == 2

    async def test_mark_sent(self, real_pool: asyncpg.Pool, use_real_pool: asyncpg.Pool) -> None:
        await db.upsert_and_get_state(_make_job("~01sent"))
        await db.mark_sent("~01sent")
        row = await real_pool.fetchrow(
            "SELECT is_sent, processing_state FROM upwork_jobs WHERE upwork_job_id = '~01sent'"
        )
        assert row["is_sent"] is True
        assert row["processing_state"] == "delivered"

    async def test_set_favorite_toggle(
        self, real_pool: asyncpg.Pool, use_real_pool: asyncpg.Pool
    ) -> None:
        await db.upsert_and_get_state(_make_job("~01fav"))
        await db.set_favorite("~01fav", True)
        assert (
            await real_pool.fetchval(
                "SELECT is_favorite FROM upwork_jobs WHERE upwork_job_id = '~01fav'"
            )
            is True
        )
        await db.set_favorite("~01fav", False)
        assert (
            await real_pool.fetchval(
                "SELECT is_favorite FROM upwork_jobs WHERE upwork_job_id = '~01fav'"
            )
            is False
        )


# --------------------------------------------------------------------------- #
# Очереди manual / menu (BOT.md §10)
# --------------------------------------------------------------------------- #
class TestQueues:
    async def test_drain_queued_by_reason_full(self, use_real_pool: asyncpg.Pool) -> None:
        for i in range(3):
            await db.upsert_and_get_state(_make_job(f"~01q{i}"))
            await db.set_analysis_state_queued(f"~01q{i}", f"Analysis {i}", 8, "manual")
        rows = await db.drain_queued_by_reason("manual")
        assert len(rows) == 3
        # после drain очередь пуста
        assert await db.drain_queued_by_reason("manual") == []

    async def test_drain_with_limit(self, use_real_pool: asyncpg.Pool) -> None:
        for i in range(5):
            await db.upsert_and_get_state(_make_job(f"~01ql{i}"))
            await db.set_analysis_state_queued(f"~01ql{i}", f"Analysis {i}", 5 + i, "manual")
        rows = await db.drain_queued_by_reason("manual", limit=2)
        assert len(rows) == 2
        # ORDER BY rating DESC — берём с самым высоким рейтингом
        assert all(r["rating"] >= 7 for r in rows)
        # Остальные 3 — всё ещё в очереди
        rest = await db.peek_queued_by_reason("manual")
        assert len(rest) == 3

    async def test_peek_does_not_consume(self, use_real_pool: asyncpg.Pool) -> None:
        await db.upsert_and_get_state(_make_job("~01peek"))
        await db.set_analysis_state_queued("~01peek", "Anal", 9, "manual")
        rows1 = await db.peek_queued_by_reason("manual")
        rows2 = await db.peek_queued_by_reason("manual")
        assert len(rows1) == 1
        assert len(rows2) == 1

    async def test_mark_queued_as_sent_returns_count(self, use_real_pool: asyncpg.Pool) -> None:
        for i in range(4):
            await db.upsert_and_get_state(_make_job(f"~01ms{i}"))
            await db.set_analysis_state_queued(f"~01ms{i}", f"A{i}", 8, "manual")
        n = await db.mark_queued_as_sent("manual")
        assert n == 4
        assert await db.peek_queued_by_reason("manual") == []

    async def test_separate_manual_and_menu_queues(self, use_real_pool: asyncpg.Pool) -> None:
        await db.upsert_and_get_state(_make_job("~01man"))
        await db.set_analysis_state_queued("~01man", "A", 8, "manual")
        await db.upsert_and_get_state(_make_job("~01menu"))
        await db.set_analysis_state_queued("~01menu", "A", 8, "menu")

        manual = await db.drain_queued_by_reason("manual")
        assert len(manual) == 1 and manual[0]["upwork_job_id"] == "~01man"
        # menu не тронули
        menu = await db.peek_queued_by_reason("menu")
        assert len(menu) == 1 and menu[0]["upwork_job_id"] == "~01menu"


# --------------------------------------------------------------------------- #
# bot_settings whitelisted setters (DATABASE.md §2)
# --------------------------------------------------------------------------- #
class TestSettings:
    async def test_set_setting_updates_only_named_field(
        self, real_pool: asyncpg.Pool, use_real_pool: asyncpg.Pool
    ) -> None:
        before = await db.get_settings_full()
        await db.set_setting("pre_screen_threshold", 7)
        after = await db.get_settings_full()
        assert after.pre_screen_threshold == 7
        assert after.analysis_threshold == before.analysis_threshold

    async def test_set_setting_unknown_field_raises(self, use_real_pool: asyncpg.Pool) -> None:
        with pytest.raises(ValueError, match="unknown field"):
            await db.set_setting("evil_field; DROP TABLE upwork_jobs", 1)

    async def test_set_model_validates(self, use_real_pool: asyncpg.Pool) -> None:
        await db.set_model("prescreen_model", "vendor/model-name")
        assert await db.get_model("prescreen_model") == "vendor/model-name"
        with pytest.raises(ValueError):
            await db.set_model("is_paused", "ха-ха")  # is_paused — не модельная колонка

    async def test_get_settings_cached_returns_dataclass(
        self, use_real_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Сбрасываем кэш чтобы прочитать свежие значения
        monkeypatch.setattr(db, "_settings_cache", None, raising=False)
        s = await db.get_settings_cached()
        assert s.is_paused is False
        assert s.hard_min_hires_for_rating == 3


# --------------------------------------------------------------------------- #
# bot_events — для UI Логи (BOT.md §11)
# --------------------------------------------------------------------------- #
class TestEvents:
    async def test_insert_and_filter_by_level(self, use_real_pool: asyncpg.Pool) -> None:
        await db.insert_event(0, "info_event", {"k": "v"})
        await db.insert_event(2, "error_event", {"err": "boom"})

        all_events = await db.fetch_events(db.LogFilter.ALL)
        only_errors = await db.fetch_events(db.LogFilter.ERRORS)

        assert len(all_events) == 2
        assert len(only_errors) == 1
        assert only_errors[0]["event"] == "error_event"

    async def test_count_events(self, use_real_pool: asyncpg.Pool) -> None:
        for i in range(5):
            await db.insert_event(0 if i < 3 else 2, f"e{i}", None)
        assert await db.count_events(db.LogFilter.ALL) == 5
        assert await db.count_events(db.LogFilter.ERRORS) == 2


# --------------------------------------------------------------------------- #
# Prompts + history (DATABASE.md §3, §4)
# --------------------------------------------------------------------------- #
class TestPrompts:
    async def test_bootstrap_inserts_three_default_slots(self, real_pool: asyncpg.Pool) -> None:
        """init_schema bootstrap'ит 3 дефолтных промта (DATABASE.md §3)."""
        rows = await real_pool.fetch("SELECT slot, content FROM ai_prompts")
        slots = {r["slot"] for r in rows}
        assert slots == {"pre_screen", "analysis", "cover"}
        for r in rows:
            assert len(r["content"]) > 0

    async def test_bootstrap_idempotent_does_not_overwrite(self, real_pool: asyncpg.Pool) -> None:
        """Повторный bootstrap не переписывает пользовательский контент."""
        from src import migrations

        custom = "MY CUSTOM PROMPT"
        await real_pool.execute(
            "UPDATE ai_prompts SET content = $1 WHERE slot = 'analysis'", custom
        )
        await migrations.init_schema(real_pool)  # повторно
        result = await real_pool.fetchval("SELECT content FROM ai_prompts WHERE slot = 'analysis'")
        assert result == custom

    async def test_update_with_history(
        self, real_pool: asyncpg.Pool, use_real_pool: asyncpg.Pool
    ) -> None:
        # Bootstrap уже вставил дефолт, теперь обновляем
        old = await db.get_prompt("analysis")
        await db.insert_prompt_history("analysis", old, 12345)
        await db.update_prompt("analysis", "new much longer prompt content " * 5)
        new_val = await db.get_prompt("analysis")
        assert new_val.startswith("new much longer prompt content")
        history = await real_pool.fetch(
            "SELECT content_before, edited_by FROM prompts_history WHERE slot = 'analysis'"
        )
        assert len(history) == 1
        assert history[0]["edited_by"] == 12345
        assert history[0]["content_before"] == old
