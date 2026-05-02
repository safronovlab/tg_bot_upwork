"""Тесты bot-хендлеров — UI логика.

Соответствие BOT.md:
- §1 главное меню + счётчики
- §2 авто-пауза в блокирующих меню
- §3.7 пресеты порогов
- §4 универсальный FSM-flow (Сохранить)
- §10 Отчёт vs Синхронизация
- §11 Логи с пагинацией
- §12 Очистка БД с Да/Нет
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from src.bot import keyboards
from src.bot.handlers import (
    cleanup as cleanup_h,
)
from src.bot.handlers import (
    favorites as favorites_h,
)
from src.bot.handlers import (
    logs as logs_h,
)
from src.bot.handlers import (
    menu as menu_h,
)
from src.bot.handlers import (
    models as models_h,
)
from src.bot.handlers import (
    prompts as prompts_h,
)
from src.bot.handlers import (
    reports as reports_h,
)
from src.bot.handlers import (
    secrets as secrets_h,
)
from src.bot.handlers import (
    thresholds as thresholds_h,
)


# --------------------------------------------------------------------------- #
# §1 Главное меню
# --------------------------------------------------------------------------- #
class TestMainMenu:
    async def test_pause_button_when_running(self, pool, stub_db):
        kb = await keyboards.main_menu_kb(pool, is_paused=False)
        text = str(kb)
        assert "Остановить" in text and "Запустить" not in text

    async def test_resume_button_when_paused(self, pool, stub_db):
        kb = await keyboards.main_menu_kb(pool, is_paused=True)
        text = str(kb)
        assert "Запустить" in text and "Остановить" not in text

    async def test_report_counter_shown_when_nonzero(self, pool, stub_db):
        stub_db["count_queued_by_reason_cached"].return_value = 3
        kb = await keyboards.main_menu_kb(pool, is_paused=False)
        assert "Отчёт (3)" in str(kb)

    async def test_report_counter_hidden_when_zero(self, pool, stub_db):
        stub_db["count_queued_by_reason_cached"].return_value = 0
        stub_db["count_favorites_cached"].return_value = 0
        kb = await keyboards.main_menu_kb(pool, is_paused=False)
        text = str(kb)
        assert "Отчёт (0)" not in text
        assert "Отчёт" in text

    async def test_favorites_no_counter(self, pool, stub_db):
        stub_db["count_favorites_cached"].return_value = 12
        kb = await keyboards.main_menu_kb(pool, is_paused=False)
        text = str(kb)
        # Счётчика у Избранного нет (упрощён UI после ребилда §3)
        assert "Избранное" in text
        assert "Избранное (" not in text

    async def test_sync_button_shows_counter(self, pool, stub_db):
        stub_db["count_queued_by_reason_cached"].return_value = 7
        kb = await keyboards.main_menu_kb(pool, is_paused=False)
        text = str(kb)
        # Счётчик `(N)` у Синхронизации, как у Отчёта/Избранного
        assert "Синхронизация (7)" in text


# --------------------------------------------------------------------------- #
# §1 Pause / Resume
# --------------------------------------------------------------------------- #
class TestPauseResume:
    async def test_pause_persists(self, message, stub_db, stub_log):
        message.text = "Остановить"
        await menu_h.handle_pause_toggle(message)
        # Любой из вариантов сохранения
        assert (
            stub_db["set_setting"].await_count > 0
            or stub_db.get("set_paused_menu", AsyncMock()).await_count > 0
            or any("is_paused" in str(c) for c in stub_db["set_setting"].call_args_list)
        )

    async def test_resume_persists(self, message, stub_db, stub_log):
        message.text = "Запустить"
        await menu_h.handle_pause_toggle(message)


# --------------------------------------------------------------------------- #
# §2 Авто-пауза в меню
# --------------------------------------------------------------------------- #
class TestMenuAutoPause:
    async def test_entering_favorites_sets_menu_pause(self, message, stub_db, stub_log):
        message.text = "Избранное"
        await favorites_h.handle_favorites_btn(message)
        stub_db["set_paused_menu"].assert_awaited_with(True)

    async def test_entering_settings_sets_menu_pause(self, message, state, stub_db, stub_log):
        message.text = "Настройки"
        await menu_h.handle_settings_btn(message, state)
        stub_db["set_paused_menu"].assert_awaited_with(True)

    async def test_back_clears_menu_pause(self, message, state, stub_db, stub_log):
        message.text = "Назад"
        await menu_h.handle_back(message, state)
        stub_db["set_paused_menu"].assert_awaited_with(False)


class TestHierarchicalBack:
    """Назад из карточки/подменю должен возвращать на один шаг выше (по breadcrumb'ам)."""

    async def test_back_from_threshold_card_goes_to_thresholds(
        self, message, state, stub_db, stub_log
    ):
        await state.update_data(field="pre_screen_threshold")
        await menu_h.handle_back(message, state)
        kb_strs = [
            str(c.kwargs.get("reply_markup"))
            for c in message.answer.call_args_list
            if c.kwargs.get("reply_markup") is not None
        ]
        joined = " ".join(kb_strs)
        assert "Pre-Screen порог" in joined or "В настройки" in joined
        # field очищен
        assert (await state.get_data()).get("field") is None

    async def test_back_from_prompt_card_goes_to_prompts(
        self, message, state, stub_db, stub_log
    ):
        await state.update_data(slot="pre_screen")
        await menu_h.handle_back(message, state)
        kb_strs = [
            str(c.kwargs.get("reply_markup"))
            for c in message.answer.call_args_list
            if c.kwargs.get("reply_markup") is not None
        ]
        joined = " ".join(kb_strs)
        assert "Промпт: Pre-Screen" in joined

    async def test_back_from_main_model_card_goes_to_main_models(
        self, message, state, stub_db, stub_log
    ):
        await state.update_data(role="prescreen_model")
        await menu_h.handle_back(message, state)
        kb_strs = [
            str(c.kwargs.get("reply_markup"))
            for c in message.answer.call_args_list
            if c.kwargs.get("reply_markup") is not None
        ]
        joined = " ".join(kb_strs)
        assert "Pre-Screen модель" in joined

    async def test_back_from_fallback_model_card_goes_to_fallback(
        self, message, state, stub_db, stub_log
    ):
        await state.update_data(role="prescreen_fallback_model")
        await menu_h.handle_back(message, state)
        kb_strs = [
            str(c.kwargs.get("reply_markup"))
            for c in message.answer.call_args_list
            if c.kwargs.get("reply_markup") is not None
        ]
        joined = " ".join(kb_strs)
        assert "Pre-Screen фолбэк" in joined

    async def test_back_from_presets_goes_to_thresholds(
        self, message, state, stub_db, stub_log
    ):
        await state.update_data(section="presets")
        await menu_h.handle_back(message, state)
        kb_strs = [
            str(c.kwargs.get("reply_markup"))
            for c in message.answer.call_args_list
            if c.kwargs.get("reply_markup") is not None
        ]
        joined = " ".join(kb_strs)
        # thresholds_menu отправляется как inline + reply-kb с "В настройки"
        assert "Pre-Screen порог" in joined or "В настройки" in joined

    async def test_back_from_no_breadcrumb_goes_to_main(
        self, message, state, stub_db, stub_log
    ):
        # State пустой — никаких breadcrumbs
        await menu_h.handle_back(message, state)
        stub_db["set_paused_menu"].assert_awaited_with(False)

    async def test_start_clears_menu_pause(self, message, stub_db, stub_log):
        await menu_h.handle_start(message)
        stub_db["set_paused_menu"].assert_awaited_with(False)


# --------------------------------------------------------------------------- #
# §10 Отчёт vs Синхронизация
# --------------------------------------------------------------------------- #
class TestReportAndSync:
    async def test_report_only_drains_manual(
        self, message, stub_db, stub_log, stub_notifier, monkeypatch
    ):
        stub_db["peek_queued_by_reason"].return_value = []
        await reports_h.handle_report(message)
        stub_db["peek_queued_by_reason"].assert_awaited_with("manual")

    async def test_sync_drains_menu_queue_immediately(
        self, message, stub_db, stub_log, stub_notifier
    ):
        stub_db["drain_queued_by_reason"].return_value = [
            {
                "upwork_job_id": "~a",
                "ai_analysis": "x",
                "upwork_url": "u",
                "job_title": "t",
                "rating": 8,
            },
        ]
        await reports_h.handle_sync(message)
        stub_db["drain_queued_by_reason"].assert_awaited_with("menu")

    async def test_sync_no_queue_says_empty(self, message, stub_db, stub_log, stub_notifier):
        stub_db["drain_queued_by_reason"].return_value = []
        await reports_h.handle_sync(message)
        message.answer.assert_awaited()
        text = (
            message.answer.call_args.args[0]
            if message.answer.call_args.args
            else message.answer.call_args.kwargs.get("text", "")
        )
        assert "нет" in text.lower()

    async def test_report_clear_marks_sent(self, message, stub_db, stub_log):
        stub_db["mark_queued_as_sent"].return_value = 3
        await reports_h.handle_report_clear(message)
        stub_db["mark_queued_as_sent"].assert_awaited_with("manual")


# --------------------------------------------------------------------------- #
# §4 Универсальный FSM-flow (Сохранить)
# --------------------------------------------------------------------------- #
class TestUniversalFsmFlow:
    async def test_threshold_save_validates_range(self, message, state, stub_db, stub_log):
        state._data.update({"buf": "999", "field": "pre_screen_threshold"})
        state.get_data = AsyncMock(return_value=state._data)
        await thresholds_h.save_threshold(message, state)
        # 999 вне 0..10 — отвечает «Диапазон …»
        text = (
            message.answer.call_args.args[0]
            if message.answer.call_args.args
            else message.answer.call_args.kwargs.get("text", "")
        )
        assert "Диапазон" in text or "диапазон" in text.lower()

    async def test_threshold_save_writes_db(self, message, state, stub_db, stub_log):
        state._data.update({"buf": "5", "field": "pre_screen_threshold"})
        state.get_data = AsyncMock(return_value=state._data)
        await thresholds_h.save_threshold(message, state)
        stub_db["set_setting"].assert_awaited()

    async def test_threshold_save_emits_event(self, message, state, stub_db, stub_log):
        state._data.update({"buf": "5", "field": "pre_screen_threshold"})
        state.get_data = AsyncMock(return_value=state._data)
        await thresholds_h.save_threshold(message, state)
        events = [c.args[0] for c in stub_log.call_args_list if c.args]
        assert "threshold_updated" in events

    async def test_prompt_save_writes_history_first(self, message, state, stub_db, stub_log):
        state._data.update({"buf": "x" * 100, "slot": "analysis"})
        state.get_data = AsyncMock(return_value=state._data)
        await prompts_h.save_prompt(message, state)
        stub_db["insert_prompt_history"].assert_awaited()
        stub_db["update_prompt"].assert_awaited()

    async def test_prompt_too_short_rejected(self, message, state, stub_db, stub_log):
        state._data.update({"buf": "short", "slot": "analysis"})
        state.get_data = AsyncMock(return_value=state._data)
        await prompts_h.save_prompt(message, state)
        # update НЕ вызывается
        stub_db["update_prompt"].assert_not_awaited()

    async def test_apikey_validation_rejects_too_short(
        self, message, state, bot, stub_db, stub_log
    ):
        state._data.update({"buf": "abc"})
        state.get_data = AsyncMock(return_value=state._data)
        await secrets_h.save_api_key(message, state, bot)
        stub_db["set_secret"].assert_not_awaited()

    async def test_apikey_save_does_not_log_value(self, message, state, bot, stub_db, stub_log):
        state._data.update({"buf": "sk-or-v1-" + "x" * 30})
        state.get_data = AsyncMock(return_value=state._data)
        await secrets_h.save_api_key(message, state, bot)
        # Среди event-вызовов нет аргумента со значением ключа
        for call in stub_log.call_args_list:
            assert "sk-or-v1-" not in str(call)

    async def test_model_format_validation(self, message, state, stub_db, stub_log):
        state._data.update({"buf": "not_a_valid_model_name", "role": "prescreen"})
        state.get_data = AsyncMock(return_value=state._data)
        await models_h.save_model(message, state)
        stub_db["set_model"].assert_not_awaited()

    async def test_model_save_emits_model_updated(self, message, state, stub_db, stub_log):
        state._data.update({"buf": "xiaomi/mimo-v2-flash", "role": "prescreen"})
        state.get_data = AsyncMock(return_value=state._data)
        await models_h.save_model(message, state)
        events = [c.args[0] for c in stub_log.call_args_list if c.args]
        assert "model_updated" in events


# --------------------------------------------------------------------------- #
# §3.7 Пресеты
# --------------------------------------------------------------------------- #
class TestPresets:
    def test_three_presets_defined(self):
        assert "zeros" in thresholds_h.PRESETS
        assert "standard" in thresholds_h.PRESETS
        assert "strict" in thresholds_h.PRESETS

    def test_zeros_preset_disables_filters(self):
        z = thresholds_h.PRESETS["zeros"]
        assert z["pre_screen_threshold"] == 0
        assert z["analysis_threshold"] == 0
        assert z["hard_min_client_spent"] == 0
        assert z["hard_reject_no_hires"] is False

    def test_standard_preset_balanced(self):
        s = thresholds_h.PRESETS["standard"]
        assert s["pre_screen_threshold"] == 5
        assert s["analysis_threshold"] == 5
        assert s["loud_notification_threshold"] == 8

    def test_strict_preset_high(self):
        s = thresholds_h.PRESETS["strict"]
        assert s["pre_screen_threshold"] == 7
        assert s["analysis_threshold"] == 7
        assert s["hard_reject_no_hires"] is True

    async def test_apply_preset_emits_preset_applied(self, pool, stub_db, stub_log):
        await thresholds_h.apply_preset(pool, "standard", user_id=701492865)
        events = [c.args[0] for c in stub_log.call_args_list if c.args]
        assert "preset_applied" in events

    async def test_apply_preset_invalidates_cache(self, pool, stub_db, stub_log):
        await thresholds_h.apply_preset(pool, "standard", user_id=701492865)
        stub_db["invalidate_settings_cache"].assert_awaited()


# --------------------------------------------------------------------------- #
# §11 Логи с пагинацией
# --------------------------------------------------------------------------- #
class TestLogs:
    async def test_logs_page_size_10(self):
        assert logs_h.PAGE_SIZE == 10

    async def test_show_logs_only_errors_filter(self, message, stub_db, stub_log):
        from src.db import LogFilter

        stub_db["fetch_events"].return_value = []
        stub_db["count_events"].return_value = 0
        await logs_h.show_logs_page(message, page=0, only_errors=True)
        # передаём typed LogFilter, а не свободный SQL
        stub_db["count_events"].assert_awaited_with(LogFilter.ERRORS)
        kwargs = stub_db["fetch_events"].call_args.kwargs
        args = stub_db["fetch_events"].call_args.args
        assert LogFilter.ERRORS in args or kwargs.get("log_filter") == LogFilter.ERRORS

    async def test_pagination_has_next_button(self, message, stub_db, stub_log):
        stub_db["count_events"].return_value = 50
        stub_db["fetch_events"].return_value = [
            {"ts": None, "level": 0, "event": "x", "data": {}} for _ in range(10)
        ]
        await logs_h.show_logs_page(message, page=0, only_errors=False)
        kwargs = message.answer.call_args.kwargs
        assert kwargs.get("reply_markup") is not None


# --------------------------------------------------------------------------- #
# §12 Очистка БД
# --------------------------------------------------------------------------- #
class TestCleanup:
    async def test_clear_db_button_asks_confirm(self, message, state):
        await cleanup_h.handle_clear_db_button(message, state)
        text = (
            message.answer.call_args.args[0]
            if message.answer.call_args.args
            else message.answer.call_args.kwargs.get("text", "")
        )
        assert "Очистить" in text or "очистить" in text.lower()

    async def test_confirm_yes_truncates(self, message, state, stub_db, stub_log):
        await cleanup_h.handle_confirm_yes(message, state)
        stub_db["truncate_jobs"].assert_awaited_once()

    async def test_confirm_yes_emits_db_truncated(self, message, state, stub_db, stub_log):
        await cleanup_h.handle_confirm_yes(message, state)
        events = [c.args[0] for c in stub_log.call_args_list if c.args]
        assert "db_truncated" in events

    async def test_confirm_no_does_not_truncate(self, message, state, stub_db, stub_log):
        await cleanup_h.handle_confirm_no(message, state)
        stub_db["truncate_jobs"].assert_not_awaited()


# --------------------------------------------------------------------------- #
# Inline кнопки на карточке вакансии (BOT.md §9)
# --------------------------------------------------------------------------- #
class TestFavoriteCallbacks:
    async def test_save_favorite_sets_flag(self, stub_db, stub_log):
        stub_db["get_job_full"].return_value = {
            "upwork_job_id": "~01abc",
            "ai_analysis": "A",
            "upwork_url": "u",
            "job_title": "T",
            "job_description": "D",
            "questions": None,
        }
        callback = MagicMock()
        callback.data = "save_~01abc"
        callback.answer = AsyncMock()
        callback.message = MagicMock()
        callback.message.edit_text = AsyncMock()
        await favorites_h.handle_save_favorite(callback)
        stub_db["set_favorite"].assert_awaited_once_with("~01abc", True)
        callback.answer.assert_awaited()

    async def test_delete_favorite_unsets_flag(self, stub_db, stub_log):
        callback = MagicMock()
        callback.data = "del_~01abc"
        callback.answer = AsyncMock()
        callback.message = MagicMock()
        callback.message.delete = AsyncMock()
        await favorites_h.handle_delete_favorite(callback)
        stub_db["set_favorite"].assert_awaited_once_with("~01abc", False)
        callback.message.delete.assert_awaited()

    async def test_analysis_live_edits_message(self, stub_db, stub_log):
        stub_db["get_job_full"].return_value = {
            "upwork_job_id": "~01abc",
            "ai_analysis": "Анализ R7",
            "upwork_url": "https://u",
            "job_title": "T",
            "job_description": "D",
            "questions": None,
        }
        callback = MagicMock()
        callback.data = "analysis_~01abc"
        callback.answer = AsyncMock()
        callback.message = MagicMock()
        callback.message.edit_text = AsyncMock()
        await favorites_h.handle_show_analysis_live(callback)
        stub_db["get_job_full"].assert_awaited_once_with("~01abc")
        callback.message.edit_text.assert_awaited()

    async def test_save_favorite_marks_and_edits_to_desc(self, stub_db, stub_log):
        stub_db["get_job_full"].return_value = {
            "upwork_job_id": "~01abc",
            "ai_analysis": "Анализ",
            "upwork_url": "https://u",
            "job_title": "T",
            "job_description": "D",
            "questions": None,
        }
        callback = MagicMock()
        callback.data = "save_~01abc"
        callback.answer = AsyncMock()
        callback.message = MagicMock()
        callback.message.edit_text = AsyncMock()
        await favorites_h.handle_save_favorite(callback)
        stub_db["set_favorite"].assert_awaited_once_with("~01abc", True)
        callback.message.edit_text.assert_awaited()
