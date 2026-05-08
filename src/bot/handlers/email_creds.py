"""Карточки и FSM редактирования IMAP/SMTP credentials. См. CHAT.md §4 Configuration.

Паттерн match-existing: API key OpenRouter (secrets.py + settings_ui.show_apikey_card).
Все 4 поля (imap_user, imap_password, smtp_user, smtp_password) живут в `secrets`
с приоритетом: БД → env (config.IMAP_*, config.SMTP_*).
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Any

from aiogram.exceptions import TelegramAPIError

from src import db, log
from src.bot import keyboards
from src.bot.states import EmailCredentialEdit

if TYPE_CHECKING:
    from aiogram import Bot
    from aiogram.fsm.context import FSMContext
    from aiogram.types import Message


# Whitelist допустимых имён полей (защита от случайной записи в чужой secret)
_EDITABLE_FIELDS: frozenset[str] = frozenset(
    {"imap_user", "imap_password", "smtp_user", "smtp_password"}
)

# Дисплей-метки для UI
_FIELD_LABELS: dict[str, str] = {
    "imap_user": "IMAP login",
    "imap_password": "IMAP пароль",
    "smtp_user": "SMTP login",
    "smtp_password": "SMTP пароль",
}

# Какие поля считать паролями (маскируются в UI + удаляются сообщения после Save)
_PASSWORD_FIELDS: frozenset[str] = frozenset({"imap_password", "smtp_password"})


# --------------------------------------------------------------------------- #
# Меню Email подключения — inline (вызывается из settings_ui)
# --------------------------------------------------------------------------- #
async def show_email_menu(message: Message) -> None:
    """Показать inline-меню с 4 полями IMAP/SMTP."""
    text = (
        "Email подключение (для Upwork-чата).\n\n"
        "iCloud требует app-specific password — генерируется на appleid.apple.com\n"
        "(Sign-In and Security → App-Specific Passwords).\n\n"
        "После ввода значения хранятся в БД, не в env."
    )
    await message.answer(text, reply_markup=keyboards.email_inline_kb())


# --------------------------------------------------------------------------- #
# Карточка одного поля — показывает текущее значение и кнопку Изменить
# --------------------------------------------------------------------------- #
async def show_email_field_card(
    message: Message, field: str, state: FSMContext | None = None
) -> None:
    """Карточка одного credential-поля. Пароли маскируются."""
    if field not in _EDITABLE_FIELDS:
        return

    value = await db.get_chat_secret(field)
    label = _FIELD_LABELS[field]

    if field in _PASSWORD_FIELDS:
        if len(value) >= 6:
            display = f"{value[:2]}…{value[-2:]} ({len(value)} символов)"
        elif value:
            display = "<задан>"
        else:
            display = "не задан"
    else:
        display = value or "не задан"

    text = (
        f"{label}\n\n"
        f"Текущее значение: {display}\n"
        f"Хранится в: secrets таблица (БД)"
    )

    if state is not None:
        # `field` — маркер для роутера: `_route_edit_btn` увидит и пойдёт сюда
        await state.update_data(
            email_field=field, slot=None, role=None, field=None, section="email"
        )

    await message.answer(text, reply_markup=keyboards.card_action_kb("Изменить"))


# --------------------------------------------------------------------------- #
# FSM start (нажата кнопка `Изменить` на карточке)
# --------------------------------------------------------------------------- #
async def start_email_edit(message: Message, state: FSMContext, field: str) -> None:
    if field not in _EDITABLE_FIELDS:
        return

    await state.set_state(EmailCredentialEdit.waiting_value)
    await state.update_data(email_field=field, buf="", user_message_ids=[])

    label = _FIELD_LABELS[field]
    if field in _PASSWORD_FIELDS:
        prompt_text = (
            f"Отправь новое значение для «{label}». "
            f"После сохранения сообщения с паролем будут удалены."
        )
    else:
        prompt_text = f"Отправь новое значение для «{label}» (например, immunerebel@icloud.com)."

    await message.answer(prompt_text, reply_markup=keyboards.CANCEL_ONLY_KB)


# --------------------------------------------------------------------------- #
# FSM save handler
# --------------------------------------------------------------------------- #
async def save_email_credential(message: Message, state: FSMContext, bot: Bot) -> None:
    """Спец-хендлер `Сохранить` для EmailCredentialEdit.

    Аналог save_api_key (secrets.py): для password-полей удаляет сообщения
    оператора с фрагментами пароля после сохранения.
    """
    data = await state.get_data()
    buf = (data.get("buf") or "").strip()
    field = data.get("email_field")

    if field not in _EDITABLE_FIELDS:
        await message.answer("Поле не выбрано.")
        return

    if not buf:
        await message.answer("Пусто. Попробуй ещё.")
        return

    # Базовая валидация
    if field in _PASSWORD_FIELDS:
        if not (4 <= len(buf) <= 200) or not buf.isascii() or not buf.isprintable():
            await message.answer("Пароль выглядит некорректно. Попробуй ещё.")
            return
    else:
        if not (3 <= len(buf) <= 200) or "@" not in buf:
            await message.answer("Email login выглядит некорректно. Попробуй ещё.")
            return

    user_id = getattr(getattr(message, "from_user", None), "id", None)
    await db.set_secret(field, buf, user_id)
    await db.invalidate_secrets_cache()

    # В лог НЕ кладём value — match secrets.py паттерну для openrouter_api_key
    await log.emit("key_updated", field=field, updated_by=user_id)

    # Удаляем сообщения с паролем (security)
    if field in _PASSWORD_FIELDS:
        chat_id = message.chat.id if message.chat else None
        user_msg_ids = list(data.get("user_message_ids", []))
        if chat_id is not None and user_msg_ids:
            for mid in user_msg_ids:
                with contextlib.suppress(TelegramAPIError):
                    await bot.delete_message(chat_id, mid)

    await state.clear()
    await message.answer("Сохранено.")
    await show_email_menu(message)


# --------------------------------------------------------------------------- #
# Inline-callback handler для меню email
# --------------------------------------------------------------------------- #
async def handle_email_inline_callback(callback: Any, state: FSMContext) -> None:
    """Inline-callback `email:<field>` — открыть карточку соответствующего поля.

    Match сигнатуры handle_settings_inline_callback (settings_ui.py): callback: Any
    позволяет тестам подсунуть mock без проседания type-checks.
    """
    raw_data = (callback.data or "")
    field = raw_data.removeprefix("email:")
    msg = callback.message

    if msg is None or not hasattr(msg, "answer"):
        await callback.answer()
        return

    # Удалить inline-сообщение перед открытием карточки (UX)
    if hasattr(msg, "delete"):
        with contextlib.suppress(Exception):
            await msg.delete()

    if field in _EDITABLE_FIELDS:
        await show_email_field_card(msg, field, state)

    await callback.answer()
