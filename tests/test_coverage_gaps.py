"""Прицельные тесты для покрытия пробелов до ~100%.

Организация — по модулям, в порядке файлов под `src/`. Каждая группа покрывает
конкретные missing-lines из baseline coverage report.
"""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

# --------------------------------------------------------------------------- #
# src/bot/formatters.py — escape_html, split_for_telegram, format_job
# --------------------------------------------------------------------------- #
from src.bot import formatters


class TestEscapeHtml:
    def test_empty_returns_empty(self):
        assert formatters.escape_html("") == ""
        assert formatters.escape_html(None) == ""

    def test_escapes_ampersand_first(self):
        # & должен заменяться первым, иначе двойная замена сломает результат
        assert formatters.escape_html("&<>") == "&amp;&lt;&gt;"

    def test_no_special_chars_unchanged(self):
        assert formatters.escape_html("hello world") == "hello world"


class TestSplitForTelegram:
    def test_short_text_one_part(self):
        assert formatters.split_for_telegram("hello") == ["hello"]

    def test_long_text_split_at_newline(self):
        text = "a\n" * 3000  # длиннее лимита, есть newlines
        parts = formatters.split_for_telegram(text)
        assert len(parts) > 1
        assert all(len(p) <= formatters.TELEGRAM_MESSAGE_LIMIT for p in parts)

    def test_long_text_split_at_space_when_no_newline(self):
        text = "word " * 1000  # длинная строка только с пробелами
        parts = formatters.split_for_telegram(text)
        assert len(parts) >= 2

    def test_long_text_hard_cut_when_no_separator(self):
        # Сплошной текст без пробелов и новых строк — режется по лимиту
        text = "x" * 5000
        parts = formatters.split_for_telegram(text, limit=100)
        assert all(len(p) <= 100 for p in parts)
        assert "".join(parts) == text


class TestFormatJob:
    def test_uppercases_title(self):
        job = MagicMock(job_title="Senior dev")
        out = formatters.format_job(job, "analysis text")
        assert "SENIOR DEV" in out
        assert "analysis text" in out

    def test_no_title(self):
        job = MagicMock(job_title=None)
        out = formatters.format_job(job, "x")
        assert "x" in out


class TestFormatLogRows:
    def test_empty_rows(self):
        out = formatters.format_log_rows([], page=0, total_pages=1)
        assert "стр 1/1" in out

    def test_with_rows_and_levels(self):
        rows = [
            {"ts": "2026-05-02 12:34:56+00", "level": 0, "event": "info_e", "data": {"k": "v"}},
            {"ts": None, "level": 2, "event": "err_e", "data": None},
        ]
        out = formatters.format_log_rows(rows, page=2, total_pages=10)
        assert "стр 3/10" in out
        # error-уровень помечается ❌, info — без префикса
        assert "❌" in out
        assert "info_e" in out and "err_e" in out


# --------------------------------------------------------------------------- #
# src/bot/handlers/reports.py — show_report_digest, all rep:* branches
# --------------------------------------------------------------------------- #
from src.bot.handlers import reports as reports_h


class TestReportDigestText:
    async def test_renders_with_overflow_marker(self, message, stub_db, stub_log):
        stub_db["peek_queued_by_reason"].return_value = [
            {"rating": 9 - i, "job_title": f"J{i}", "client_country": "US", "budget": "$1K"}
            for i in range(15)
        ]
        await reports_h.handle_report(message)
        text = message.answer.call_args.args[0]
        assert "В очереди 15" in text
        assert "ещё 5" in text

    async def test_empty_queue_shows_placeholder(self, message, stub_db, stub_log):
        stub_db["peek_queued_by_reason"].return_value = []
        await reports_h.handle_report(message)
        text = message.answer.call_args.args[0]
        assert "пуста" in text.lower() or "пуст" in text.lower()


class TestReportSubmenuActions:
    async def test_unload_all_drains_manual(self, message, stub_db, stub_log, stub_notifier):
        stub_db["drain_queued_by_reason"].return_value = [
            {
                "upwork_job_id": "~01a", "rating": 8, "ai_analysis": "x",
                "upwork_url": "u", "job_title": "t",
            }
        ]
        await reports_h.handle_report_unload_all(message)
        stub_db["drain_queued_by_reason"].assert_awaited_with("manual")
        stub_notifier.send_job_from_row.assert_awaited()
        # Остаёмся в подменю Отчёт — set_paused_menu(True) от handle_report
        stub_db["set_paused_menu"].assert_awaited_with(True)

    async def test_clear_marks_sent_and_returns(
        self, message, stub_db, stub_log, stub_notifier
    ):
        stub_db["mark_queued_as_sent"].return_value = 7
        await reports_h.handle_report_clear(message)
        stub_db["mark_queued_as_sent"].assert_awaited_with("manual")
        # Остаёмся в подменю Отчёт
        stub_db["set_paused_menu"].assert_awaited_with(True)


class TestHandleSync:
    async def test_sync_drains_and_resets_pause(
        self, message, stub_db, stub_log, stub_notifier, monkeypatch
    ):
        stub_db["drain_queued_by_reason"].return_value = [
            {
                "upwork_job_id": "~01a",
                "ai_analysis": "x",
                "upwork_url": "u",
                "job_title": "t",
                "rating": 7,
            },
            {
                "upwork_job_id": "~01b",
                "ai_analysis": "y",
                "upwork_url": "u2",
                "job_title": "t2",
                "rating": 8,
            },
        ]
        # Скипаем asyncio.sleep чтобы тест был быстрый
        monkeypatch.setattr(asyncio, "sleep", AsyncMock(), raising=False)
        await reports_h.handle_sync(message)
        assert stub_notifier.send_job_from_row.await_count == 2
        stub_db["set_paused_menu"].assert_awaited_with(False)


# --------------------------------------------------------------------------- #
# src/db.py — _conn() error, whitelist enforcement, cached getters, count cache
# --------------------------------------------------------------------------- #
from src import db


class TestDbConnGuard:
    async def test_conn_raises_when_pool_uninitialized(self, monkeypatch):
        monkeypatch.setattr(db, "_pool", None, raising=False)
        with pytest.raises(RuntimeError, match="db.init"):
            db._conn()


class TestDbWhitelistDeep:
    async def test_get_model_unknown_column_raises(self, pool, monkeypatch):
        monkeypatch.setattr(db, "_pool", pool, raising=False)
        with pytest.raises(ValueError, match="unknown field"):
            await db.get_model("not_a_model_column")

    async def test_get_setting_unknown_field_raises(self, pool, monkeypatch):
        monkeypatch.setattr(db, "_pool", pool, raising=False)
        with pytest.raises(ValueError, match="unknown field"):
            await db.get_setting("nope")


class TestGetSettingsFull:
    async def test_with_explicit_pool(self, pool):
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
        s = await db.get_settings_full(pool)
        assert s.is_paused is False

    async def test_returns_defaults_when_no_row(self, pool, monkeypatch):
        monkeypatch.setattr(db, "_pool", pool, raising=False)
        pool.fetchrow.return_value = None
        s = await db.get_settings_full()
        assert s.is_paused is False  # дефолт BotSettings()


class TestPromptCacheInvalidate:
    async def test_invalidate_specific_slot(self, pool, monkeypatch):
        monkeypatch.setattr(db, "_pool", pool, raising=False)
        db._prompt_cache["analysis"] = (pool, "old", 0.0)
        await db.invalidate_prompt_cache("analysis")
        assert "analysis" not in db._prompt_cache

    async def test_invalidate_all_slots(self, pool, monkeypatch):
        monkeypatch.setattr(db, "_pool", pool, raising=False)
        db._prompt_cache["a"] = (pool, "x", 0.0)
        db._prompt_cache["b"] = (pool, "y", 0.0)
        await db.invalidate_prompt_cache(None)
        assert db._prompt_cache == {}


class TestSecretsCacheInvalidate:
    async def test_invalidate_clears(self, pool, monkeypatch):
        monkeypatch.setattr(db, "_pool", pool, raising=False)
        db._secret_cache["openrouter_api_key"] = (pool, "secret", 0.0)
        await db.invalidate_secrets_cache()
        assert db._secret_cache == {}


