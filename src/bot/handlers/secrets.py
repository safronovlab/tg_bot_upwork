"""Карточка API ключа OpenRouter + ApiKeyEdit FSM. См. ../BOT.md §5."""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

from aiogram.exceptions import TelegramAPIError

from src import db, log

if TYPE_CHECKING:
    from aiogram import Bot
    from aiogram.fsm.context import FSMContext
    from aiogram.types import Message


async def save_api_key(message: Message, state: FSMContext, bot: Bot) -> None:
    """Спец-хендлер `Сохранить` для ApiKeyEdit (BOT.md §5).

    `value` НИКОГДА не попадает в логи / событие — security.
    После успешного сохранения все сообщения пользователя из state-сессии удаляются.
    """
    data = await state.get_data()
    buf = data.get("buf") or ""

    if not (10 <= len(buf) <= 200) or not buf.isascii() or not buf.isprintable():
        await message.answer("Ключ выглядит некорректно. Попробуй ещё.")
        return

    user_id = getattr(getattr(message, "from_user", None), "id", None)
    await db.set_secret("openrouter_api_key", buf, user_id)
    await db.invalidate_secrets_cache()
    await log.emit(
        "key_updated",
        field="openrouter_api_key",
        updated_by=user_id,
    )

    # Удаляем сообщения с фрагментами ключа (BOT.md §5)
    chat_id = message.chat.id if message.chat else None
    user_msg_ids = list(data.get("user_message_ids", []))
    if chat_id is not None and user_msg_ids:
        for mid in user_msg_ids:
            with contextlib.suppress(TelegramAPIError):
                await bot.delete_message(chat_id, mid)

    from src.bot.handlers.settings_ui import show_settings_menu

    await state.clear()
    await message.answer("Сохранено.")
    await show_settings_menu(message)
