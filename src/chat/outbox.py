"""SMTP отправка ответов клиенту через iCloud → mg.upwork.com Reply-To. См. CHAT.md §6.

Race-условия защиты перед SMTP-send:
  1. is_paused вернулось в False → оператор разбудился, AI отменяет задачу
  2. has_recent_human_outbound → оператор сам ответил, AI не дублирует
  3. chat_ai_night_enabled выключен → AI не должен отправлять (даже если задача уже в очереди)
"""

from __future__ import annotations

import logging
import random
from datetime import UTC, datetime
from email.message import EmailMessage

import aiosmtplib

from src import config, db, log

# Анти-race окно: проверяем активность оператора в последние N секунд.
# Сделано шире чем delay_max — если оператор написал 3 минуты назад, мы не лезем.
_HUMAN_ACTIVITY_WINDOW_S = 300


async def _build_message(
    *,
    smtp_user: str,
    reply_to_token: str,
    subject: str,
    body_text: str,
    in_reply_to: str | None,
) -> EmailMessage:
    """RFC 822 message с правильным subject (Re: ...) и In-Reply-To header."""
    msg = EmailMessage()
    msg["From"] = smtp_user
    msg["To"] = reply_to_token
    msg["Subject"] = subject if subject.lower().startswith("re:") else f"Re: {subject}"
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = in_reply_to
    msg.set_content(body_text)
    return msg


async def _smtp_send(msg: EmailMessage) -> bool:
    """Низкоуровневая SMTP-отправка через iCloud. True на успехе."""
    smtp_user = await db.get_chat_secret("smtp_user")
    smtp_password = await db.get_chat_secret("smtp_password")

    if not smtp_user or not smtp_password:
        await log.emit(
            "smtp_send_failed",
            level=logging.WARNING,
            reason="credentials_missing",
        )
        return False

    try:
        await aiosmtplib.send(
            msg,
            hostname=config.SMTP_HOST,
            port=config.SMTP_PORT,
            username=smtp_user,
            password=smtp_password,
            start_tls=True,
            timeout=30,
        )
        return True
    except (aiosmtplib.SMTPException, TimeoutError, OSError) as e:
        await log.emit(
            "smtp_send_failed",
            level=logging.WARNING,
            reason=type(e).__name__,
            err=str(e)[:200],
        )
        return False


def _pick_delay(min_s: int, max_s: int) -> int:
    """Случайная задержка в диапазоне [min_s, max_s]. Защита от палева бота."""
    if min_s <= 0 and max_s <= 0:
        return 0
    lo = max(0, min_s)
    hi = max(lo, max_s)
    if lo == hi:
        return lo
    return random.randint(lo, hi)  # noqa: S311  # не криптография — anti-bot timing


async def _race_check_safe_to_send(email_thread_key: bytes) -> tuple[bool, str | None]:
    """Перед SMTP send: проверить что условия для AI-ответа всё ещё валидны.

    Returns: (safe_to_send, abort_reason).
    """
    settings = await db.get_settings_cached()
    if not settings.is_paused:
        return False, "operator_resumed"
    if not settings.chat_ai_night_enabled:
        return False, "ai_disabled"

    if await db.has_recent_human_outbound(email_thread_key, _HUMAN_ACTIVITY_WINDOW_S):
        return False, "human_already_replied"

    return True, None


async def send_ai_reply(
    *,
    email_thread_key: bytes,
    body_text: str,
    reply_to: str,
    in_reply_to: str | None,
    subject: str,
    client_name: str,
    upwork_job_id: int | None,
    job_title: str | None,
    job_url: str | None,
    ai_model: str | None,
) -> bool:
    """Полный flow отправки AI-ответа: задержка → race check → SMTP → DB.

    Returns: True если отправлено успешно (есть запись в chat_messages).
    """
    import asyncio

    settings = await db.get_settings_cached()
    delay = _pick_delay(
        settings.chat_ai_delay_min_seconds,
        settings.chat_ai_delay_max_seconds,
    )

    if delay > 0:
        await asyncio.sleep(delay)

    safe, abort_reason = await _race_check_safe_to_send(email_thread_key)
    if not safe:
        await log.emit(
            "night_reply_aborted",
            reason=abort_reason or "unknown",
            thread_key=email_thread_key.hex()[:16],
        )
        return False

    smtp_user = await db.get_chat_secret("smtp_user")
    if not smtp_user:
        await log.emit("night_reply_aborted", reason="no_smtp_user")
        return False

    msg = await _build_message(
        smtp_user=smtp_user,
        reply_to_token=reply_to,
        subject=subject or "Re: Upwork message",
        body_text=body_text,
        in_reply_to=in_reply_to,
    )

    sent_ok = await _smtp_send(msg)
    if not sent_ok:
        return False

    sent_at = datetime.now(UTC)
    await db.insert_outbound_message(
        email_thread_key=email_thread_key,
        client_name=client_name,
        body_text=body_text,
        ai_generated=True,
        ai_model=ai_model,
        upwork_job_id=upwork_job_id,
        job_title=job_title,
        job_url=job_url,
        subject=subject,
        email_in_reply_to=in_reply_to,
        sent_at=sent_at,
    )
    await log.emit(
        "night_reply_sent",
        thread_key=email_thread_key.hex()[:16],
        delay_s=delay,
        body_len=len(body_text),
    )
    return True