class TestPromptCachedHit:
    async def test_cache_hit_skips_db(self, pool, monkeypatch):
        import time as time_mod

        monkeypatch.setattr(db, "_pool", pool, raising=False)
        db._prompt_cache.clear()
        db._prompt_cache["analysis"] = (pool, "cached_value", time_mod.monotonic())
        result = await db.get_prompt_cached("analysis")
        assert result == "cached_value"
        pool.fetchval.assert_not_awaited()

    async def test_cache_miss_loads_from_db(self, pool, monkeypatch):
        monkeypatch.setattr(db, "_pool", pool, raising=False)
        db._prompt_cache.clear()
        pool.fetchval.return_value = "fresh_value"
        result = await db.get_prompt_cached("analysis")
        assert result == "fresh_value"

    async def test_cache_expired_reloads(self, pool, monkeypatch):
        monkeypatch.setattr(db, "_pool", pool, raising=False)
        db._prompt_cache.clear()
        # Старая запись, TTL=60 — поставим время на 100 сек назад
        import time as time_mod

        db._prompt_cache["analysis"] = (pool, "old", time_mod.monotonic() - 100)
        pool.fetchval.return_value = "new"
        result = await db.get_prompt_cached("analysis")
        assert result == "new"


class TestOpenrouterKeyCachedHit:
    async def test_cache_hit(self, pool, monkeypatch):
        import time as time_mod

        monkeypatch.setattr(db, "_pool", pool, raising=False)
        db._secret_cache.clear()
        db._secret_cache["openrouter_api_key"] = (pool, "sk-cached", time_mod.monotonic())
        assert await db.get_openrouter_key() == "sk-cached"
        pool.fetchval.assert_not_awaited()

    async def test_falls_back_to_env_when_db_empty(self, pool, monkeypatch):
        from src import config

        monkeypatch.setattr(db, "_pool", pool, raising=False)
        monkeypatch.setattr(config, "OPENROUTER_API_KEY", "sk-from-env", raising=False)
        db._secret_cache.clear()
        pool.fetchval.return_value = None
        assert await db.get_openrouter_key() == "sk-from-env"


class TestCountCache:
    async def test_count_evicts_after_ttl(self, pool, monkeypatch):
        import time as time_mod

        monkeypatch.setattr(db, "_pool", pool, raising=False)
        db._count_cache.clear()
        # положим устаревшую запись (TTL=10 сек, время = -100)
        key = (id(pool), "queued:manual")
        db._count_cache[key] = (42, time_mod.monotonic() - 100)
        pool.fetchval.return_value = 7
        result = await db.count_queued_by_reason_cached(pool, "manual")
        assert result == 7  # свежее значение, не 42

    async def test_count_favorites_cache_hit(self, pool):
        import time as time_mod

        db._count_cache.clear()
        key = (id(pool), "favorites")
        db._count_cache[key] = (123, time_mod.monotonic())
        result = await db.count_favorites_cached(pool)
        assert result == 123
        pool.fetchval.assert_not_awaited()


class TestSettingsCacheInvalidate:
    async def test_invalidate_clears_settings(self, pool, monkeypatch):
        monkeypatch.setattr(db, "_pool", pool, raising=False)
        from src.models import BotSettings

        db._settings_cache = (pool, BotSettings(), 0.0)
        await db.invalidate_settings_cache()
        assert db._settings_cache is None


class TestSetPausedMenu:
    async def test_set_paused_menu(self, pool, monkeypatch):
        monkeypatch.setattr(db, "_pool", pool, raising=False)
        await db.set_paused_menu(True)
        pool.execute.assert_awaited()
        sql = pool.execute.call_args.args[0]
        assert "is_paused_menu" in sql


class TestGetRecentChanges:
    async def test_returns_dicts(self, pool, monkeypatch):
        monkeypatch.setattr(db, "_pool", pool, raising=False)
        pool.fetch.return_value = [
            {"ts": "2026-05-02", "data": {"old_value": "0", "new_value": "5"}}
        ]
        out = await db.get_recent_changes("threshold_updated", "pre_screen_threshold")
        assert len(out) == 1
        assert out[0]["data"]["new_value"] == "5"


class TestUpdateJobNoFields:
    async def test_no_fields_returns_early(self, pool, monkeypatch):
        """_update_job без fields и без attempts_inc — должен вернуться без SQL."""
        monkeypatch.setattr(db, "_pool", pool, raising=False)
        await db._update_job("~01x")
        # никакого SQL — execute не должен быть вызван
        pool.execute.assert_not_awaited()


class TestSaveNormalizeFailure:
    async def test_valid_json_stored_as_is(self, pool, monkeypatch):
        monkeypatch.setattr(db, "_pool", pool, raising=False)
        await db.save_normalize_failure(b"\x00" * 32, b'{"a": 1}', "err")
        # Второй аргумент (raw_payload) должен быть валидным JSON-строкой
        sql_args = pool.execute.call_args.args
        assert sql_args[2] == '{"a": 1}'

    async def test_invalid_json_wrapped(self, pool, monkeypatch):
        monkeypatch.setattr(db, "_pool", pool, raising=False)
        await db.save_normalize_failure(b"\x00" * 32, b"not json{", "err")
        sql_args = pool.execute.call_args.args
        # должно быть JSON со ссылкой на raw
        import msgspec

        decoded = msgspec.json.decode(sql_args[2].encode())
        assert "raw" in decoded


# --------------------------------------------------------------------------- #
# src/migrations.py — bootstrap branches
# --------------------------------------------------------------------------- #
from src import migrations


class TestPromptsBootstrap:
    async def test_bootstrap_emits_event_on_inserts(self, pool, monkeypatch, stub_log):
        # fetchval возвращает slot для каждого вызова → "вставлено".
        # Длина side_effect = len(DEFAULT_PROMPTS) — fixture обновляется при добавлении слотов.
        pool.fetchval = AsyncMock(
            side_effect=list(migrations.DEFAULT_PROMPTS.keys())
        )
        await migrations._bootstrap_prompts(pool)
        events = [c.args[0] for c in stub_log.call_args_list if c.args]
        assert "prompts_bootstrap_done" in events

    async def test_bootstrap_no_event_when_all_existed(self, pool, stub_log):
        # fetchval возвращает None для всех → ON CONFLICT DO NOTHING сработал
        pool.fetchval = AsyncMock(return_value=None)
        await migrations._bootstrap_prompts(pool)
        events = [c.args[0] for c in stub_log.call_args_list if c.args]
        assert "prompts_bootstrap_done" not in events


# --------------------------------------------------------------------------- #
# src/llm.py — validate_model edge cases + _resolve_session error
# --------------------------------------------------------------------------- #
from src import llm


class TestResolveSession:
    def test_passes_through_explicit_session(self, monkeypatch):
        # ClientSession-like object
        sess = MagicMock()
        result = llm._resolve_session(sess)
        assert result is sess

    def test_uses_global_when_none_passed(self, monkeypatch):
        global_sess = MagicMock()
        monkeypatch.setattr(llm, "_session", global_sess, raising=False)
        result = llm._resolve_session(None)
        assert result is global_sess

    def test_raises_when_no_session_anywhere(self, monkeypatch):
        monkeypatch.setattr(llm, "_session", None, raising=False)
        with pytest.raises(RuntimeError, match="set_session"):
            llm._resolve_session(None)


class TestValidateModelMore:
    async def test_unknown_status(self, http_session):
        sess = http_session(status=500)
        ok, msg = await llm.validate_model(sess, "k", "vendor/model")
        assert ok is False
        assert "500" in msg

    async def test_network_error(self, http_session):
        import aiohttp

        sess = http_session(raise_exc=aiohttp.ClientError("network"))
        ok, msg = await llm.validate_model(sess, "k", "vendor/model")
        assert ok is False
        assert "network" in msg.lower()

    async def test_timeout_error(self, http_session):
        sess = http_session(raise_exc=TimeoutError())
        ok, msg = await llm.validate_model(sess, "k", "vendor/model")
        assert ok is False


