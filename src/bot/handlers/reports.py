"""Подменю Отчёт + Синхронизация (мгновенная выгрузка). См. ../BOT.md §10."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from src import db, notifier
from src.bot.keyboards import report_submenu_kb

if TYPE_CHECKING:
    from aiogram.types import Message


async def handle_report(message: Message) -> None:
    """Reply-кнопка `Отчёт` → подменю Отчёт + дайджест очереди (BOT.md §10)."""
    await db.set_paused_menu(True)
    rows = await db.peek_queued_by_reason("manual")
    n = len(rows)
    if n == 0:
        await message.answer(
            "Очередь Отчёта пуста.",
            reply_markup=report_submenu_kb(0),
        )
        return
    text_lines = [f"В очереди {n} вакансий (накопились за паузу):", ""]
    for r in rows[:10]:
        rating = r.get("rating") or 0
        title = (r.get("job_title") or "")[:40]
        country = r.get("client_country") or "?"
        budget = r.get("budget") or "?"
        text_lines.append(f"{rating:>2}  {title:<40}  ({country}, {budget})")
    if n > 10:
        text_lines.append(f"... (ещё {n - 10} с рейтингом ниже)")
    await message.answer(
        "\n".join(text_lines),
        reply_markup=report_submenu_kb(n),
    )


# --------------------------------------------------------------------------- #
# Reply-кнопки подменю — drain manual-очереди
# --------------------------------------------------------------------------- #
async def handle_report_unload_all(message: Message) -> None:
    """`Выгрузить все [N]` — drain manual + остаёмся в подменю Отчёт."""
    rows = await db.drain_queued_by_reason("manual")
    for row in rows:
        await notifier.send_job_from_row(row)
        await asyncio.sleep(0.05)
    # Возврат в подменю Отчёт (теперь очередь пуста, дайджест покажет соответственно)
    await handle_report(message)


async def handle_report_clear(message: Message) -> None:
    """`Очистить очередь` — пометить всё manual как sent без отправки.

    Остаёмся в подменю Отчёт — пользователь сам выйдет через Назад.
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
