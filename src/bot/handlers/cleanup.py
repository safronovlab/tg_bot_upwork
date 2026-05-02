"""Очистить БД (Да/Нет) — CleanupConfirm FSM. См. ../BOT.md §12."""

from __future__ import annotations

from typing import Any

from src import db, log
from src.bot.keyboards import cleanup_confirm_kb
from src.bot.states import CleanupConfirm


async def handle_clear_db_button(message: Any, state: Any) -> None:
    """Кнопка `Очистить БД` — задаёт Да/Нет."""
    await message.answer(
        "Очистить базу данных вакансий? Все записи будут удалены безвозвратно.",
        reply_markup=cleanup_confirm_kb(),
    )
    await state.set_state(CleanupConfirm.waiting)


async def handle_confirm_yes(message: Any, state: Any) -> None:
    from src.bot.handlers.settings_ui import show_settings_menu

    await db.truncate_jobs()
    user_id = getattr(getattr(message, "from_user", None), "id", None)
    await log.emit("db_truncated", updated_by=user_id)
    await message.answer("Сохранено.")
    await show_settings_menu(message)
    await state.clear()


async def handle_confirm_no(message: Any, state: Any) -> None:
    from src.bot.handlers.settings_ui import show_settings_menu

    await message.answer("Отменено.")
    await show_settings_menu(message)
    await state.clear()
