"""Тесты db.py — CRUD, идемпотентность, кэшированные геттеры, объединённые UPDATE.

Соответствует DATABASE.md §1-§8 и PIPELINE.md §4.

Все тесты — против моков asyncpg.Pool из conftest. Реальный коннект к БД
не нужен — мы проверяем что db-функции существуют, принимают ожидаемые
параметры и используют пул правильно.
"""

from __future__ import annotations

import pytest
from src import db


class TestInit:
    async def test_init_accepts_pool(self, pool, monkeypatch):
        # init() ставит модуль-глобал; монкеи-патчим, чтобы откатить после теста
        monkeypatch.setattr(db, "_pool", None, raising=False)
        await db.init(pool)
        assert db._pool is pool


class TestTryRegisterRequest:
    async def test_returns_true_on_first_insert(self, pool, monkeypatch):
        monkeypatch.setattr(db, "_pool", pool, raising=False)
        pool.fetchval.return_value = True
        assert await db.try_register_request(b"x" * 32) is True

    async def test_returns_false_on_duplicate(self, pool, monkeypatch):
        monkeypatch.setattr(db, "_pool", pool, raising=False)
        pool.fetchval.return_value = False
        assert await db.try_register_request(b"x" * 32) is False


class TestSaveNormalizeFailure:
    async def test_inserts_raw_payload(self, pool, monkeypatch):
        monkeypatch.setattr(db, "_pool", pool, raising=False)
        await db.save_normalize_failure(b"req-id" * 5 + b"xx", b"{bad json", "ValidationError")
        pool.execute.assert_awaited()


class TestMarkRequestProcessed:
    async def test_updates_processed_at(self, pool, monkeypatch):
        monkeypatch.setattr(db, "_pool", pool, raising=False)
        await db.mark_request_processed(b"x" * 32)
        pool.execute.assert_awaited()


class TestUpsertAndGetState:
    async def test_returns_tuple_inserted_state(self, pool, monkeypatch, job):
        monkeypatch.setattr(db, "_pool", pool, raising=False)
        pool.fetchrow.return_value = {"inserted": True, "processing_state": "pending"}
        inserted, state = await db.upsert_and_get_state(job)
        assert isinstance(inserted, bool)
        assert state in {"pending", "pre_screened", "analyzed", "delivered", "filtered", "failed"}


class TestStateTransitions:
    """Объединённые UPDATE — один SQL вместо двух (PIPELINE.md §4)."""

    async def test_set_pre_rating_and_state_one_call(self, pool, monkeypatch):
        monkeypatch.setattr(db, "_pool", pool, raising=False)
        await db.set_pre_rating_and_state("~01abc", 7, "pre_screened")
        assert pool.execute.await_count == 1

    async def test_set_analysis_and_state_one_call(self, pool, monkeypatch):
        monkeypatch.setattr(db, "_pool", pool, raising=False)
        await db.set_analysis_and_state("~01abc", "анализ", 8, "delivered")
        assert pool.execute.await_count == 1

    async def test_set_analysis_state_queued_with_reason(self, pool, monkeypatch):
        monkeypatch.setattr(db, "_pool", pool, raising=False)
        await db.set_analysis_state_queued("~01abc", "анализ", 8, "manual")
        pool.execute.assert_awaited()

    async def test_delete_job(self, pool, monkeypatch):
        monkeypatch.setattr(db, "_pool", pool, raising=False)
        await db.delete_job("~01abc")
        pool.execute.assert_awaited()

    async def test_mark_failed_writes_last_error(self, pool, monkeypatch):
        monkeypatch.setattr(db, "_pool", pool, raising=False)
        await db.mark_failed("~01abc", "pre_screen_no_response")
        call = pool.execute.call_args
        assert any("pre_screen_no_response" in str(a) for a in call.args)

    async def test_bump_attempts(self, pool, monkeypatch):
        monkeypatch.setattr(db, "_pool", pool, raising=False)
        await db.bump_attempts("~01abc", "analysis_short_or_empty")
        pool.execute.assert_awaited()

    async def test_mark_sent(self, pool, monkeypatch):
        monkeypatch.setattr(db, "_pool", pool, raising=False)
        await db.mark_sent("~01abc")
        pool.execute.assert_awaited()


