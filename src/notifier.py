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
    return InlineKeyboardButton(text="Открыть", url=url or "https://www.upwork.com/")


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
def _fmt_budget(job: Job) -> str:
    """Ставка/бюджет из вакансии: '$60-90/час' или '$500'."""
    if not job.budget:
        return ""
    suffix = "/час" if (job.budget_type or "").lower().startswith("hour") else ""
    return f"{job.budget}{suffix}"


def _fmt_client(job: Job) -> str:
    """Сводка по клиенту: страна · потрачено · наймы · рейтинг (что есть)."""
    parts: list[str] = []
    if job.client_country:
        parts.append(job.client_country)
    if job.client_total_spent:
        parts.append(f"${job.client_total_spent:,.0f} потрач.")
    if job.client_total_hires is not None:
        parts.append(f"{job.client_total_hires} наймов")
    if job.client_rating:
        parts.append(f"{job.client_rating:.2f}★")
    return " · ".join(parts)


# Цвет-маркер по баллу — единственный эмодзи в карточке. Цвета = редкость предметов
# в World of Warcraft (по возрастанию): Poor(серый) → Common(белый) → Uncommon(зелёный)
# → Rare(синий) → Epic(фиолетовый) → Legendary(оранжевый). В попапе Telegram сразу
# видно «редкость» лида по цвету. (Серого кружка в эмодзи нет — Poor показываем ⚫.)
_RATING_EMOJI: dict[int, str] = {
    10: "🟠",  # Legendary
    9: "🟣",   # Epic
    8: "🔵",   # Rare
    7: "🟢",   # Uncommon
    6: "⚪",   # Common
    5: "⚪",   # Common
}


def _rating_emoji(rating: int) -> str:
    """Цвет-маркер рейтинга по редкости WoW. ≤4 → ⚫ (Poor / серый)."""
    return _RATING_EMOJI.get(rating, "⚫")


def render_analysis_card(parsed: dict, job: Job) -> str:
    """Собирает decision-first текст карточки из JSON-полей анализа + фактов вакансии.

    Эмодзи только у рейтинга (см. _rating_emoji); остальные строки — без иконок.
    Возвращает ПЛЭЙН-текст (без HTML-тегов) — escape делает слой отправки
    (format_analysis → _safe_send_html). Хранится как ai_analysis, поэтому все
    места показа (live-карточка/очередь/избранное) работают без изменений.
    """
    rating = round(float(parsed.get("rating") or 0))
    lines = [f"{_rating_emoji(rating)} РЕЙТИНГ {rating}"]
    if parsed.get("summary"):
        lines.append(f"📝 {parsed['summary']}")
    budget = _fmt_budget(job)
    if budget:
        lines.append(f"💰 {budget}")
    if parsed.get("stack_match"):
        lines.append(f"🧩 {parsed['stack_match']}")
    client = _fmt_client(job)
    if client:
        lines.append(f"👤 {client}")
    risks = parsed.get("risks") or []
    if risks:
        lines.append(f"⚠️ {'; '.join(risks)}")
    if parsed.get("reason"):
        lines.append(f"💬 {parsed['reason']}")
    return "\n\n".join(lines)  # пустая строка между пунктами


def _build_state_a_text(title: str, analysis: str) -> str:
    """State A (initial delivered card): РЕЙТИНГ (первая строка карточки) → ЗАГОЛОВОК
    → остальной разбор. Заголовок вставляется после первой строки analysis (это
    строка рейтинга у render_analysis_card). HTML-escape обязателен."""
    title_html = f"💼 <b>{html.escape((title or '').upper())}</b>"
    # рейтинг — первый блок; делим по пустой строке (пункты разделены \n\n)
    parts = (analysis or "").split("\n\n", 1)
    head = format_analysis(parts[0])  # строка рейтинга (цветная шапка)
    if len(parts) == 1:
        return f"{head}\n{title_html}"
    # рейтинг, заголовок, ПУСТАЯ строка, затем пункты (каждый через пустую строку)
    return f"{head}\n{title_html}\n\n{format_analysis(parts[1])}"


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


# --------------------------------------------------------------------------- #
# Chat-подсистема — карточки входящих/Q+A. См. CHAT.md §7.
# --------------------------------------------------------------------------- #
_DEFAULT_UPWORK_MESSAGES_URL = "https://www.upwork.com/messages/"