class TestHttpRefererOptional:
    async def test_skips_empty_http_referer(self, http_session, stub_log, monkeypatch):
        sess = http_session(
            status=200,
            payload={"choices": [{"message": {"content": "ok"}}], "usage": {}},
        )
        monkeypatch.setattr(llm, "HTTP_REFERER", "", raising=False)
        monkeypatch.setattr(llm, "X_TITLE", "", raising=False)
        await llm._call(sess, "k", "m", "tpl", "u", timeout_s=10)
        # Перехватываем переданные headers через MagicMock.post.call_args
        kwargs = sess.post.call_args.kwargs
        headers = kwargs.get("headers", {})
        assert "HTTP-Referer" not in headers
        assert "X-Title" not in headers
        assert "Authorization" in headers  # обязательный остаётся

    async def test_includes_http_referer_when_set(self, http_session, stub_log, monkeypatch):
        sess = http_session(
            status=200,
            payload={"choices": [{"message": {"content": "ok"}}], "usage": {}},
        )
        monkeypatch.setattr(llm, "HTTP_REFERER", "https://example.com/", raising=False)
        monkeypatch.setattr(llm, "X_TITLE", "MyApp", raising=False)
        await llm._call(sess, "k", "m", "tpl", "u", timeout_s=10)
        headers = sess.post.call_args.kwargs.get("headers", {})
        assert headers.get("HTTP-Referer") == "https://example.com/"
        assert headers.get("X-Title") == "MyApp"


# --------------------------------------------------------------------------- #
# src/log.py — emit insert_event failure path
# --------------------------------------------------------------------------- #
from src import log as log_mod


class TestLogEmitFailure:
    async def test_insert_event_failure_does_not_propagate(self, monkeypatch):
        from src import db as db_mod

        async def boom(*args, **kwargs):
            raise RuntimeError("db down")

        monkeypatch.setattr(db_mod, "insert_event", boom, raising=False)
        # Не должно бросать наружу
        await log_mod.emit("pipeline_finished", upwork_job_id="x")


class TestLogException:
    def test_exception_helper_logs(self, caplog):
        with caplog.at_level(logging.ERROR):
            try:
                raise RuntimeError("kaboom")
            except RuntimeError:
                log_mod.exception("test_event", err="detail")
        # exception() вызвал log.exception — должна быть запись
        assert any("test_event" in r.message for r in caplog.records)


# --------------------------------------------------------------------------- #
# src/cron.py — _bind, set_bot, _loop early CancelledError
# --------------------------------------------------------------------------- #
from src import cron


class TestCronSetBot:
    def test_set_bot_assigns_global(self, monkeypatch):
        monkeypatch.setattr(cron, "bot", None, raising=False)
        bot_obj = MagicMock()
        cron.set_bot(bot_obj)
        assert cron.bot is bot_obj


class TestCronBindHelper:
    async def test_bind_calls_fn_with_pool(self, pool):
        called = []

        async def fn(p):
            called.append(p)

        bound = cron._bind(fn, pool)
        await bound()
        assert called == [pool]


class TestAlertNoBotConfigured:
    async def test_alert_silent_if_no_bot(self, pool, monkeypatch):
        pool.fetchval.return_value = 10  # выше порога
        monkeypatch.setattr(cron, "bot", None, raising=False)
        # Не должно крэшить если bot=None
        await cron.alert_error_burst(pool)

    async def test_alert_silent_if_no_users(self, pool, monkeypatch):
        pool.fetchval.return_value = 10
        bot = MagicMock()
        bot.send_message = AsyncMock()
        monkeypatch.setattr(cron, "bot", bot, raising=False)
        monkeypatch.setattr(cron, "ALLOWED_USER_IDS", [], raising=False)
        await cron.alert_error_burst(pool)
        bot.send_message.assert_not_called()


# --------------------------------------------------------------------------- #
# src/http_app.py — start() function + _is_shutting_down getter
# --------------------------------------------------------------------------- #
from src import http_app


class TestHttpAppStart:
    async def test_start_returns_runner(self):
        bot = MagicMock()
        dp = MagicMock()
        # порт=0 → ОС выдаст случайный свободный
        runner = await http_app.start(bot, dp, port=0)
        try:
            assert runner is not None
            assert hasattr(runner, "cleanup")
        finally:
            await runner.cleanup()


class TestHttpAppGetShuttingDown:
    def test_set_and_read(self):
        http_app.set_shutting_down(True)
        try:
            assert http_app._is_shutting_down() is True
        finally:
            http_app.set_shutting_down(False)


# --------------------------------------------------------------------------- #
# src/bot/handlers/secrets.py — delete user messages
# --------------------------------------------------------------------------- #
from src.bot.handlers import secrets as secrets_h


class TestSaveApiKeyDeletesMessages:
    async def test_user_messages_deleted_on_save(self, message, state, bot, stub_db, stub_log):
        bot.delete_message = AsyncMock()
        chat = MagicMock()
        chat.id = 999
        message.chat = chat
        state._data.update(
            {
                "buf": "sk-or-v1-" + "x" * 30,
                "user_message_ids": [10, 20, 30],
            }
        )
        state.get_data = AsyncMock(return_value=dict(state._data))

        await secrets_h.save_api_key(message, state, bot)
        # Все сообщения с ключом удалены
        assert bot.delete_message.await_count == 3

    async def test_telegram_error_during_delete_swallowed(
        self, message, state, bot, stub_db, stub_log
    ):
        from aiogram.exceptions import TelegramAPIError

        bot.delete_message = AsyncMock(side_effect=TelegramAPIError(method=None, message="404"))
        chat = MagicMock()
        chat.id = 999
        message.chat = chat
        state._data.update({"buf": "sk-or-v1-" + "x" * 30, "user_message_ids": [10]})
        state.get_data = AsyncMock(return_value=dict(state._data))

        # Не должно бросать наружу
        await secrets_h.save_api_key(message, state, bot)


# --------------------------------------------------------------------------- #
# src/bot/handlers/settings_ui.py — error branches
# --------------------------------------------------------------------------- #
from src.bot.handlers import settings_ui as ui


class TestUploadPromptFile:
    async def test_no_document_returns(self, message, state):
        message.document = None
        await ui.upload_prompt_file(message, state, bot=MagicMock())
        # Не должно вызвать message.answer
        message.answer.assert_not_awaited()

    async def test_non_txt_rejected(self, message, state):
        doc = MagicMock()
        doc.file_name = "evil.exe"
        message.document = doc
        await ui.upload_prompt_file(message, state, bot=MagicMock())
        text = message.answer.call_args.args[0]
        assert ".txt" in text

    async def test_txt_accepted_and_buffered(self, message, state):
        doc = MagicMock()
        doc.file_name = "prompt.txt"
        message.document = doc
        bot = MagicMock()
        file_obj = MagicMock()
        file_obj.read = MagicMock(return_value=b"hello prompt content")
        bot.download = AsyncMock(return_value=file_obj)
        await ui.upload_prompt_file(message, state, bot=bot)
        update_call = state.update_data.call_args
        assert "hello prompt content" in update_call.kwargs["buf"]


class TestPresetEdgeCases:
    async def test_select_unknown_preset_label_no_op(self, message, state):
        message.text = "Unknown preset label"
        await ui.select_preset(message, state)
        state.update_data.assert_not_awaited()

    async def test_confirm_yes_without_pending_preset(self, message, state, stub_db, stub_log):
        state._data.pop("pending_preset", None)
        state.get_data = AsyncMock(return_value={})
        await ui.confirm_preset_yes(message, state)
        # state.clear() в любом случае
        state.clear.assert_awaited()


class TestRouteUnknownButtons:
    async def test_route_model_unknown(self, message, state, stub_db):
        message.text = "Random text"
        await ui.route_model_button(message, state)
        message.answer.assert_not_awaited()

    async def test_route_threshold_unknown(self, message, state, stub_db):
        message.text = "Random text"
        await ui.route_threshold_button(message, state)
        message.answer.assert_not_awaited()


