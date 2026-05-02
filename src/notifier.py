"""Отправка карточек вакансий в Telegram + four view-states. См. bot/BOT.md §9."""

from __future__ import annotations

import html
import logging
from typing import TYPE_CHECKING

from aiogram.exceptions import TelegramAPIError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from src import config, log
from src.models import Job

if TYPE_CHECKING:
    from aiogram import Bot


bot: Bot | None = None

DEFAULT_LOUD_THRESHOLD = 8

# Telegram сообщение: лимит 4096 символов. Оставляем запас под HTML-теги.
TG_MSG_LIMIT = 4000

# Трекинг overflow-сообщения (длинные вопросы вылезают во второе сообщение).
# upwork_job_id → message_id «продолжения», чтобы удалить его на toggle/del.
# Single-user single-chat — простой dict в памяти достаточно.
_overflow_msgs: dict[str, int] = {}


def set_bot(bot_instance: Bot) -> None:
    global bot
    bot = bot_instance


def _resolve_chat_id() -> int:
    if config.ALLOWED_USER_IDS:
        return next(iter(config.ALLOWED_USER_IDS))
    return 0


# --------------------------------------------------------------------------- #
# Форматирование текста — HTML-escape + <code> для тап-копирования
# --------------------------------------------------------------------------- #
def _code_block(content: str) -> str:
    """HTML-escape + обёртка в <code> (тап-копирование в Telegram)."""
    return f"<code>{html.escape(content)}</code>"


def format_full_card(title: str, description: str, questions: str | None) -> str:
    """[Заголовок]/[Описание]/[Вопросы] — для live карточки в избранном (state B).

    Каждое поле — копируемый <code>-блок. HTML-escape обязателен, иначе
    спец-символы (`<`, `>`, `&`) ломают парсер Telegram.
    """
    parts = [f"[Заголовок]\n{_code_block(title or '')}"]
    if description:
        parts.append(f"[Описание]\n{_code_block(description)}")
    if questions:
        parts.append(f"[Вопросы]\n{_code_block(questions)}")
    return "\n\n".join(parts)


def format_title_only(title: str) -> str:
    """Submenu favorites — только заголовок, копируемый. State `sub-title`."""
    return _code_block(title or "")


def format_analysis(analysis: str) -> str:
    """Анализ — без обёртки в <code> (он уже в свободной форме, копировать целиком
    обычно не нужно). HTML-escape обязателен.
    """
    return html.escape(analysis or "")


def split_for_telegram(text: str) -> tuple[str, str | None]:
    """Если текст помещается в TG_MSG_LIMIT — `(text, None)`. Иначе — режем по
    границе секций `\\n\\n` так, чтобы первая часть была <= лимита.

    Примечание: внутри <code>...</code> мы НЕ режем — секции уже самостоятельны.
    """
    if len(text) <= TG_MSG_LIMIT:
        return text, None
    cut = text.rfind("\n\n", 0, TG_MSG_LIMIT)
    if cut <= 0:
        # Нет подходящей границы — жёсткий срез
        cut = TG_MSG_LIMIT
        return text[:cut], text[cut:]
    return text[:cut], text[cut + 2 :]


# --------------------------------------------------------------------------- #
# Билдеры inline-клавиатур (BOT.md §9)
# --------------------------------------------------------------------------- #
def _upwork_btn(url: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text="Открыть на Upwork", url=url or "https://www.upwork.com/")


def kb_unfavorited(upwork_job_id: str, url: str) -> InlineKeyboardMarkup:
    """State A: live, не в избранном. `[Open Upwork] [Избранное]`."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [_upwork_btn(url), InlineKeyboardButton(
                text="Избранное", callback_data=f"save_{upwork_job_id}"
            )],
        ]
    )


def kb_live_desc_view(upwork_job_id: str, url: str) -> InlineKeyboardMarkup:
    """State B: live, в избранном, режим `Описание`. Кнопка → `Анализ`."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [_upwork_btn(url), InlineKeyboardButton(
                text="Анализ", callback_data=f"analysis_{upwork_job_id}"
            )],
        ]
    )


def kb_live_analysis_view(upwork_job_id: str, url: str) -> InlineKeyboardMarkup:
    """State C: live, в избранном, режим `Анализ`. Кнопка → `Описание`."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [_upwork_btn(url), InlineKeyboardButton(
                text="Описание", callback_data=f"desc_{upwork_job_id}"
            )],
        ]
    )


def kb_sub_title_view(upwork_job_id: str, url: str) -> InlineKeyboardMarkup:
    """Submenu favorites, режим `Заголовок`. `[Open Upwork] [Анализ]` + Удалить."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [_upwork_btn(url), InlineKeyboardButton(
                text="Анализ", callback_data=f"subana_{upwork_job_id}"
            )],
            [InlineKeyboardButton(
                text="Удалить из избранного", callback_data=f"del_{upwork_job_id}"
            )],
        ]
    )


