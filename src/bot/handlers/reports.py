"""Подменю Отчёт + Синхронизация (мгновенная выгрузка). См. ../BOT.md §10 + CHAT.md §7.

Отчёт теперь даёт два типа выгрузки:
    - Показать вакансии (N)  — drain manual-queue вакансий (классика)
    - Показать сообщения (M) — drain непоказанных chat_messages (CHAT.md §7.2)
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from src import db, notifier
from src.bot.keyboards import report_submenu_kb

if TYPE_CHECKING:
    from aiogram.types import Message


def _format_received_at(ts: object) -> str | None:
    """timestamptz → 'YYYY-MM-DD HH:MM'. None passthrough.

    Принимаем datetime от asyncpg, str-fallback если что-то странное вернётся.
    Используем getattr чтобы избежать узкой типизации на datetime (asyncpg row
    возвращает Any и mypy без аннотации не поверит в strftime).
    """
    if ts is None:
        return None
    strftime = getattr(ts, "strftime", None)
    if callable(strftime):
        return str(strftime("%Y-%m-%d %H:%M"))
    return str(ts)


async def handle_report(message: Message) -> None:
    """Reply-кнопка `Отчёт` → подменю Отчёт + дайджест очередей."""
    await db.set_paused_menu(True)
    job_rows = await db.peek_queued_by_reason("manual")
    n_jobs = len(job_rows)
    pool = db._conn()
    n_chat = await db.count_unshown_inbound_messages_cached(pool)

    if n_jobs == 0 and n_chat == 0:
        await message.answer(
            "Очередь Отчёта пуста.",
            reply_markup=report_submenu_kb(0, 0),
        )
        return

    text_lines: list[str] = []
    if n_jobs > 0:
        text_lines.append(f"В очереди {n_jobs} вакансий (накопились за паузу):")
        for r in job_rows[:10]:
            rating = r.get("rating") or 0
            title = (r.get("job_title") or "")[:40]
            country = r.get("client_country") or "?"
            budget = r.get("budget") or "?"
            text_lines.append(f"{rating:>2}  {title:<40}  ({country}, {budget})")
        if n_jobs > 10:
            text_lines.append(f"... (ещё {n_jobs - 10} с рейтингом ниже)")
    if n_chat > 0:
        if text_lines:
            text_lines.append("")
        text_lines.append(f"Сообщений от клиентов: {n_chat}")

    await message.answer(
        "\n".join(text_lines),
        reply_markup=report_submenu_kb(n_jobs, n_chat),
    )


# --------------------------------------------------------------------------- #
# Reply-кнопки подменю — drain
# --------------------------------------------------------------------------- #
async def handle_report_show_jobs(message: Message) -> None:
    """`Показать вакансии (N)` — drain manual + возврат в подменю Отчёт."""
    rows = await db.drain_queued_by_reason("manual")
    for row in rows:
        await notifier.send_job_from_row(row)
        await asyncio.sleep(0.05)
    await handle_report(message)


async def handle_report_show_messages(message: Message) -> None:
    """`Показать сообщения (M)` — выгрузка накопленных карточек диалогов
    (CHAT.md §7.2).

    Группирует drain'нутые chat_messages по email_thread_key и шлёт ОДНУ
    карточку на тред с последним in-сообщением. Если в треде есть AI-ответ
    (chat_ai_night_enabled был ВКЛ и AI ответил) — карточка показывает Q+A.
    Если AI был ВЫКЛ — карточка показывает просто входящее. Если AI был ВКЛ
    но эскалейтнул — карточка показывает причину почему не ответил.
    """
    rows = await db.drain_unshown_messages_for_report()
    if not rows:
        await message.answer("Новых сообщений нет.")
        await handle_report(message)
        return

    # Группируем по thread_key, сохраняя порядок появления
    threads: dict[bytes, list[dict]] = {}
    for r in rows:
        key = bytes(r["email_thread_key"])
        threads.setdefault(key, []).append(r)

    cards_shown = 0
    threads_with_escalate = 0  # AI пытался но не ответил (для footer warning)

    for thread_messages in threads.values():
        last_in = next(
            (m for m in reversed(thread_messages) if m["direction"] == "in"),
            None,
        )
        if last_in is None:
            continue

        last_out = next(
            (m for m in reversed(thread_messages) if m["direction"] == "out"),
            None,
        )
        escalate_reason = last_in.get("escalate_reason")
        if escalate_reason and last_out is None:
            threads_with_escalate += 1

        await notifier.send_qna_card(
            client_name=last_in.get("client_name") or "?",
            job_title=last_in.get("job_title"),
            job_url=last_in.get("job_url"),
            inbound_text=last_in.get("body_text") or "",
            inbound_at=_format_received_at(last_in.get("received_at")),
            ai_text=last_out.get("body_text") if last_out else None,
            ai_at=_format_received_at(last_out.get("sent_at")) if last_out else None,
            escalate_reason=escalate_reason,
        )
        cards_shown += 1
        await asyncio.sleep(0.05)

    # Footer: краткая сводка. Warning только если AI пытался и не справился.
    footer_parts = [f"✅ Готово, показано {cards_shown} сообщений"]
    if threads_with_escalate > 0:
        footer_parts.append(
            f"⚠️ Из них {threads_with_escalate} с пометкой что AI не справился — "
            f"посмотри причины в карточках"
        )
    await message.answer("\n\n".join(footer_parts))
    await handle_report(message)


async def handle_report_clear(message: Message) -> None:
    """`Очистить очередь` — пометить всё manual вакансий как sent без отправки.

    Chat-сообщения этим действием не трогаются (для них есть `Показать сообщения`).
    """
    n = await db.mark_queued_as_sent("manual")
    await message.answer(f"Очищено {n} вакансий.")
    await handle_report(message)


# --------------------------------------------------------------------------- #
# Синхронизация — мгновенная выгрузка menu-очереди (без подменю)
# --------------------------------------------------------------------------- #
async def handle_sync(message: Message) -> None:
    rows = await db.drain_queued_by_reason("menu")
    if not rows:
        await message.answer("Новых вакансий нет.")
        return
    for row in rows:
        await notifier.send_job_from_row(row)
        await asyncio.sleep(0.05)
    await db.set_paused_menu(False)


# Обратная совместимость: старое имя для router'а
handle_report_unload_all = handle_report_show_jobs
