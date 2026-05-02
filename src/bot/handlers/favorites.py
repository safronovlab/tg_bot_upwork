"""Подменю Избранное + четыре view-state карточки. См. ../BOT.md §9."""

from __future__ import annotations

from typing import TYPE_CHECKING

from aiogram.exceptions import TelegramAPIError

from src import db, notifier
from src.bot.keyboards import favorites_submenu_kb

if TYPE_CHECKING:
    from aiogram.types import CallbackQuery, Message


# --------------------------------------------------------------------------- #
# Reply-кнопка `Избранное` главного меню — открыть подменю + дамп списка
# --------------------------------------------------------------------------- #
async def handle_favorites_btn(message: Message) -> None:
    """Вход в Избранное → подменю + список favorited вакансий (BOT.md §9)."""
    await db.set_paused_menu(True)
    rows = await db.list_favorites()
    if not rows:
        await message.answer(
            "Избранное пусто.",
            reply_markup=favorites_submenu_kb(),
        )
        return
    await message.answer(
        f"Избранное ({len(rows)}). Кнопки на каждой карточке.",
        reply_markup=favorites_submenu_kb(),
    )
    for row in rows:
        await notifier.send_favorite_card(row)


# --------------------------------------------------------------------------- #
# Reply-кнопка `Очистить всё` — inline-confirm затем clear
# --------------------------------------------------------------------------- #
async def handle_clear_all_request(message: Message) -> None:
    """`Очистить всё` → confirm-сообщение с inline `[Да] [Нет]`."""
    n = len(await db.list_favorites())
    if n == 0:
        await message.answer("Избранное и так пусто.")
        return
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Да, очистить всё", callback_data="clrfav:yes"),
                InlineKeyboardButton(text="Нет", callback_data="clrfav:no"),
            ]
        ]
    )
    await message.answer(f"Удалить все {n} записи из избранного?", reply_markup=kb)


async def handle_clear_all_callback(callback: CallbackQuery) -> None:
    """Inline-callback `clrfav:yes|no`."""
    data = (callback.data or "").split(":", 1)[-1]
    msg = callback.message
    if data == "no":
        if msg is not None and hasattr(msg, "delete"):
            await msg.delete()
        await callback.answer()
        return
    n = await db.clear_all_favorites()
    if msg is not None and hasattr(msg, "edit_text"):
        await msg.edit_text(f"Очищено {n} записей.")
    await callback.answer()


# --------------------------------------------------------------------------- #
# Inline-callback `save_<id>` — A → B (live, mark fav, edit to desc-view)
# --------------------------------------------------------------------------- #
async def handle_save_favorite(callback: CallbackQuery) -> None:
    upwork_job_id = (callback.data or "").removeprefix("save_")
    if not upwork_job_id:
        await callback.answer()
        return
    full = await db.get_job_full(upwork_job_id)
    if full is None:
        await callback.answer("Вакансия не найдена.")
        return
    await db.set_favorite(upwork_job_id, True)
    await _edit_to_desc_view(callback, upwork_job_id, full)
    await callback.answer("Добавлено в избранное.")


# --------------------------------------------------------------------------- #
# Inline-callback `desc_<id>` — C → B (live, switch back to desc-view)
# --------------------------------------------------------------------------- #
async def handle_show_description(callback: CallbackQuery) -> None:
    upwork_job_id = (callback.data or "").removeprefix("desc_")
    if not upwork_job_id:
        await callback.answer()
        return
    full = await db.get_job_full(upwork_job_id)
    if full is None:
        await callback.answer("Вакансия не найдена.")
        return
    await _edit_to_desc_view(callback, upwork_job_id, full)
    await callback.answer()