class TestCachedGetters:
    async def test_get_settings_cached_returns_dataclass(self, pool, monkeypatch):
        monkeypatch.setattr(db, "_pool", pool, raising=False)
        pool.fetchrow.return_value = {
            "is_paused": False,
            "is_paused_menu": False,
            "pre_screen_threshold": 0,
            "analysis_threshold": 0,
            "hard_min_client_spent": 0,
            "hard_min_client_rating": 0,
            "hard_min_hires_for_rating": 3,
            "hard_min_budget_hourly": 0,
            "hard_min_budget_fixed": 0,
            "hard_reject_no_hires": False,
            "hard_max_vacancy_age_h": 0,
            "prescreen_model": "x/y",
            "analysis_model": "a/b",
            "prescreen_fallback_model": "p/q",
            "analysis_fallback_model": "r/s",
            "loud_notification_threshold": 8,
        }
        s1 = await db.get_settings_cached()
        assert hasattr(s1, "pre_screen_threshold")

    async def test_get_settings_cached_uses_cache(self, pool, monkeypatch):
        """Один read на batch — не идём в БД повторно сразу."""
        monkeypatch.setattr(db, "_pool", pool, raising=False)
        pool.fetchrow.return_value = {
            "is_paused": False,
            "is_paused_menu": False,
            "pre_screen_threshold": 0,
            "analysis_threshold": 0,
            "hard_min_client_spent": 0,
            "hard_min_client_rating": 0,
            "hard_min_hires_for_rating": 3,
            "hard_min_budget_hourly": 0,
            "hard_min_budget_fixed": 0,
            "hard_reject_no_hires": False,
            "hard_max_vacancy_age_h": 0,
            "prescreen_model": "x/y",
            "analysis_model": "a/b",
            "prescreen_fallback_model": "p/q",
            "analysis_fallback_model": "r/s",
            "loud_notification_threshold": 8,
        }
        await db.get_settings_cached()
        await db.get_settings_cached()
        assert pool.fetchrow.await_count == 1

    async def test_invalidate_settings_cache_forces_refresh(self, pool, monkeypatch):
        monkeypatch.setattr(db, "_pool", pool, raising=False)
        pool.fetchrow.return_value = {
            "is_paused": False,
            "is_paused_menu": False,
            "pre_screen_threshold": 0,
            "analysis_threshold": 0,
            "hard_min_client_spent": 0,
            "hard_min_client_rating": 0,
            "hard_min_hires_for_rating": 3,
            "hard_min_budget_hourly": 0,
            "hard_min_budget_fixed": 0,
            "hard_reject_no_hires": False,
            "hard_max_vacancy_age_h": 0,
            "prescreen_model": "x/y",
            "analysis_model": "a/b",
            "prescreen_fallback_model": "p/q",
            "analysis_fallback_model": "r/s",
            "loud_notification_threshold": 8,
        }
        await db.get_settings_cached()
        await db.invalidate_settings_cache()
        await db.get_settings_cached()
        assert pool.fetchrow.await_count == 2

    async def test_get_prompt_cached(self, pool, monkeypatch):
        monkeypatch.setattr(db, "_pool", pool, raising=False)
        pool.fetchval.return_value = "TEMPLATE"
        assert await db.get_prompt_cached("pre_screen") == "TEMPLATE"

    async def test_get_openrouter_key_prefers_db(self, pool, monkeypatch):
        monkeypatch.setattr(db, "_pool", pool, raising=False)
        pool.fetchval.return_value = "sk-or-v1-from-db"
        key = await db.get_openrouter_key()
        assert key == "sk-or-v1-from-db"


class TestSecretsAndPrompts:
    async def test_set_secret_writes_updated_by(self, pool, monkeypatch):
        monkeypatch.setattr(db, "_pool", pool, raising=False)
        await db.set_secret("openrouter_api_key", "sk-or-v1-xxx", 701492865)
        pool.execute.assert_awaited()

    async def test_insert_prompt_history_before_update(self, pool, monkeypatch):
        """История пишется ДО UPDATE ai_prompts (DATABASE.md §4)."""
        monkeypatch.setattr(db, "_pool", pool, raising=False)
        await db.insert_prompt_history("analysis", "old text", 701492865)
        pool.execute.assert_awaited()

    async def test_update_prompt(self, pool, monkeypatch):
        monkeypatch.setattr(db, "_pool", pool, raising=False)
        await db.update_prompt("analysis", "new text")
        pool.execute.assert_awaited()


