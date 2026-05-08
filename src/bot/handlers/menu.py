"""/start, главное меню, кнопки Запустить/Остановить. См. ../BOT.md §1, §2."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from src import db, log
from src.bot.keyboards import main_menu_kb

if TYPE_CHECKING:
    from aiogram.fsm.context import FSMContext
    from aiogram.types import Message

    from src.models import BotSettings


async def _send_main_menu(message: Message, settings: BotSettings) -> None:
    """Прислать главную ReplyKeyboard со счётчиками (BOT.md §1)."""
    pool = db._conn()
    kb = await main_menu_kb(pool, is_paused=settings.is_paused)
    await message.answer("Радар Upwork-вакансий. Жми кнопку.", reply_markup=kb)


async def _send_main_menu_full(message: Message) -> None:
    await db.set_paused_menu(False)
    settings = await db.get_settings_cached()
    await _send_main_menu(message, settings)


async def handle_start(message: Message) -> None:
    """`/start` — приветствие + главное меню + сброс is_paused_menu в False."""
    await _send_main_menu_full(message)


async def handle_back(message: Message, state: FSMContext) -> None:
    """`Назад` — иерархическая навигация по breadcrumb'ам в state (BOT.md §3).

    Уровни:
      - card (slot/role/field) → возврат в parent submenu
      - presets submenu (section=presets) → возврат в thresholds submenu
      - settings inline / sub-submenu без breadcrumb'ов → главное меню
    """
    from src.bot.handlers import settings_ui as ui

    data = await state.get_data()

    # Email-card (CHAT.md §4) → email inline-menu (раньше field-card → thresholds)
    if data.get("email_field"):
        await state.update_data(email_field=None)
        from src.bot.handlers.email_creds import show_email_menu

        await show_email_menu(message)
        return

    # Card → parent sub-submenu
    if data.get("field"):
        await state.update_data(field=None)
        await ui.show_thresholds_menu(message)
        return
    if data.get("role"):
        role = str(data["role"])
        await state.update_data(role=None)
        if "fallback" in role:
            await ui.open_fallback_models_submenu(message)
        else:
            await ui.open_main_models_submenu(message)
        return
    if data.get("slot"):
        await state.update_data(slot=None)
        await ui.open_prompts_submenu(message)
        return

    # Presets submenu → thresholds inline-menu
    if data.get("section") == "presets":
        await state.update_data(section=None)
        await ui.show_thresholds_menu(message)
        return

    # Email submenu → Settings inline (вышли из email-меню)
    if data.get("section") == "email":
        await state.update_data(section=None)
        await ui.show_settings_menu(message)
        return

    # Logs / API key / Cleanup card → Settings inline
    if data.get("section") == "settings":
        await state.update_data(section=None)
        await ui.show_settings_menu(message)
        return

    # Default: main menu
    await _send_main_menu_full(message)


async def handle_settings_btn(message: Message, state: FSMContext) -> None:
    """`Настройки` — inline-меню разделов + reply-keyboard `[Назад]` (BOT.md §3)."""
    from src.bot.handlers.settings_ui import show_settings_menu

    # Чистим breadcrumb'ы — пользователь начинает свежую навигацию
    await state.update_data(slot=None, role=None, field=None, section=None)
    await db.set_paused_menu(True)
    await show_settings_menu(message)


async def handle_pause_toggle(message: Message) -> None:
    """`Запустить` / `Остановить` — переключает is_paused, сохраняет в БД."""
    new_value = message.text == "Остановить"
    await db.set_setting("is_paused", new_value)
    await db.invalidate_settings_cache()
    user_id = getattr(getattr(message, "from_user", None), "id", None)
    await log.emit(
        "pause_toggled",
        level=logging.INFO,
        is_paused=new_value,
        updated_by=user_id,
    )
    settings = await db.get_settings_cached()
    pool = db._conn()
    kb = await main_menu_kb(pool, is_paused=settings.is_paused)
    text = "Остановлено." if new_value else "Запущено."
    await message.answer(text, reply_markup=kb)
