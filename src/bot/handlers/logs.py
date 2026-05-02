"""Логи с пагинацией + фильтр «Только ошибки». См. ../BOT.md §11."""

from __future__ import annotations

from typing import TYPE_CHECKING

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from src import db
from src.bot.formatters import format_log_rows

if TYPE_CHECKING:
    from aiogram.types import Message


PAGE_SIZE = 10


async def show_logs_page(
    message: Message,
    page: int = 0,
    only_errors: bool = False,
    *,
    edit: bool = False,
) -> None:
    """Отрисовать страницу логов. `edit=True` — обновляет существующее сообщение
    (для пагинации/фильтра), `edit=False` — отправляет новое (первый вход в Логи).
    """
    log_filter = db.LogFilter.ERRORS if only_errors else db.LogFilter.ALL
    total = await db.count_events(log_filter)
    rows = await db.fetch_events(log_filter, offset=page * PAGE_SIZE, limit=PAGE_SIZE)
    text = format_log_rows(rows, page, max(total // PAGE_SIZE + 1, 1))

    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(
            InlineKeyboardButton(
                text="← Назад",
                callback_data=f"logs:{page - 1}:{int(only_errors)}",
            )
        )
    if (page + 1) * PAGE_SIZE < total:
        nav.append(
            InlineKeyboardButton(
                text="Вперёд →",
                callback_data=f"logs:{page + 1}:{int(only_errors)}",
            )
        )
    filter_btn = InlineKeyboardButton(
        text="Все события" if only_errors else "Только ошибки",
        callback_data=f"logs:0:{int(not only_errors)}",
    )
    clear_btn = InlineKeyboardButton(text="Очистить все логи", callback_data="logs:clear")
    rows_kb: list[list[InlineKeyboardButton]] = []
    if nav:
        rows_kb.append(nav)
    rows_kb.append([filter_btn])
    rows_kb.append([clear_btn])
    kb = InlineKeyboardMarkup(inline_keyboard=rows_kb)

    if edit and hasattr(message, "edit_text"):
        from aiogram.exceptions import TelegramAPIError

        try:
            await message.edit_text(text, reply_markup=kb, parse_mode="HTML")
            return
        except TelegramAPIError:
            # Сообщение могло быть удалено/слишком старое — fallback на send
            pass
    await message.answer(text, reply_markup=kb, parse_mode="HTML")


async def handle_clear_logs(callback) -> None:  # type: ignore[no-untyped-def]
    """Inline-callback `logs:clear` — confirm-сообщение перед TRUNCATE."""
    msg = callback.message
    if msg is None or not hasattr(msg, "answer"):
        await callback.answer()
        return
    confirm_kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Да, очистить", callback_data="logs:clearyes"),
                InlineKeyboardButton(text="Нет", callback_data="logs:clearno"),
            ]
        ]
    )
    n = await db.count_events()
    await msg.answer(f"Удалить все {n} логов?", reply_markup=confirm_kb)
    await callback.answer()


async def handle_clear_logs_confirm(callback, yes: bool) -> None:  # type: ignore[no-untyped-def]
    """Confirm/cancel callback для `logs:clearyes` / `logs:clearno`."""
    msg = callback.message
    if not yes:
        if msg is not None and hasattr(msg, "delete"):
            await msg.delete()
        await callback.answer()
        return
    n = await db.clear_all_events()
    if msg is not None and hasattr(msg, "edit_text"):
        await msg.edit_text(f"Очищено {n} логов.")
    await callback.answer()