def kb_sub_analysis_view(upwork_job_id: str, url: str) -> InlineKeyboardMarkup:
    """Submenu favorites, режим `Анализ`. `[Open Upwork] [Заголовок]` + Удалить."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [_upwork_btn(url), InlineKeyboardButton(
                text="Заголовок", callback_data=f"subtit_{upwork_job_id}"
            )],
            [InlineKeyboardButton(
                text="Удалить из избранного", callback_data=f"del_{upwork_job_id}"
            )],
        ]
    )


# --------------------------------------------------------------------------- #
# Низкоуровневые отправители
# --------------------------------------------------------------------------- #
async def _safe_send_html(
    *,
    upwork_job_id: str,
    text: str,
    reply_markup: InlineKeyboardMarkup | None,
    silent: bool,
) -> int | None:
    """send_message с parse_mode=HTML и swallow Telegram-ошибок. Возвращает msg_id."""
    if bot is None:
        return None
    try:
        sent = await bot.send_message(
            chat_id=_resolve_chat_id(),
            text=text,
            reply_markup=reply_markup,
            disable_notification=silent,
            parse_mode="HTML",
        )
        return getattr(sent, "message_id", None)
    except TelegramAPIError as e:
        await log.emit(
            "telegram_send_failed",
            level=logging.WARNING,
            upwork_job_id=upwork_job_id,
            err=str(e)[:200],
        )
        return None


async def _send_with_overflow(
    *,
    upwork_job_id: str,
    text: str,
    reply_markup: InlineKeyboardMarkup,
    silent: bool,
) -> None:
    """Отправляет основное сообщение + если текст слишком длинный — overflow-продолжение.

    Overflow-msg-id трекается в `_overflow_msgs` чтобы удалить при toggle/del.
    """
    primary, overflow = split_for_telegram(text)
    await _safe_send_html(
        upwork_job_id=upwork_job_id,
        text=primary,
        reply_markup=reply_markup,
        silent=silent,
    )
    if overflow:
        ovf_id = await _safe_send_html(
            upwork_job_id=upwork_job_id,
            text=overflow,
            reply_markup=None,
            silent=True,
        )
        if ovf_id is not None:
            _overflow_msgs[upwork_job_id] = ovf_id


async def _delete_overflow(upwork_job_id: str) -> None:
    """Удаляет привязанное overflow-сообщение (если есть) — для toggle/del."""
    msg_id = _overflow_msgs.pop(upwork_job_id, None)
    if msg_id is None or bot is None:
        return
    try:
        await bot.delete_message(chat_id=_resolve_chat_id(), message_id=msg_id)
    except TelegramAPIError:
        pass


# --------------------------------------------------------------------------- #
# Публичные API: отправка карточек
# --------------------------------------------------------------------------- #
def _build_state_a_text(title: str, analysis: str) -> str:
    """State A (initial delivered card): анализ сверху, ЗАГОЛОВОК под ним. HTML-escape."""
    title_html = html.escape((title or "").upper())
    return f"{format_analysis(analysis)}\n\n<b>{title_html}</b>"


async def send_job(job: Job, analysis: str, *, silent: bool) -> None:
    """Доставка свеже-проанализированной вакансии — state A (не в избранном)."""
    text = _build_state_a_text(job.job_title or "", analysis)
    await _safe_send_html(
        upwork_job_id=job.upwork_job_id,
        text=text,
        reply_markup=kb_unfavorited(job.upwork_job_id, job.upwork_url or ""),
        silent=silent,
    )


async def send_job_from_row(row: dict) -> None:
    """Отправка вакансии из выгрузки очереди — state A (BOT.md §10)."""
    upwork_job_id = row.get("upwork_job_id", "") or ""
    title = row.get("job_title", "") or ""
    url = row.get("upwork_url", "") or ""
    analysis = row.get("ai_analysis", "") or ""
    silent = (row.get("rating") or 0) < DEFAULT_LOUD_THRESHOLD
    text = _build_state_a_text(title, analysis)
    await _safe_send_html(
        upwork_job_id=upwork_job_id,
        text=text,
        reply_markup=kb_unfavorited(upwork_job_id, url),
        silent=silent,
    )


async def send_favorite_card(row: dict) -> None:
    """Карточка в submenu Избранное — title-only + sub-кнопки. Тихая отправка."""
    upwork_job_id = row.get("upwork_job_id", "") or ""
    title = row.get("job_title", "") or ""
    url = row.get("upwork_url", "") or ""
    await _safe_send_html(
        upwork_job_id=upwork_job_id,
        text=format_title_only(title),
        reply_markup=kb_sub_title_view(upwork_job_id, url),
        silent=True,
    )