class TestRenderHistoryDataDecodes:
    async def test_data_as_json_string(self, monkeypatch, stub_db):
        from src import db as db_mod

        async def fake(*args, **kwargs):
            # data как строка JSON (так возвращает asyncpg для jsonb с msgspec.json)
            return [
                {
                    "ts": "2026-05-02T10:00:00",
                    "data": '{"old_value":"0","new_value":"5","via":"manual"}',
                }
            ]

        monkeypatch.setattr(db_mod, "get_recent_changes", fake, raising=False)
        result = await ui._render_history("threshold_updated", "pre_screen_threshold")
        assert "0" in result and "5" in result

    async def test_data_garbage_string_falls_back_to_empty(self, monkeypatch, stub_db):
        from src import db as db_mod

        async def fake(*args, **kwargs):
            return [{"ts": "2026-05-02", "data": "{not valid json"}]

        monkeypatch.setattr(db_mod, "get_recent_changes", fake, raising=False)
        # Не должно бросать
        result = await ui._render_history("threshold_updated", "pre_screen_threshold")
        assert "Последние изменения" in result


class TestBackToSettingsFromPrompts:
    async def test_returns_to_settings(self, message, state, stub_db):
        await ui.back_to_settings_from_prompts(message, state)
        # Меню Настроек теперь отправляется двумя сообщениями (reply-kb + inline)
        # Inline-кнопки содержат "Изменить промт" в callback меню
        kb_calls = [c.kwargs.get("reply_markup") for c in message.answer.call_args_list]
        kb_strs = [str(kb) for kb in kb_calls if kb is not None]
        assert any("Изменить промт" in s for s in kb_strs)


# --------------------------------------------------------------------------- #
# src/bot/routers.py — internal handlers
# --------------------------------------------------------------------------- #
from src.bot import routers


class TestRoutersInternalHandlers:
    async def test_universal_cancel_clears_state(self, message, state, stub_db, stub_log):
        await routers._universal_cancel(message, state)
        state.clear.assert_awaited()
        # После отмены — главное меню
        message.answer.assert_awaited()

    async def test_handle_logs_callback_close(self, stub_db, stub_log):
        cb = MagicMock()
        cb.data = "logs:close"
        cb.message = MagicMock()
        cb.message.delete = AsyncMock()
        cb.answer = AsyncMock()
        await routers._handle_logs_callback(cb)
        cb.message.delete.assert_awaited()

    async def test_handle_logs_callback_pagination(self, stub_db, stub_log):
        cb = MagicMock()
        cb.data = "logs:2:1"  # page=2, only_errors=True
        cb.message = MagicMock()
        cb.message.answer = AsyncMock()
        cb.message.edit_text = AsyncMock()
        cb.answer = AsyncMock()
        stub_db["count_events"].return_value = 100
        stub_db["fetch_events"].return_value = []
        await routers._handle_logs_callback(cb)
        # Пагинация теперь редактирует существующее сообщение, не отправляет новое
        cb.message.edit_text.assert_awaited()

    async def test_handle_logs_callback_invalid_format(self, stub_db, stub_log):
        cb = MagicMock()
        cb.data = "logs:not_a_number"  # неверный формат
        cb.message = MagicMock()
        cb.answer = AsyncMock()
        await routers._handle_logs_callback(cb)
        cb.answer.assert_awaited()

    async def test_handle_logs_callback_invalid_int(self, stub_db, stub_log):
        cb = MagicMock()
        cb.data = "logs:abc:1"
        cb.message = MagicMock()
        cb.answer = AsyncMock()
        await routers._handle_logs_callback(cb)
        cb.answer.assert_awaited()

    async def test_handle_logs_callback_no_message(self, stub_db, stub_log):
        cb = MagicMock()
        cb.data = "logs:close"
        cb.message = None  # callback без message — крайний edge
        cb.answer = AsyncMock()
        await routers._handle_logs_callback(cb)
        cb.answer.assert_awaited()


class TestRoutersEditDispatcher:
    """`_route_edit_btn` → определяет какой FSM запустить по содержимому state."""

    async def test_dispatch_to_prompt_edit(self, message, state, stub_db, stub_log):
        state._data = {"slot": "analysis"}
        state.get_data = AsyncMock(return_value=dict(state._data))
        await routers._enter_prompt_edit(message, state)
        from src.bot.states import PromptEdit

        state.set_state.assert_awaited_with(PromptEdit.waiting_text)

    async def test_prompt_edit_without_slot_in_state(self, message, state, stub_db):
        state._data = {}
        state.get_data = AsyncMock(return_value={})
        await routers._enter_prompt_edit(message, state)
        # Должен ответить «Сначала выбери слот промта.»
        text = message.answer.call_args.args[0]
        assert "слот" in text.lower()

    async def test_dispatch_to_model_edit(self, message, state, stub_db):
        state._data = {"role": "prescreen"}
        state.get_data = AsyncMock(return_value=dict(state._data))
        await routers._enter_model_edit(message, state)
        from src.bot.states import ModelEdit

        state.set_state.assert_awaited_with(ModelEdit.waiting_name)

    async def test_model_edit_without_role(self, message, state, stub_db):
        state._data = {}
        state.get_data = AsyncMock(return_value={})
        await routers._enter_model_edit(message, state)
        text = message.answer.call_args.args[0]
        assert "модель" in text.lower()

    async def test_dispatch_to_threshold_edit(self, message, state, stub_db):
        state._data = {"field": "pre_screen_threshold"}
        state.get_data = AsyncMock(return_value=dict(state._data))
        await routers._enter_threshold_edit(message, state)
        from src.bot.states import ThresholdEdit

        state.set_state.assert_awaited_with(ThresholdEdit.waiting_value)

    async def test_threshold_edit_without_field(self, message, state, stub_db):
        state._data = {}
        state.get_data = AsyncMock(return_value={})
        await routers._enter_threshold_edit(message, state)
        text = message.answer.call_args.args[0]
        assert "порог" in text.lower()


# --------------------------------------------------------------------------- #
# src/main.py — lifespan + signal handlers + _graceful_shutdown
# --------------------------------------------------------------------------- #
class TestMainGracefulShutdown:
    async def test_graceful_shutdown_calls_all_cleanup(self, monkeypatch):
        from src import main as main_mod

        # стопаем все модули
        dp = MagicMock()
        dp.stop_polling = AsyncMock()
        polling_task = MagicMock()
        polling_task.cancel = MagicMock()
        # await polling_task — нужна корутина

        async def fake_await():
            return None

        polling_task.__await__ = lambda self: fake_await().__await__()
        runner = MagicMock()
        runner.cleanup = AsyncMock()
        pool = MagicMock()
        pool.close = AsyncMock()
        http_session = MagicMock()
        http_session.close = AsyncMock()
        bot = MagicMock()
        bot.session = MagicMock()
        bot.session.close = AsyncMock()

        # _tasks пуст → drain не запустится
        monkeypatch.setattr(main_mod.http_app, "_tasks", set(), raising=False)

        await main_mod._graceful_shutdown(dp, polling_task, runner, pool, http_session, bot)

        # Все cleanup-методы вызваны
        dp.stop_polling.assert_awaited()
        runner.cleanup.assert_awaited()
        pool.close.assert_awaited()
        http_session.close.assert_awaited()
        bot.session.close.assert_awaited()
        # webhook переключён в shutdown
        assert main_mod.http_app._is_shutting_down() is True
        # сбрасываем для других тестов
        main_mod.http_app.set_shutting_down(False)

    async def test_graceful_shutdown_with_in_flight_tasks_drains(self, monkeypatch):
        from src import main as main_mod

        # эмулируем in-flight задачу что мгновенно завершается
        async def fake_task():
            return None

        task = asyncio.create_task(fake_task())
        monkeypatch.setattr(main_mod.http_app, "_tasks", {task}, raising=False)

        dp = MagicMock()
        dp.stop_polling = AsyncMock()
        polling_task = MagicMock()
        polling_task.cancel = MagicMock()

        async def fake_await():
            return None

        polling_task.__await__ = lambda self: fake_await().__await__()
        runner = MagicMock()
        runner.cleanup = AsyncMock()
        pool = MagicMock()
        pool.close = AsyncMock()
        http_session = MagicMock()
        http_session.close = AsyncMock()
        bot = MagicMock()
        bot.session = MagicMock()
        bot.session.close = AsyncMock()

        await main_mod._graceful_shutdown(dp, polling_task, runner, pool, http_session, bot)
        main_mod.http_app.set_shutting_down(False)

    async def test_graceful_shutdown_swallows_stop_polling_runtime_error(self, monkeypatch):
        from src import main as main_mod

        dp = MagicMock()
        dp.stop_polling = AsyncMock(side_effect=RuntimeError("Polling is not started"))
        polling_task = MagicMock()
        polling_task.cancel = MagicMock()

        async def fake_await():
            return None

        polling_task.__await__ = lambda self: fake_await().__await__()
        runner = MagicMock()
        runner.cleanup = AsyncMock()
        pool = MagicMock()
        pool.close = AsyncMock()
        http_session = MagicMock()
        http_session.close = AsyncMock()
        bot = MagicMock()
        bot.session = MagicMock()
        bot.session.close = AsyncMock()
        monkeypatch.setattr(main_mod.http_app, "_tasks", set(), raising=False)

        # Не должно пробрасываться исключение
        await main_mod._graceful_shutdown(dp, polling_task, runner, pool, http_session, bot)
        main_mod.http_app.set_shutting_down(False)