class TestQueues:
    async def test_drain_queued_by_reason_returns_rows(self, pool, monkeypatch):
        monkeypatch.setattr(db, "_pool", pool, raising=False)
        pool.fetch.return_value = [
            {"id": 1, "ai_analysis": "...", "upwork_url": "u", "upwork_job_id": "x"}
        ]
        rows = await db.drain_queued_by_reason("manual")
        assert rows and rows[0]["upwork_job_id"] == "x"

    async def test_drain_with_limit(self, pool, monkeypatch):
        monkeypatch.setattr(db, "_pool", pool, raising=False)
        await db.drain_queued_by_reason("manual", limit=5)
        pool.fetch.assert_awaited()

    async def test_peek_does_not_update(self, pool, monkeypatch):
        monkeypatch.setattr(db, "_pool", pool, raising=False)
        await db.peek_queued_by_reason("manual")
        pool.fetch.assert_awaited()
        # peek не должен делать UPDATE — execute не вызывается
        assert pool.execute.await_count == 0


class TestInsertEvent:
    async def test_inserts_with_level_and_data(self, pool, monkeypatch):
        monkeypatch.setattr(db, "_pool", pool, raising=False)
        await db.insert_event(2, "llm_failed", {"slot": "pre_screen"})
        pool.execute.assert_awaited()


class TestCounters:
    async def test_count_queued_by_reason_cached(self, pool, monkeypatch):
        monkeypatch.setattr(db, "_pool", pool, raising=False)
        pool.fetchval.return_value = 12
        n = await db.count_queued_by_reason_cached(pool, "manual")
        assert n == 12

    async def test_count_favorites_cached(self, pool, monkeypatch):
        monkeypatch.setattr(db, "_pool", pool, raising=False)
        pool.fetchval.return_value = 5
        n = await db.count_favorites_cached(pool)
        assert n == 5


class TestTruncate:
    async def test_truncate_jobs(self, pool, monkeypatch):
        monkeypatch.setattr(db, "_pool", pool, raising=False)
        await db.truncate_jobs()
        pool.execute.assert_awaited()


class TestSqlWhitelist:
    """Защита от SQL-инъекции — динамические колонки только из whitelist."""

    async def test_get_setting_rejects_unknown_field(self, pool, monkeypatch):
        monkeypatch.setattr(db, "_pool", pool, raising=False)
        with pytest.raises(ValueError, match="unknown field"):
            await db.get_setting("'; DROP TABLE upwork_jobs; --")

    async def test_set_setting_rejects_unknown_field(self, pool, monkeypatch):
        monkeypatch.setattr(db, "_pool", pool, raising=False)
        with pytest.raises(ValueError):
            await db.set_setting("malicious_field", 1)

    async def test_get_model_rejects_non_model_column(self, pool, monkeypatch):
        monkeypatch.setattr(db, "_pool", pool, raising=False)
        with pytest.raises(ValueError):
            await db.get_model("is_paused")  # не входит в MODEL_COLUMNS

    async def test_set_setting_accepts_known_field(self, pool, monkeypatch):
        monkeypatch.setattr(db, "_pool", pool, raising=False)
        await db.set_setting("pre_screen_threshold", 5)
        pool.execute.assert_awaited()


class TestNewHelpers:
    """Helpers для UI вместо прямого доступа к pool."""

    async def test_set_favorite_true(self, pool, monkeypatch):
        monkeypatch.setattr(db, "_pool", pool, raising=False)
        await db.set_favorite("~01abc", True)
        pool.execute.assert_awaited()

    async def test_get_analysis_returns_empty_when_missing(self, pool, monkeypatch):
        monkeypatch.setattr(db, "_pool", pool, raising=False)
        pool.fetchval.return_value = None
        assert await db.get_analysis("~01abc") == ""

    async def test_get_card_returns_tuple(self, pool, monkeypatch):
        monkeypatch.setattr(db, "_pool", pool, raising=False)
        pool.fetchrow.return_value = {"job_title": "T", "upwork_url": "U"}
        assert await db.get_card("~01abc") == ("T", "U")


class TestLogFilter:
    async def test_count_events_uses_enum(self, pool, monkeypatch):
        monkeypatch.setattr(db, "_pool", pool, raising=False)
        pool.fetchval.return_value = 7
        n = await db.count_events(db.LogFilter.ERRORS)
        assert n == 7
        sql = pool.fetchval.call_args.args[0]
        assert "level >= 1" in sql

    async def test_fetch_events_default_all(self, pool, monkeypatch):
        monkeypatch.setattr(db, "_pool", pool, raising=False)
        pool.fetch.return_value = []
        await db.fetch_events()
        sql = pool.fetch.call_args.args[0]
        assert "TRUE" in sql
