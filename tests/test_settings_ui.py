"""Тесты settings UI: подменю, карточки, FSM entry, buffer, toggle, presets.

Соответствие BOT.md §3.1-§3.7, §4, §5, §6, §7, §8.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

from src.bot.handlers import settings_ui as ui
from src.bot.states import ApiKeyEdit, ModelEdit, PromptEdit, ThresholdEdit


# --------------------------------------------------------------------------- #
# Подменю (BOT.md §3.1-§3.4)
# --------------------------------------------------------------------------- #
class TestSubmenus:
    async def test_open_prompts_submenu(self, message):
        await ui.open_prompts_submenu(message)
        message.answer.assert_awaited_once()
        kb = message.answer.call_args.kwargs.get("reply_markup")
        assert kb is not None
        text = str(kb)
        for slot_label in ui.PROMPT_LABEL_TO_SLOT:
            assert slot_label in text

    async def test_open_main_models_submenu(self, message):
        await ui.open_main_models_submenu(message)
        kb_text = str(message.answer.call_args.kwargs["reply_markup"])
        assert "Pre-Screen модель" in kb_text
        assert "Анализ модель" in kb_text

    async def test_open_thresholds_submenu(self, message):
        await ui.open_thresholds_submenu(message)
        # Меню порогов теперь идёт двумя сообщениями: reply-kb [В настройки] +
        # inline-kb со всеми лейблами.
        kb_strs = [
            str(c.kwargs.get("reply_markup"))
            for c in message.answer.call_args_list
            if c.kwargs.get("reply_markup") is not None
        ]
        joined = " ".join(kb_strs)
        for label in ui.THRESHOLD_LABEL_TO_FIELD:
            assert label in joined


# --------------------------------------------------------------------------- #
# Карточки (BOT.md §5, §6, §7, §3.5)
# --------------------------------------------------------------------------- #
class TestCards:
    async def test_show_prompt_card_renders_content(self, message, stub_db, stub_log):
        stub_db["get_prompt"].return_value = "x" * 700
        await ui.show_prompt_card(message, "analysis")
        text = message.answer.call_args.args[0]
        assert "Промпт: analysis" in text
        assert "700 символов" in text

    async def test_show_model_card_renders(self, message, stub_db, stub_log):
        stub_db["get_model"].return_value = "vendor/model-name"
        await ui.show_model_card(message, "prescreen")
        text = message.answer.call_args.args[0]
        assert "Pre-Screen модель" in text
        assert "vendor/model-name" in text

    async def test_show_threshold_card_renders(self, message, stub_db, stub_log):
        stub_db["get_setting"].return_value = 5
        await ui.show_threshold_card(message, "pre_screen_threshold")
        text = message.answer.call_args.args[0]
        assert "Pre-Screen порог" in text
        assert "Текущее значение: 5" in text

    async def test_show_apikey_card_masks_key(self, message, stub_db, stub_log):
        stub_db["get_openrouter_key"].return_value = "sk-or-v1-abc1234567890xyz"
        await ui.show_apikey_card(message)
        text = message.answer.call_args.args[0]
        assert "sk-or" in text
        assert "0xyz" in text  # последние 4 символа
        # полный ключ НЕ должен быть в выводе
        assert "abc1234567890" not in text

    async def test_show_apikey_card_when_no_key(self, message, stub_db, stub_log):
        stub_db["get_openrouter_key"].return_value = ""
        await ui.show_apikey_card(message)
        text = message.answer.call_args.args[0]
        assert "не задан" in text

    async def test_show_no_hires_toggle_card(self, message, stub_db, stub_log):
        stub_db["get_setting"].return_value = True
        await ui.show_no_hires_toggle_card(message)
        text = message.answer.call_args.args[0]
        assert "Отсекать клиентов без наймов" in text
        assert "ВКЛЮЧЕНО" in text


# --------------------------------------------------------------------------- #
# FSM entry handlers (BOT.md §4)
# --------------------------------------------------------------------------- #
class TestFsmEntries:
    async def test_start_prompt_edit_sets_state(self, message, state, stub_db):
        await ui.start_prompt_edit(message, state, "analysis")
        state.set_state.assert_awaited_with(PromptEdit.waiting_text)
        assert state._data.get("slot") == "analysis"
        assert state._data.get("buf") == ""

    async def test_start_model_edit_sets_state(self, message, state, stub_db):
        await ui.start_model_edit(message, state, "prescreen")
        state.set_state.assert_awaited_with(ModelEdit.waiting_name)
        assert state._data.get("role") == "prescreen"

    async def test_start_threshold_edit_sets_state(self, message, state, stub_db):
        await ui.start_threshold_edit(message, state, "pre_screen_threshold")
        state.set_state.assert_awaited_with(ThresholdEdit.waiting_value)
        assert state._data.get("field") == "pre_screen_threshold"

    async def test_start_apikey_edit_sets_state(self, message, state, stub_db):
        await ui.start_apikey_edit(message, state)
        state.set_state.assert_awaited_with(ApiKeyEdit.waiting_key)


# --------------------------------------------------------------------------- #
# Universal buffer (BOT.md §4)
# --------------------------------------------------------------------------- #
class TestUniversalBuffer:
    async def test_prompt_edit_accumulates(self, message, state):
        state.get_state = AsyncMock(return_value=PromptEdit.waiting_text.state)
        state._data["buf"] = "first "
        state.get_data = AsyncMock(return_value=dict(state._data))

        message.text = "second"
        await ui.universal_buffer(message, state)
        # Накопительный append
        update_call = state.update_data.call_args
        assert update_call.kwargs["buf"] == "first second"

    async def test_threshold_edit_replaces(self, message, state):
        state.get_state = AsyncMock(return_value=ThresholdEdit.waiting_value.state)
        state._data["buf"] = "old"
        state.get_data = AsyncMock(return_value=dict(state._data))

        message.text = "  42  "
        await ui.universal_buffer(message, state)
        update_call = state.update_data.call_args
        # Замещающий + strip
        assert update_call.kwargs["buf"] == "42"

    async def test_apikey_buffer_redacts_in_preview(self, message, state):
        state.get_state = AsyncMock(return_value=ApiKeyEdit.waiting_key.state)
        state._data = {"buf": "", "user_message_ids": []}
        state.get_data = AsyncMock(return_value=dict(state._data))
        message.text = "sk-or-v1-very-secret-12345"
        message.message_id = 42

        await ui.universal_buffer(message, state)
        # В preview — только маска
        preview_call = message.answer.call_args.args[0]
        assert "very-secret" not in preview_call
        assert "12345" in preview_call or "…" in preview_call

    async def test_apikey_buffer_collects_message_ids(self, message, state):
        state.get_state = AsyncMock(return_value=ApiKeyEdit.waiting_key.state)
        state._data = {"buf": "", "user_message_ids": [10, 20]}
        state.get_data = AsyncMock(return_value=dict(state._data))
        message.text = "sk-or-v1-key"
        message.message_id = 30

        await ui.universal_buffer(message, state)
        assert state.update_data.call_args.kwargs["user_message_ids"] == [10, 20, 30]


# --------------------------------------------------------------------------- #
# Toggle (BOT.md §3.6)
# --------------------------------------------------------------------------- #
class TestToggle:
    async def test_toggle_inverts_and_emits(self, message, state, stub_db, stub_log):
        # Generic handler читает поле для toggle из state.data.field
        state._data["field"] = "hard_reject_no_hires"
        state.get_data = AsyncMock(return_value=dict(state._data))
        stub_db["get_setting"].return_value = False
        await ui.handle_no_hires_toggle(message, state)
        # set_setting был вызван с противоположным значением
        stub_db["set_setting"].assert_awaited()
        call = stub_db["set_setting"].call_args
        assert call.args[0] == "hard_reject_no_hires"
        assert call.args[1] is True  # инверсия False
        events = [c.args[0] for c in stub_log.call_args_list if c.args]
        assert "threshold_updated" in events

    async def test_chat_ai_toggle(self, message, state, stub_db, stub_log):
        """CHAT.md §6 — toggle chat_ai_night_enabled через тот же generic handler."""
        state._data["field"] = "chat_ai_night_enabled"
        state.get_data = AsyncMock(return_value=dict(state._data))
        stub_db["get_setting"].return_value = True
        await ui.handle_no_hires_toggle(message, state)
        call = stub_db["set_setting"].call_args
        assert call.args[0] == "chat_ai_night_enabled"
        assert call.args[1] is False  # инверсия True

    async def test_unknown_field_silently_ignored(self, message, state, stub_db):
        """Generic handler не должен трогать БД если field не whitelisted."""
        state._data["field"] = "totally_random_field"
        state.get_data = AsyncMock(return_value=dict(state._data))
        await ui.handle_no_hires_toggle(message, state)
        stub_db["set_setting"].assert_not_awaited()


# --------------------------------------------------------------------------- #
# Presets (BOT.md §3.7)
# --------------------------------------------------------------------------- #
class TestPresetsFlow:
    async def test_select_preset_saves_pending_and_shows_summary(
        self, message, state, stub_db, stub_log
    ):
        message.text = "Стандарт"
        await ui.select_preset(message, state)
        update_call = state.update_data.call_args
        assert update_call.kwargs["pending_preset"] == "standard"
        # Сводка содержит хотя бы одно из значений пресета
        text = message.answer.call_args.args[0]
        assert "Стандарт" in text or "standard" in text

    async def test_confirm_preset_yes_applies(
        self, pool, message, state, stub_db, stub_log, monkeypatch
    ):
        from src import db as db_mod

        monkeypatch.setattr(db_mod, "_pool", pool, raising=False)
        state._data["pending_preset"] = "zeros"
        state.get_data = AsyncMock(return_value=dict(state._data))

        await ui.confirm_preset_yes(message, state)
        events = [c.args[0] for c in stub_log.call_args_list if c.args]
        assert "preset_applied" in events
        state.clear.assert_awaited()

    async def test_confirm_preset_no_clears_state(self, message, state, stub_db, stub_log):
        await ui.confirm_preset_no(message, state)
        state.clear.assert_awaited()


# --------------------------------------------------------------------------- #
# Routing helpers (BOT.md §3.1-§3.4)
# --------------------------------------------------------------------------- #
class TestRouting:
    async def test_route_prompt_button_opens_card(self, message, state, stub_db, stub_log):
        message.text = "Промпт: Анализ"
        stub_db["get_prompt"].return_value = "test prompt"
        await ui.route_prompt_button(message, state)
        message.answer.assert_awaited()
        # state должен содержать slot для последующего «Изменить»
        update_call = state.update_data.call_args
        assert update_call.kwargs.get("slot") == "analysis"

    async def test_route_model_button_opens_card(self, message, state, stub_db, stub_log):
        message.text = "Анализ фолбэк"
        stub_db["get_model"].return_value = "vendor/m"
        await ui.route_model_button(message, state)
        message.answer.assert_awaited()
        assert state.update_data.call_args.kwargs.get("role") == "analysis_fallback"

    async def test_route_threshold_button_opens_card(self, message, state, stub_db, stub_log):
        message.text = "Pre-Screen порог"
        stub_db["get_setting"].return_value = 5
        await ui.route_threshold_button(message, state)
        message.answer.assert_awaited()
        assert state.update_data.call_args.kwargs.get("field") == "pre_screen_threshold"

    async def test_unknown_button_does_nothing(self, message, state, stub_db):
        message.text = "Случайный текст"
        await ui.route_prompt_button(message, state)
        message.answer.assert_not_awaited()


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #
class TestValidation:
    def test_valid_model_name(self):
        assert ui.is_valid_model_name("xiaomi/mimo-v2-flash")
        assert ui.is_valid_model_name("anthropic/claude-haiku-4-5")
        assert ui.is_valid_model_name("vendor/model:tag")

    def test_invalid_model_name(self):
        assert not ui.is_valid_model_name("not_a_model")
        assert not ui.is_valid_model_name("ABC/XYZ")  # uppercase
        assert not ui.is_valid_model_name("")
        assert not ui.is_valid_model_name("a/b" * 60)


# --------------------------------------------------------------------------- #
# History block (BOT.md §8)
# --------------------------------------------------------------------------- #
class TestHistoryRender:
    async def test_render_history_returns_empty_when_no_changes(self, monkeypatch, stub_db):
        from src import db as db_mod

        async def empty(*args, **kwargs):
            return []

        monkeypatch.setattr(db_mod, "get_recent_changes", empty, raising=False)
        result = await ui._render_history("threshold_updated", "pre_screen_threshold")
        assert result == ""

    async def test_render_history_threshold_format(self, monkeypatch, stub_db):
        from src import db as db_mod

        async def fake(*args, **kwargs):
            return [
                {
                    "ts": "2026-05-02T14:30:00",
                    "data": {"old_value": "0", "new_value": "5", "via": "manual"},
                }
            ]

        monkeypatch.setattr(db_mod, "get_recent_changes", fake, raising=False)
        result = await ui._render_history("threshold_updated", "pre_screen_threshold")
        assert "Последние изменения" in result
        assert "было 0" in result and "стало 5" in result and "manual" in result

    async def test_render_history_key_updated_no_value(self, monkeypatch, stub_db):
        """key_updated должен показывать только updated_by, не value."""
        from src import db as db_mod

        async def fake(*args, **kwargs):
            return [{"ts": "2026-05-02T10:00:00", "data": {"updated_by": 12345}}]

        monkeypatch.setattr(db_mod, "get_recent_changes", fake, raising=False)
        result = await ui._render_history("key_updated", "openrouter_api_key")
        assert "12345" in result
        assert "value" not in result.lower()