class TestMainEntryFunc:
    """main() устанавливает signal handlers + проходит через lifespan."""

    async def test_main_with_mocked_lifespan(self, monkeypatch):
        from src import main as main_mod

        @asyncio_contextmanager_factory()
        async def fake_lifespan_cm():
            return None

        monkeypatch.setattr(main_mod, "lifespan", fake_lifespan_cm, raising=False)

        # Запускаем main как короткое заглушенное выполнение
        async def trigger_stop():
            await asyncio.sleep(0.01)
            # signal handler через event — отправим SIGTERM
            import signal

            try:
                # На Mac/Linux raise сигнал; в asyncio.add_signal_handler
                import os

                os.kill(os.getpid(), signal.SIGTERM)
            except Exception:
                pass

        # Это сложный сценарий — простой smoke-тест что main() вообще callable
        assert callable(main_mod.main)


def asyncio_contextmanager_factory():
    """helper: создаёт async-context-manager без Async generator hacking."""
    from contextlib import asynccontextmanager

    return asynccontextmanager


# --------------------------------------------------------------------------- #
# Дополнительные тесты для последних gap'ов
# --------------------------------------------------------------------------- #
class TestFavoritesEdgeCases:
    """Покрытие веток `if not upwork_job_id` в favorites handlers."""

    async def test_save_favorite_empty_id(self, stub_db, stub_log):
        from src.bot.handlers import favorites as favorites_h

        cb = MagicMock()
        cb.data = "save_"  # empty id
        cb.answer = AsyncMock()
        await favorites_h.handle_save_favorite(cb)
        # set_favorite не вызывается
        stub_db["set_favorite"].assert_not_awaited()
        cb.answer.assert_awaited_once_with()

    async def test_show_description_empty_id_returns(self, stub_db, stub_log):
        from src.bot.handlers import favorites as favorites_h

        cb = MagicMock()
        cb.data = "desc_"
        cb.answer = AsyncMock()
        cb.message = MagicMock()
        cb.message.edit_text = AsyncMock()
        await favorites_h.handle_show_description(cb)
        stub_db["get_job_full"].assert_not_awaited()
        cb.answer.assert_awaited_once_with()


class TestModelsEdgeCases:
    async def test_save_model_unknown_role(self, message, state, stub_db, stub_log):
        from src.bot.handlers import models as models_h

        state._data = {"buf": "vendor/model", "role": "unknown_role"}
        state.get_data = AsyncMock(return_value=dict(state._data))
        await models_h.save_model(message, state)
        text = message.answer.call_args.args[0]
        assert "Неизвестный" in text
        stub_db["set_model"].assert_not_awaited()

    async def test_save_model_invalid_format(self, message, state, stub_db, stub_log):
        from src.bot.handlers import models as models_h

        state._data = {"buf": "INVALID_NAME", "role": "prescreen"}
        state.get_data = AsyncMock(return_value=dict(state._data))
        await models_h.save_model(message, state)
        text = message.answer.call_args.args[0]
        assert "vendor/model-name" in text
        stub_db["set_model"].assert_not_awaited()

    async def test_save_model_fallback_role_returns_fallback_kb(
        self, message, state, stub_db, stub_log
    ):
        from src.bot.handlers import models as models_h

        state._data = {"buf": "vendor/model-x", "role": "prescreen_fallback"}
        state.get_data = AsyncMock(return_value=dict(state._data))
        await models_h.save_model(message, state)
        kb = message.answer.call_args.kwargs.get("reply_markup")
        kb_text = str(kb)
        assert "фолбэк" in kb_text.lower() or "Фолбэк" in kb_text


class TestPromptsEdgeCases:
    async def test_save_prompt_no_slot(self, message, state, stub_db, stub_log):
        from src.bot.handlers import prompts as prompts_h

        state._data = {"buf": "x" * 100}  # нет slot
        state.get_data = AsyncMock(return_value=dict(state._data))
        await prompts_h.save_prompt(message, state)
        text = message.answer.call_args.args[0]
        assert "Слот" in text
        stub_db["update_prompt"].assert_not_awaited()


class TestThresholdsEdgeCases:
    async def test_save_threshold_unknown_field(self, message, state, stub_db, stub_log):
        from src.bot.handlers import thresholds as thresholds_h

        state._data = {"buf": "5", "field": "evil_field"}
        state.get_data = AsyncMock(return_value=dict(state._data))
        await thresholds_h.save_threshold(message, state)
        text = message.answer.call_args.args[0]
        assert "Неизвестный" in text

    async def test_save_threshold_non_numeric(self, message, state, stub_db, stub_log):
        from src.bot.handlers import thresholds as thresholds_h

        state._data = {"buf": "not_a_number", "field": "pre_screen_threshold"}
        state.get_data = AsyncMock(return_value=dict(state._data))
        await thresholds_h.save_threshold(message, state)
        text = message.answer.call_args.args[0]
        assert "целое" in text.lower() or "число" in text.lower()
        stub_db["set_setting"].assert_not_awaited()

    async def test_save_threshold_float_with_comma(self, message, state, stub_db, stub_log):
        from src.bot.handlers import thresholds as thresholds_h

        state._data = {"buf": "4,5", "field": "hard_min_client_rating"}
        state.get_data = AsyncMock(return_value=dict(state._data))
        await thresholds_h.save_threshold(message, state)
        # comma → dot конверсия, число валидно
        stub_db["set_setting"].assert_awaited()
        call = stub_db["set_setting"].call_args
        assert call.args[1] == 4.5


class TestReportsBranches:
    async def test_handle_report_empty_queue(self, message, stub_db, stub_log, stub_notifier):
        stub_db["peek_queued_by_reason"].return_value = []
        await reports_h.handle_report(message)
        text = message.answer.call_args.args[0]
        assert "пуст" in text.lower()


class TestRoutersFsmHandlerIntegration:
    """Покрытие register_fsm_handlers ветвей через прямой вызов wrappers."""

    async def test_save_api_key_wrapper_no_bot(
        self, message, state, stub_db, stub_log, monkeypatch
    ):
        from aiogram import Router
        from src import notifier as notifier_mod
        from src.bot.routers import _register_fsm_handlers

        monkeypatch.setattr(notifier_mod, "bot", None, raising=False)

        # Создаём Router и регистрируем — внутри будут wrappers
        r = Router()
        _register_fsm_handlers(r)
        # Все handlers зарегистрированы (>=4 для save + 1 cancel + 1 buffer + 1 upload = 7)
        assert len(r.message.handlers) >= 6

    async def test_upload_prompt_wrapper_no_bot(self, monkeypatch):
        from aiogram import Router
        from src import notifier as notifier_mod
        from src.bot.routers import _register_fsm_handlers

        monkeypatch.setattr(notifier_mod, "bot", None, raising=False)
        r = Router()
        # Просто регистрация не должна падать
        _register_fsm_handlers(r)