async def _edit_to_desc_view(
    callback: CallbackQuery, upwork_job_id: str, full: dict
) -> None:
    """Редактирует сообщение в формат [Заголовок]/[Описание]/[Вопросы] (state B)."""
    text = notifier.format_full_card(
        full.get("job_title") or "",
        full.get("job_description") or "",
        full.get("questions") or "",
    )
    primary, overflow = notifier.split_for_telegram(text)
    msg = callback.message
    if msg is None or not hasattr(msg, "edit_text"):
        return
    # Удаляем предыдущий overflow если был
    await notifier._delete_overflow(upwork_job_id)
    try:
        await msg.edit_text(
            primary,
            reply_markup=notifier.kb_live_desc_view(
                upwork_job_id, full.get("upwork_url") or ""
            ),
            parse_mode="HTML",
        )
    except TelegramAPIError:
        return
    if overflow:
        ovf_id = await notifier._safe_send_html(
            upwork_job_id=upwork_job_id,
            text=overflow,
            reply_markup=None,
            silent=True,
        )
        if ovf_id is not None:
            notifier._overflow_msgs[upwork_job_id] = ovf_id


# --------------------------------------------------------------------------- #
# Inline-callback `analysis_<id>` — B → C (live, switch to analysis view)
# --------------------------------------------------------------------------- #
async def handle_show_analysis_live(callback: CallbackQuery) -> None:
    upwork_job_id = (callback.data or "").removeprefix("analysis_")
    if not upwork_job_id:
        await callback.answer()
        return
    full = await db.get_job_full(upwork_job_id)
    if full is None:
        await callback.answer("Вакансия не найдена.")
        return
    await notifier._delete_overflow(upwork_job_id)
    msg = callback.message
    if msg is None or not hasattr(msg, "edit_text"):
        return
    try:
        await msg.edit_text(
            notifier.format_analysis(full.get("ai_analysis") or "Анализ недоступен."),
            reply_markup=notifier.kb_live_analysis_view(
                upwork_job_id, full.get("upwork_url") or ""
            ),
            parse_mode="HTML",
        )
    except TelegramAPIError:
        pass
    await callback.answer()


# --------------------------------------------------------------------------- #
# Inline-callback `subana_<id>` — submenu: title-only → analysis view
# --------------------------------------------------------------------------- #
async def handle_show_analysis_sub(callback: CallbackQuery) -> None:
    upwork_job_id = (callback.data or "").removeprefix("subana_")
    if not upwork_job_id:
        await callback.answer()
        return
    full = await db.get_job_full(upwork_job_id)
    if full is None:
        await callback.answer("Вакансия не найдена.")
        return
    msg = callback.message
    if msg is None or not hasattr(msg, "edit_text"):
        return
    try:
        await msg.edit_text(
            notifier.format_analysis(full.get("ai_analysis") or "Анализ недоступен."),
            reply_markup=notifier.kb_sub_analysis_view(
                upwork_job_id, full.get("upwork_url") or ""
            ),
            parse_mode="HTML",
        )
    except TelegramAPIError:
        pass
    await callback.answer()


# --------------------------------------------------------------------------- #
# Inline-callback `subtit_<id>` — submenu: analysis → title-only view
# --------------------------------------------------------------------------- #
async def handle_show_title_sub(callback: CallbackQuery) -> None:
    upwork_job_id = (callback.data or "").removeprefix("subtit_")
    if not upwork_job_id:
        await callback.answer()
        return
    full = await db.get_job_full(upwork_job_id)
    if full is None:
        await callback.answer("Вакансия не найдена.")
        return
    msg = callback.message
    if msg is None or not hasattr(msg, "edit_text"):
        return
    try:
        await msg.edit_text(
            notifier.format_title_only(full.get("job_title") or ""),
            reply_markup=notifier.kb_sub_title_view(
                upwork_job_id, full.get("upwork_url") or ""
            ),
            parse_mode="HTML",
        )
    except TelegramAPIError:
        pass
    await callback.answer()


# --------------------------------------------------------------------------- #
# Inline-callback `del_<id>` — снять fav + удалить сообщение
# --------------------------------------------------------------------------- #
async def handle_delete_favorite(callback: CallbackQuery) -> None:
    upwork_job_id = (callback.data or "").removeprefix("del_")
    if upwork_job_id:
        await db.set_favorite(upwork_job_id, False)
        await notifier._delete_overflow(upwork_job_id)
    msg = callback.message
    if msg is not None and hasattr(msg, "delete"):
        await msg.delete()
    await callback.answer("Удалено из избранного.")