def _chat_card_kb(job_url: str | None) -> InlineKeyboardMarkup:
    """Inline-кнопка `🔗 Открыть чат в Upwork` под chat-карточкой."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Открыть чат в Upwork",
                    url=job_url or _DEFAULT_UPWORK_MESSAGES_URL,
                )
            ]
        ]
    )


def _truncate(text: str, limit: int) -> str:
    """Обрезать text до limit chars с многоточием."""
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def format_inbound_card(
    *,
    client_name: str,
    job_title: str | None,
    body_text: str,
    received_at: str | None = None,
) -> str:
    """Карточка входящего сообщения: 💼/👤/🕐 заголовок + body. CHAT.md §7.4.

    HTML-escape всего пользовательского контента.
    """
    parts: list[str] = []
    if job_title:
        parts.append(f"💼 <b>{html.escape(_truncate(job_title, 80))}</b>")
    parts.append(f"👤 {html.escape(_truncate(client_name, 60))}")
    if received_at:
        parts.append(f"🕐 {html.escape(received_at)}")
    body_preview = _truncate((body_text or "").strip(), 800)
    if body_preview:
        parts.append("")
        parts.append(html.escape(body_preview))
    return "\n".join(parts)


def format_qna_card(
    *,
    client_name: str,
    job_title: str | None,
    inbound_text: str,
    inbound_at: str | None,
    ai_text: str | None,
    ai_at: str | None,
    escalate_reason: str | None = None,
) -> str:
    """Карточка диалога для digest «Показать сообщения». CHAT.md §7.3.

    3 режима в зависимости от наличия ai_text + escalate_reason:
        ai_text есть                  → Q+A карточка (клиент + AI-ответ)
        ai_text=None, escalate_reason → Карточка с пометкой почему AI не ответил
        ai_text=None, escalate=None   → Просто карточка входящего (AI был выключен,
                                        даже не пытался ответить)
    """
    parts: list[str] = []
    if job_title:
        parts.append(f"💼 <b>{html.escape(_truncate(job_title, 80))}</b>")
    parts.append(f"👤 {html.escape(_truncate(client_name, 60))}")
    if inbound_at:
        parts.append(f"🕐 {html.escape(inbound_at)}")
    parts.append("")
    parts.append("📥 <i>Клиент:</i>")
    parts.append(html.escape(_truncate(inbound_text or "", 800)))

    if ai_text:
        parts.append("")
        ai_label = f"📤 <i>AI ответил{' (' + html.escape(ai_at) + ')' if ai_at else ''}:</i>"
        parts.append(ai_label)
        parts.append(html.escape(_truncate(ai_text, 800)))
    elif escalate_reason:
        parts.append("")
        parts.append(
            f"⚠️ <i>AI не ответил, причина: {html.escape(_truncate(escalate_reason, 80))}</i>"
        )
    # Если ai_text=None и escalate_reason=None — AI просто был выключен,
    # никаких пометок не нужно (это нормальная карточка входящего сообщения).

    return "\n".join(parts)


async def send_inbound_alert(
    *,
    client_name: str,
    job_title: str | None,
    body_text: str,
    job_url: str | None,
    silent: bool,
) -> None:
    """Push-уведомление в TG о новом сообщении от клиента (CHAT.md §7.4).

    silent=True (is_paused → ночной режим) — без звука.
    silent=False (day mode) — громко.
    """
    text = format_inbound_card(
        client_name=client_name,
        job_title=job_title,
        body_text=body_text,
    )
    primary, overflow = split_for_telegram(text)
    await _safe_send_html(
        upwork_job_id=f"chat:{client_name[:32]}",
        text=primary,
        reply_markup=_chat_card_kb(job_url),
        silent=silent,
    )
    if overflow:
        await _safe_send_html(
            upwork_job_id=f"chat:{client_name[:32]}",
            text=overflow,
            reply_markup=None,
            silent=True,
        )


async def send_qna_card(
    *,
    client_name: str,
    job_title: str | None,
    job_url: str | None,
    inbound_text: str,
    inbound_at: str | None,
    ai_text: str | None,
    ai_at: str | None,
    escalate_reason: str | None = None,
) -> None:
    """Карточка диалога для digest в Отчёте (CHAT.md §7.3). Тихая отправка."""
    text = format_qna_card(
        client_name=client_name,
        job_title=job_title,
        inbound_text=inbound_text,
        inbound_at=inbound_at,
        ai_text=ai_text,
        ai_at=ai_at,
        escalate_reason=escalate_reason,
    )
    primary, overflow = split_for_telegram(text)
    await _safe_send_html(
        upwork_job_id=f"chat:{client_name[:32]}",
        text=primary,
        reply_markup=_chat_card_kb(job_url),
        silent=True,
    )
    if overflow:
        await _safe_send_html(
            upwork_job_id=f"chat:{client_name[:32]}",
            text=overflow,
            reply_markup=None,
            silent=True,
        )