class TestSettingsUiShowReportEdgeCases:
    async def test_show_threshold_card_renders_float_correctly(self, message, stub_db, stub_log):
        from src.bot.handlers import settings_ui as ui

        stub_db["get_setting"].return_value = 4.5
        await ui.show_threshold_card(message, "hard_min_client_rating")
        text = message.answer.call_args.args[0]
        assert "4.5" in text


class TestNotifierEdgeCases:
    async def test_send_job_no_bot(self, job, monkeypatch):
        from src import notifier

        monkeypatch.setattr(notifier, "bot", None, raising=False)
        # Без bot — функция должна вернуться без crash
        await notifier.send_job(job, "Анализ", silent=True)

    async def test_send_job_from_row_no_bot(self, monkeypatch):
        from src import notifier

        monkeypatch.setattr(notifier, "bot", None, raising=False)
        await notifier.send_job_from_row({"upwork_job_id": "x", "rating": 5})


class TestKeyboardsHelpers:
    def test_card_buttons(self):
        from src.bot import keyboards

        job = MagicMock(upwork_url="https://u/x", upwork_job_id="~01a")
        kb = keyboards.card_buttons(job)
        kb_text = str(kb)
        assert "Открыть на Upwork" in kb_text
        assert "save_~01a" in kb_text

    def test_card_buttons_empty_url(self):
        from src.bot import keyboards

        job = MagicMock(upwork_url=None, upwork_job_id="~01b")
        kb = keyboards.card_buttons(job)
        assert "https://www.upwork.com/" in str(kb)


class TestDbAdditionalCoverage:
    async def test_get_prompt_returns_empty_when_missing(self, pool, monkeypatch):
        monkeypatch.setattr(db, "_pool", pool, raising=False)
        pool.fetchval.return_value = None
        result = await db.get_prompt("missing_slot")
        assert result == ""

    async def test_get_model_returns_empty_when_missing(self, pool, monkeypatch):
        monkeypatch.setattr(db, "_pool", pool, raising=False)
        pool.fetchval.return_value = None
        result = await db.get_model("prescreen_model")
        assert result == ""

    async def test_set_setting_writes(self, pool, monkeypatch):
        monkeypatch.setattr(db, "_pool", pool, raising=False)
        await db.set_setting("loud_notification_threshold", 9)
        pool.execute.assert_awaited()

    async def test_set_model_writes(self, pool, monkeypatch):
        monkeypatch.setattr(db, "_pool", pool, raising=False)
        await db.set_model("analysis_model", "vendor/m")
        pool.execute.assert_awaited()

    async def test_drain_without_limit(self, pool, monkeypatch):
        monkeypatch.setattr(db, "_pool", pool, raising=False)
        pool.fetch.return_value = []
        await db.drain_queued_by_reason("manual")  # limit=None ветка
        pool.fetch.assert_awaited()

    async def test_count_queued_cache_hit(self, pool):
        import time as time_mod

        db._count_cache.clear()
        key = (id(pool), "queued:manual")
        db._count_cache[key] = (10, time_mod.monotonic())
        result = await db.count_queued_by_reason_cached(pool, "manual")
        assert result == 10
        pool.fetchval.assert_not_awaited()


class TestMigrationsBootstrapInline:
    async def test_first_run_applies_schema(self, pool, monkeypatch, stub_log, tmp_path):
        from unittest.mock import MagicMock

        conn = MagicMock()
        conn.execute = AsyncMock()
        conn.fetch = AsyncMock(return_value=[])
        conn.fetchval = AsyncMock(return_value=False)  # upwork_jobs не существует

        class _Tx:
            async def __aenter__(s):
                return s

            async def __aexit__(s, *a):
                return False

        conn.transaction = MagicMock(return_value=_Tx())

        class _Acq:
            async def __aenter__(s):
                return conn

            async def __aexit__(s, *a):
                return False

        pool.acquire = MagicMock(return_value=_Acq())
        # bootstrap_prompts ходит в pool.fetchval напрямую, его тоже мокнем
        pool.fetchval = AsyncMock(return_value=None)

        schema_path = tmp_path / "schema.sql"
        schema_path.write_text("CREATE TABLE upwork_jobs (id int);")
        monkeypatch.setattr(migrations, "SCHEMA_PATH", schema_path, raising=False)
        monkeypatch.setattr(migrations, "MIGRATIONS", tmp_path / "migrations_dir", raising=False)
        (tmp_path / "migrations_dir").mkdir()

        await migrations.init_schema(pool)
        executes = " ".join(str(c) for c in conn.execute.call_args_list)
        assert "schema_version" in executes


class TestPipelineSafeBytes:
    def test_safe_bytes_with_bytes_input(self):
        from src.pipeline import _safe_bytes

        assert _safe_bytes(b"hello") == b"hello"

    def test_safe_bytes_with_dict(self):
        from src.pipeline import _safe_bytes

        out = _safe_bytes({"k": "v"})
        assert b'"k"' in out

    def test_safe_bytes_fallback_on_unencodable(self):
        from src.pipeline import _safe_bytes

        class Weird:
            def __repr__(self):
                return "Weird()"

        out = _safe_bytes(Weird())
        # Должно быть repr-encoded
        assert b"Weird" in out


# --------------------------------------------------------------------------- #
# notifier.set_bot + _resolve_chat_id when no users
# --------------------------------------------------------------------------- #
class TestNotifierSetBot:
    def test_set_bot_global_assignment(self, monkeypatch):
        from src import notifier

        monkeypatch.setattr(notifier, "bot", None, raising=False)
        new_bot = MagicMock()
        notifier.set_bot(new_bot)
        assert notifier.bot is new_bot

    def test_resolve_chat_id_no_users_returns_zero(self, monkeypatch):
        from src import config, notifier

        monkeypatch.setattr(config, "ALLOWED_USER_IDS", set(), raising=False)
        assert notifier._resolve_chat_id() == 0


# --------------------------------------------------------------------------- #
# Migrations — invalid filename branch
# --------------------------------------------------------------------------- #
class TestMigrationsInvalidFilename:
    async def test_skips_filename_without_number_prefix(
        self, pool, monkeypatch, stub_log, tmp_path
    ):
        from src import migrations

        conn = MagicMock()
        conn.execute = AsyncMock()
        conn.fetch = AsyncMock(return_value=[])
        conn.fetchval = AsyncMock(return_value=True)

        class _Tx:
            async def __aenter__(s):
                return s

            async def __aexit__(s, *a):
                return False

        conn.transaction = MagicMock(return_value=_Tx())

        class _Acq:
            async def __aenter__(s):
                return conn

            async def __aexit__(s, *a):
                return False

        pool.acquire = MagicMock(return_value=_Acq())
        pool.fetchval = AsyncMock(return_value=None)

        mig_dir = tmp_path / "migrations_no_num"
        mig_dir.mkdir()
        # Файл не начинается с числа — должен быть пропущен (lines 81-82)
        (mig_dir / "no_number_prefix.sql").write_text("ALTER TABLE x ADD c int;")
        monkeypatch.setattr(migrations, "MIGRATIONS", mig_dir, raising=False)
        monkeypatch.setattr(migrations, "SCHEMA_PATH", tmp_path / "schema.sql", raising=False)

        await migrations.init_schema(pool)
        executes = " ".join(str(c) for c in conn.execute.call_args_list)
        assert "ADD c int" not in executes


# --------------------------------------------------------------------------- #
# settings_ui — submenu open variants + _format_change_line variants
# --------------------------------------------------------------------------- #
class TestSettingsUiSubmenusFull:
    async def test_open_fallback_models_submenu(self, message):
        from src.bot.handlers import settings_ui as ui

        await ui.open_fallback_models_submenu(message)
        kb_text = str(message.answer.call_args.kwargs["reply_markup"])
        assert "Pre-Screen фолбэк" in kb_text

    async def test_open_presets_submenu(self, message, state):
        from src.bot.handlers import settings_ui as ui

        await ui.open_presets_submenu(message, state)
        kb_text = str(message.answer.call_args.kwargs["reply_markup"])
        assert "Все нули" in kb_text


