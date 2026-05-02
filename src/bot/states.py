"""FSM-states для редактирования промтов, ключа, моделей, порогов, очистки. См. BOT.md §4."""

from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class PromptEdit(StatesGroup):
    waiting_text = State()


class ApiKeyEdit(StatesGroup):
    waiting_key = State()


class ModelEdit(StatesGroup):
    waiting_name = State()


class ThresholdEdit(StatesGroup):
    waiting_value = State()


class CleanupConfirm(StatesGroup):
    waiting = State()
