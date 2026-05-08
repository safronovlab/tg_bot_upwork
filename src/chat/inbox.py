"""IMAP IDLE watcher: long-polling iCloud INBOX, оркестрация всего chat-flow.

См. CHAT.md §5, §6.

Lifecycle:
    1. Подключиться к imap.mail.me.com:993 SSL с credentials из db.get_chat_secret
    2. SELECT INBOX → запомнить last seen UID (или max при первом запуске)
    3. IDLE → ждём server-push о новых письмах
    4. На EXISTS event: SEARCH UNSEEN или FETCH UID > last_seen → обработать каждый
    5. Reconnect с exponential backoff при разрывах (1s → 2s → ... → 60s max)

Process per email:
    parser.parse_email
        ├── None (не Upwork) → skip
        ├── message_type != client_message → skip
        └── ParsedEmail
                ↓
        thread_resolver.resolve_thread_key + link_to_upwork_job
                ↓
        db.insert_inbound_message (UNIQUE on raw_email_uid → дубль skip)
                ↓
        notifier.send_inbound_alert (push card в TG, тон зависит от is_paused)
                ↓
        if is_paused AND chat_ai_night_enabled:
            escalate.pre_gate
                ├── Failed → mark_inbound_escalated, не вызываем AI
                └── Passed → dialog_ai.generate_reply
                        ├── None → mark_inbound_escalated('ai_failed')
                        └── text → escalate.post_validate
                                ├── Failed → mark_inbound_escalated(reason)
                                └── Passed → outbox.send_ai_reply (with delay)
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import aioimaplib

from src import config, db, log, notifier
from src.chat import dialog_ai, escalate, outbox, parser, thread_resolver

# --------------------------------------------------------------------------- #
# Reconnect / backoff параметры
# --------------------------------------------------------------------------- #
_INITIAL_BACKOFF_S = 1
_MAX_BACKOFF_S = 60
_IDLE_TIMEOUT_S = 29 * 60  # iCloud рвёт IDLE после 30 мин — рестартуем за 1 мин до


# --------------------------------------------------------------------------- #
# Per-email обработка (вызывается из IDLE event handler)
# --------------------------------------------------------------------------- #
async def _process_inbound_email(raw_bytes: bytes, uid: str) -> None:
    """Полный flow одного входящего email. Глотает все ошибки внутри (внешний
    loop не должен падать из-за одного битого письма)."""
    try:
        parsed = parser.parse_email(raw_bytes, raw_email_uid=uid)
    except Exception:
        await log.emit(
            "inbox_parse_failed",
            level=logging.WARNING,
            uid=uid,
            err="parser_exception",
        )
        return

    if parsed is None:
        # Не Upwork sender — игнорируем без шума
        return

    if parsed.message_type != parser.MESSAGE_TYPE_CLIENT_MESSAGE:
        # Hire / digest / application_sent — пока не наш case (Phase 6+)
        await log.emit(
            "inbox_skipped_non_message",
            level=logging.INFO,
            uid=uid,
            message_type=parsed.message_type,
        )
        return

    # Resolve thread + link to job
    thread_key = await thread_resolver.resolve_thread_key(
        in_reply_to=parsed.in_reply_to,
        client_name=parsed.from_name,
        job_title=parsed.job_title,
    )
    upwork_job_id = await thread_resolver.link_to_upwork_job(
        job_title=parsed.job_title,
        job_url=parsed.job_url,
    )

    # Insert (дубль по UID → ON CONFLICT DO NOTHING вернёт None)
    inserted_id = await db.insert_inbound_message(
        email_thread_key=thread_key,
        client_name=parsed.from_name,
        body_text=parsed.body_text or "(пустое тело — открой Upwork)",
        upwork_job_id=upwork_job_id,
        job_title=parsed.job_title,
        job_url=parsed.job_url,
        subject=parsed.subject,
        has_attachment=parsed.has_attachment,
        email_message_id=parsed.message_id,
        email_in_reply_to=parsed.in_reply_to,
        raw_email_uid=parsed.raw_email_uid,
    )

    if inserted_id is None:
        # Дубликат — IMAP вернул то же письмо при reconnect
        return

    await log.emit(
        "inbox_message_received",
        thread_key=thread_key.hex()[:16],
        client=parsed.from_name[:30],
        job=parsed.job_title[:60] if parsed.job_title else None,
        body_len=len(parsed.body_text),
    )

    # Day-mode push (loud) или night-mode push (silent)
    settings = await db.get_settings_cached()
    silent = settings.is_paused
    await notifier.send_inbound_alert(
        client_name=parsed.from_name,
        job_title=parsed.job_title,
        body_text=parsed.body_text,
        job_url=parsed.job_url,
        silent=silent,
    )

    # Только в night mode (is_paused=true) И при включенном AI ответе — генерим
    if not (settings.is_paused and settings.chat_ai_night_enabled):
        return

    # Pre-gate
    pre_reason = escalate.pre_gate(parsed.body_text)
    if pre_reason is not None:
        await db.mark_inbound_escalated(inserted_id, pre_reason)
        await log.emit(
            "night_reply_escalated",
            stage="pre_gate",
            reason=pre_reason,
            thread_key=thread_key.hex()[:16],
        )
        return

    # LLM
    ai_text = await dialog_ai.generate_reply(
        email_thread_key=thread_key,
        current_message=parsed.body_text,
    )
    if not ai_text:
        await db.mark_inbound_escalated(inserted_id, "llm_failed")
        await log.emit(
            "night_reply_escalated",
            stage="llm",
            reason="empty_response",
            thread_key=thread_key.hex()[:16],
        )
        return

    # Post-validator
    post_reason = escalate.post_validate(ai_text)
    if post_reason is not None:
        await db.mark_inbound_escalated(inserted_id, f"post_validate:{post_reason}")
        await log.emit(
            "night_reply_escalated",
            stage="post_validate",
            reason=post_reason,
            thread_key=thread_key.hex()[:16],
        )
        return

    # SMTP send (с задержкой и race-check'ами внутри)
    if not parsed.reply_to:
        await db.mark_inbound_escalated(inserted_id, "no_reply_to_token")
        return

    await outbox.send_ai_reply(
        email_thread_key=thread_key,
        body_text=ai_text,
        reply_to=parsed.reply_to,
        in_reply_to=parsed.message_id,
        subject=parsed.subject,
        client_name=parsed.from_name,
        upwork_job_id=upwork_job_id,
        job_title=parsed.job_title,
        job_url=parsed.job_url,
        ai_model=(await db.get_settings_cached()).analysis_model,
    )


# --------------------------------------------------------------------------- #
# IMAP connection + IDLE loop (одна сессия)
# --------------------------------------------------------------------------- #
async def _imap_session_once() -> None:
    """Одна полная IMAP-сессия: подключение + IDLE до timeout/разрыва.

    Поднимает исключение наружу — вызывающий run_imap_watcher применит backoff
    и переподключится.
    """
    user = await db.get_chat_secret("imap_user")
    password = await db.get_chat_secret("imap_password")
    if not user or not password:
        await log.emit(
            "imap_credentials_missing",
            level=logging.WARNING,
            has_user=bool(user),
            has_password=bool(password),
        )
        # Спим долго — нет смысла ретраить пока оператор не введёт credentials
        await asyncio.sleep(_MAX_BACKOFF_S)
        return

    client = aioimaplib.IMAP4_SSL(host=config.IMAP_HOST, port=config.IMAP_PORT)
    await client.wait_hello_from_server()

    try:
        await client.login(user, password)
        await client.select(config.IMAP_FOLDER)

        # При первом старте — определяем последний UID, дальше ловим только новые
        last_uid = await _get_last_known_uid(client)
        await log.emit(
            "imap_connected",
            level=logging.INFO,
            user=user,
            folder=config.IMAP_FOLDER,
            last_uid=last_uid,
        )

        while True:
            new_uids = await _fetch_new_uids(client, last_uid)
            for uid in new_uids:
                raw = await _fetch_uid(client, uid)
                if raw is not None:
                    await _process_inbound_email(raw, uid)
                # uid из IMAP — строка цифр; приводим к int для сравнения
                last_uid = int(uid)

            # IDLE — ждём server-push (или таймаут)
            try:
                idle_task = await client.idle_start(timeout=_IDLE_TIMEOUT_S)
            except AttributeError:
                # Старая aioimaplib API — fallback на простой sleep
                await asyncio.sleep(60)
                continue

            try:
                await asyncio.wait_for(idle_task, timeout=_IDLE_TIMEOUT_S)
            except TimeoutError:
                pass
            finally:
                client.idle_done()
    finally:
        # logout best-effort — никаких ошибок наружу, мы уже в finally
        import contextlib

        with contextlib.suppress(Exception):
            await client.logout()


async def _get_last_known_uid(client: Any) -> int:
    """Получить максимальный UID в INBOX. Используется для skip-already-seen
    при первом подключении (чтобы не ретриггерить уведомления).
    """
    response = await client.search("ALL")
    # response.lines = [b"1 2 3 ... 999"] — последний UID = max
    if not response or not getattr(response, "lines", None):
        return 0
    raw = response.lines[0]
    if isinstance(raw, bytes):
        raw = raw.decode("ascii", errors="ignore")
    parts = (raw or "").split()
    if not parts:
        return 0
    try:
        return max(int(p) for p in parts if p.isdigit())
    except ValueError:
        return 0


async def _fetch_new_uids(client: Any, last_uid: int) -> list[str]:
    """Найти UID > last_uid (письма пришедшие после нашего last seen)."""
    if last_uid <= 0:
        return []
    response = await client.search(f"UID {last_uid + 1}:*")
    if not response or not getattr(response, "lines", None):
        return []
    raw = response.lines[0]
    if isinstance(raw, bytes):
        raw = raw.decode("ascii", errors="ignore")
    parts = (raw or "").split()
    return [p for p in parts if p.isdigit() and int(p) > last_uid]


async def _fetch_uid(client: Any, uid: str) -> bytes | None:
    """Скачать raw bytes одного письма по UID."""
    try:
        response = await client.fetch(uid, "(RFC822)")
    except Exception as e:
        await log.emit(
            "imap_fetch_failed",
            level=logging.WARNING,
            uid=uid,
            err=str(e)[:100],
        )
        return None
    lines = getattr(response, "lines", None) or []
    # Структура: [b"1 FETCH (RFC822 {N}", b"<raw bytes>", b")", b"...OK..."]
    for line in lines:
        if isinstance(line, bytes) and len(line) > 200:
            return line
    return None


# --------------------------------------------------------------------------- #
# Public entrypoint — бесконечный loop с reconnect
# --------------------------------------------------------------------------- #
async def run_imap_watcher() -> None:
    """Бесконечный IMAP IDLE цикл с exponential backoff на разрывах.

    Запускается из cron.start_cron как отдельная asyncio.Task. Глотает все
    исключения кроме CancelledError (для graceful shutdown).
    """
    backoff = _INITIAL_BACKOFF_S
    while True:
        try:
            await _imap_session_once()
            # Сессия завершилась штатно (timeout) — можно сразу переподключаться
            backoff = _INITIAL_BACKOFF_S
        except asyncio.CancelledError:
            await log.emit("imap_watcher_cancelled")
            raise
        except Exception as e:
            await log.emit(
                "imap_connection_lost",
                level=logging.WARNING,
                err=type(e).__name__,
                detail=str(e)[:200],
                backoff_s=backoff,
            )
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, _MAX_BACKOFF_S)