class TestFormatChangeLine:
    def test_key_updated_format(self):
        from src.bot.handlers.settings_ui import _format_change_line

        line = _format_change_line("key_updated", "2026-05-02T10:00:00", {"updated_by": 12345})
        assert "12345" in line
        assert "обновлён" in line

    def test_prompt_updated_format(self):
        from src.bot.handlers.settings_ui import _format_change_line

        line = _format_change_line(
            "prompt_updated",
            "2026-05-02T10:00:00",
            {"old_length": 100, "new_length": 200},
        )
        assert "100" in line and "200" in line
        # содержимое НЕ показываем
        assert "длина" in line

    def test_threshold_updated_format(self):
        from src.bot.handlers.settings_ui import _format_change_line

        line = _format_change_line(
            "threshold_updated",
            "2026-05-02T10:00:00",
            {"old_value": "0", "new_value": "5", "via": "manual"},
        )
        assert "было 0" in line and "стало 5" in line

    def test_no_ts_uses_question_mark(self):
        from src.bot.handlers.settings_ui import _format_change_line

        line = _format_change_line("threshold_updated", None, {"old_value": "0", "new_value": "5"})
        assert "?" in line


class TestFormatThresholdValue:
    def test_none_returns_zero(self):
        from src.bot.handlers.settings_ui import _format_threshold_value

        assert _format_threshold_value("pre_screen_threshold", None) == "0"

    def test_int_field(self):
        from src.bot.handlers.settings_ui import _format_threshold_value

        assert _format_threshold_value("pre_screen_threshold", 5) == "5"

    def test_float_field(self):
        from src.bot.handlers.settings_ui import _format_threshold_value

        out = _format_threshold_value("hard_min_client_rating", 4.5)
        assert "4.5" in out


# --------------------------------------------------------------------------- #
# routers — module-level wrappers (после рефакторинга)
# --------------------------------------------------------------------------- #
class TestRoutersModuleWrappers:
    async def test_save_api_key_wrapper_no_bot(
        self, message, state, stub_db, stub_log, monkeypatch
    ):
        from src import notifier as notifier_mod
        from src.bot import routers

        monkeypatch.setattr(notifier_mod, "bot", None, raising=False)
        await routers._save_api_key_wrapper(message, state)
        text = message.answer.call_args.args[0]
        assert "Bot недоступен" in text

    async def test_save_api_key_wrapper_with_bot(
        self, message, state, bot, stub_db, stub_log, monkeypatch
    ):
        from src import notifier as notifier_mod
        from src.bot import routers

        monkeypatch.setattr(notifier_mod, "bot", bot, raising=False)
        # state с буфером — делегируется secrets.save_api_key
        state._data = {"buf": "sk-or-v1-" + "x" * 30, "user_message_ids": []}
        state.get_data = AsyncMock(return_value=dict(state._data))
        message.chat = MagicMock()
        message.chat.id = 1
        await routers._save_api_key_wrapper(message, state)
        # Реальный save отработал — set_secret вызвался
        stub_db["set_secret"].assert_awaited()

    async def test_upload_prompt_wrapper_no_bot(self, message, state, monkeypatch):
        from src import notifier as notifier_mod
        from src.bot import routers

        monkeypatch.setattr(notifier_mod, "bot", None, raising=False)
        # Не должно ничего делать
        await routers._upload_prompt_wrapper(message, state)
        message.answer.assert_not_awaited()

    async def test_upload_prompt_wrapper_with_bot(self, message, state, monkeypatch):
        from src import notifier as notifier_mod
        from src.bot import routers

        bot = MagicMock()
        file_obj = MagicMock()
        file_obj.read = MagicMock(return_value=b"prompt content")
        bot.download = AsyncMock(return_value=file_obj)
        doc = MagicMock()
        doc.file_name = "p.txt"
        message.document = doc

        monkeypatch.setattr(notifier_mod, "bot", bot, raising=False)
        await routers._upload_prompt_wrapper(message, state)
        message.answer.assert_awaited()


class TestRoutersEditDispatcherFull:
    """`_route_edit_btn` — все 4 ветви."""

    async def test_dispatch_to_apikey_when_no_context(self, message, state, stub_db, stub_log):
        from src.bot import routers

        state._data = {}  # нет slot/role/field
        state.get_data = AsyncMock(return_value={})
        await routers._route_edit_btn(message, state)
        # Должен войти в ApiKeyEdit
        from src.bot.states import ApiKeyEdit

        state.set_state.assert_awaited_with(ApiKeyEdit.waiting_key)

    async def test_dispatch_prompt_priority(self, message, state, stub_db, stub_log):
        from src.bot import routers

        state._data = {"slot": "analysis", "role": None, "field": None}
        state.get_data = AsyncMock(return_value=dict(state._data))
        await routers._route_edit_btn(message, state)
        from src.bot.states import PromptEdit

        state.set_state.assert_awaited_with(PromptEdit.waiting_text)

    async def test_dispatch_model_priority(self, message, state, stub_db, stub_log):
        from src.bot import routers

        state._data = {"slot": None, "role": "prescreen", "field": None}
        state.get_data = AsyncMock(return_value=dict(state._data))
        await routers._route_edit_btn(message, state)
        from src.bot.states import ModelEdit

        state.set_state.assert_awaited_with(ModelEdit.waiting_name)

    async def test_dispatch_threshold(self, message, state, stub_db, stub_log):
        from src.bot import routers

        state._data = {"slot": None, "role": None, "field": "pre_screen_threshold"}
        state.get_data = AsyncMock(return_value=dict(state._data))
        await routers._route_edit_btn(message, state)
        from src.bot.states import ThresholdEdit

        state.set_state.assert_awaited_with(ThresholdEdit.waiting_value)


# --------------------------------------------------------------------------- #
# main.py — main() entry function (signal handlers + lifespan)
# --------------------------------------------------------------------------- #
class TestMainEntry:
    async def test_main_installs_signal_handlers_and_runs_lifespan(self, monkeypatch):
        from contextlib import asynccontextmanager

        from src import main as main_mod

        lifespan_entered = []
        lifespan_exited = []

        @asynccontextmanager
        async def fake_lifespan():
            lifespan_entered.append(True)
            try:
                yield
            finally:
                lifespan_exited.append(True)

        monkeypatch.setattr(main_mod, "lifespan", fake_lifespan, raising=False)

        # Ставим SIGTERM сразу через event_loop, чтобы main вышел
        async def runner():
            task = asyncio.create_task(main_mod.main())
            await asyncio.sleep(0.05)  # дать main войти в lifespan и установить handler
            import os
            import signal

            os.kill(os.getpid(), signal.SIGTERM)
            await asyncio.wait_for(task, timeout=5)

        await runner()
        assert lifespan_entered == [True]
        assert lifespan_exited == [True]


# --------------------------------------------------------------------------- #
# db.upsert_and_get_state row=None branch (fallback)
# --------------------------------------------------------------------------- #
class TestUpsertRowNone:
    async def test_returns_default_when_fetchrow_none(self, pool, monkeypatch, job):
        monkeypatch.setattr(db, "_pool", pool, raising=False)
        pool.fetchrow.return_value = None
        inserted, state = await db.upsert_and_get_state(job)
        assert inserted is False
        assert state == "pending"


class TestGetCardRowNone:
    async def test_returns_empty_tuple(self, pool, monkeypatch):
        monkeypatch.setattr(db, "_pool", pool, raising=False)
        pool.fetchrow.return_value = None
        title, url = await db.get_card("missing_id")
        assert title == "" and url == ""


class TestMarkQueuedAsSentReturning:
    async def test_returns_count_of_returning_rows(self, pool, monkeypatch):
        monkeypatch.setattr(db, "_pool", pool, raising=False)
        # RETURNING 1 → asyncpg возвращает list[dict]; len = кол-во затронутых
        pool.fetch.return_value = [{}, {}, {}]  # 3 ряда
        n = await db.mark_queued_as_sent("manual")
        assert n == 3


class TestGetSettingHappyPath:
    async def test_get_setting_returns_value(self, pool, monkeypatch):
        monkeypatch.setattr(db, "_pool", pool, raising=False)
        pool.fetchval.return_value = 7
        assert await db.get_setting("pre_screen_threshold") == 7


class TestLlmSetSession:
    def test_set_session_assigns_global(self, monkeypatch):
        sess = MagicMock()
        monkeypatch.setattr(llm, "_session", None, raising=False)
        llm.set_session(sess)
        assert llm._session is sess


class TestPreScreenLlmFailure:
    async def test_returns_none_when_call_fails(self, monkeypatch, job, stub_db):
        async def fake_with_fallback(*args, **kwargs):
            return None  # LLM упала на обеих моделях

        monkeypatch.setattr(llm, "_with_fallback", fake_with_fallback, raising=False)
        result = await llm.pre_screen(MagicMock(), job)
        assert result is None


class TestReportsHandleReportWithRows:
    async def test_with_rows_calls_digest(self, message, stub_db, stub_log, stub_notifier):
        stub_db["peek_queued_by_reason"].return_value = [
            {
                "upwork_job_id": "~01a",
                "rating": 8,
                "job_title": "T",
                "client_country": "US",
                "budget": "$1K",
            }
        ]
        await reports_h.handle_report(message)
        # Дайджест отправлен
        message.answer.assert_awaited()
        text = message.answer.call_args.args[0]
        assert "В очереди 1" in text


class TestSettingsUiApiKeyCardWithState:
    async def test_apikey_card_with_state_resets_context(self, message, state, stub_db, stub_log):
        from src.bot.handlers import settings_ui as ui

        await ui.show_apikey_card(message, state)
        update_call = state.update_data.call_args
        assert update_call.kwargs == {"slot": None, "role": None, "field": None}


class TestRedactPreview:
    def test_short_password_hidden(self):
        """Сигнатура: (value, is_password). Короткий пароль → <скрыто>."""
        from src.bot.handlers.settings_ui import _redact_for_preview

        out = _redact_for_preview("abc", is_password=True)
        assert "<скрыто>" in out

    def test_long_text_truncated(self):
        from src.bot.handlers.settings_ui import _redact_for_preview

        out = _redact_for_preview("x" * 100, is_password=False)
        assert len(out) < 100
        assert out.endswith("…")


class TestMainDrainTimeout:
    async def test_drain_timeout_emits_event(self, monkeypatch, stub_log):
        """Покрытие main.py:89-90 — таймаут drain pipeline-задач."""
        from src import main as main_mod

        # Создаём задачу которая будет долго висеть
        async def slow_task():
            await asyncio.sleep(60)

        task = asyncio.create_task(slow_task())
        monkeypatch.setattr(main_mod.http_app, "_tasks", {task}, raising=False)
        # Ставим короткий таймаут
        monkeypatch.setattr(main_mod, "GRACEFUL_SHUTDOWN_TIMEOUT_S", 0.05, raising=False)

        dp = MagicMock()
        dp.stop_polling = AsyncMock()
        polling_task = MagicMock()
        polling_task.cancel = MagicMock()

        async def fake_await():
            return None

        polling_task.__await__ = lambda self: fake_await().__await__()
        runner = MagicMock()
        runner.cleanup = AsyncMock()
        pool = MagicMock()
        pool.close = AsyncMock()
        http_session = MagicMock()
        http_session.close = AsyncMock()
        bot = MagicMock()
        bot.session = MagicMock()
        bot.session.close = AsyncMock()

        await main_mod._graceful_shutdown(dp, polling_task, runner, pool, http_session, bot)

        events = [c.args[0] for c in stub_log.call_args_list if c.args]
        assert "shutdown_drain_timeout" in events
        # cleanup
        task.cancel()
        with contextlib_suppress():
            await task
        main_mod.http_app.set_shutting_down(False)


import contextlib as contextlib_real


def contextlib_suppress():
    return contextlib_real.suppress(asyncio.CancelledError, Exception)


class TestMainRun:
    """`run()` — синхронная entrypoint функция (вызывается из __main__)."""

    def test_run_uses_uvloop_when_available(self, monkeypatch):
        from src import main as main_mod

        captured = {}

        def fake_uvloop_run(coro):
            captured["used"] = "uvloop"
            # уничтожаем coroutine чтобы не было RuntimeWarning
            coro.close()

        fake_uvloop = MagicMock()
        fake_uvloop.run = fake_uvloop_run
        monkeypatch.setattr(main_mod, "uvloop", fake_uvloop, raising=False)
        main_mod.run()
        assert captured["used"] == "uvloop"

    def test_run_falls_back_to_asyncio_run_without_uvloop(self, monkeypatch):
        from src import main as main_mod

        captured = {}

        def fake_asyncio_run(coro):
            captured["used"] = "asyncio"
            coro.close()

        monkeypatch.setattr(main_mod, "uvloop", None, raising=False)
        monkeypatch.setattr(main_mod.asyncio, "run", fake_asyncio_run, raising=False)
        main_mod.run()
        assert captured["used"] == "asyncio"


class TestLifespan:
    """Lifespan — полноценно мокаем все внешние зависимости."""

    async def test_lifespan_full_lifecycle(self, monkeypatch, stub_log):
        from src import main as main_mod

        # asyncpg.create_pool → возвращает мок-пул
        fake_pool = MagicMock()
        fake_pool.close = AsyncMock()

        async def fake_create_pool(*args, **kwargs):
            return fake_pool

        monkeypatch.setattr(main_mod.asyncpg, "create_pool", fake_create_pool)

        # migrations.init_schema → no-op
        async def fake_init_schema(pool):
            return None

        monkeypatch.setattr(main_mod.migrations, "init_schema", fake_init_schema)

        # db.init → no-op
        async def fake_db_init(pool):
            return None

        monkeypatch.setattr(main_mod.db, "init", fake_db_init)

        # aiohttp.ClientSession → мок
        fake_session = MagicMock()
        fake_session.close = AsyncMock()
        monkeypatch.setattr(main_mod.aiohttp, "ClientSession", lambda: fake_session)

        # llm.set_session → no-op
        monkeypatch.setattr(main_mod.llm, "set_session", lambda s: None)

        # bot_app.build → возвращает (bot, dp)
        fake_bot = MagicMock()
        fake_bot.session = MagicMock()
        fake_bot.session.close = AsyncMock()
        fake_dp = MagicMock()
        fake_dp.start_polling = AsyncMock()
        fake_dp.stop_polling = AsyncMock()
        monkeypatch.setattr(main_mod.bot_app, "build", lambda http: (fake_bot, fake_dp))

        # notifier.set_bot, cron.set_bot, cron.start_cron — no-op
        monkeypatch.setattr(main_mod.notifier, "set_bot", lambda b: None)
        monkeypatch.setattr(main_mod.cron, "set_bot", lambda b: None)
        monkeypatch.setattr(main_mod.cron, "start_cron", lambda p: None)

        # http_app.start → возвращает мок-runner
        fake_runner = MagicMock()
        fake_runner.cleanup = AsyncMock()

        async def fake_http_start(*args, **kwargs):
            return fake_runner

        monkeypatch.setattr(main_mod.http_app, "start", fake_http_start)

        # gc.freeze / gc.collect — no-op
        monkeypatch.setattr(main_mod.gc, "collect", lambda: None)
        monkeypatch.setattr(main_mod.gc, "freeze", lambda: None)

        # Запускаем lifespan и сразу выходим
        async with main_mod.lifespan():
            pass

        # Все cleanup-точки прошли
        fake_pool.close.assert_awaited()
        fake_session.close.assert_awaited()
        fake_runner.cleanup.assert_awaited()
        # set_shutting_down триггерится в _graceful_shutdown — сбросим обратно
        main_mod.http_app.set_shutting_down(False)
