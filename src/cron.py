"""6 asyncio.create_task циклов: recovery, cleanup, retention, alert. См. PIPELINE.md §9."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from src import config, log

if TYPE_CHECKING:
    from aiogram import Bot
    from asyncpg import Pool


# Глобальные — выставляются из main.py через set_bot() и из config (ARCHITECTURE.md §5.2).
bot: Bot | None = None
ALLOWED_USER_IDS: list[int] = list(config.ALLOWED_USER_IDS)

# Хранилище ссылок на cron-задачи, чтобы их не съел GC (RUF006).
_tasks: set[asyncio.Task] = set()


def set_bot(bot_instance: Bot) -> None:
    global bot
    bot = bot_instance


async def _loop(coro: Callable[[], Awaitable[None]], period_s: int) -> None:
    """Бесконечный цикл с глотанием исключений (кроме CancelledError)."""
    while True:
        try:
            await coro()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.log.exception("cron_failed")
        await asyncio.sleep(period_s)


def _bind(fn: Callable[[Pool], Awaitable[None]], pool: Pool) -> Callable[[], Awaitable[None]]:
    """Каррирование fn(pool) → callable без аргументов для _loop()."""

    async def _bound() -> None:
        await fn(pool)

    return _bound


def start_cron(pool: Pool) -> None:
    """Запускает 6 фоновых циклов (PIPELINE.md §9) + IMAP IDLE watcher (CHAT.md §5).

    IMAP-watcher — НЕ периодическая задача (period=0); внутри он сам держит
    long-poll IDLE и переподключается с exponential backoff. Запускается как
    отдельный create_task без _loop wrapper.
    """
    schedule: list[tuple[Callable[[Pool], Awaitable[None]], int]] = [
        (recover_stuck_jobs, 600),
        (compact_and_cleanup_jobs, 86400),
        (cleanup_inbox, 3600),
        (cleanup_events, 86400),
        (prompts_history_trim, 86400),
        (alert_error_burst, 900),
    ]
    for fn, period in schedule:
        task = asyncio.create_task(_loop(_bind(fn, pool), period))
        _tasks.add(task)
        task.add_done_callback(_tasks.discard)

    # IMAP IDLE watcher (CHAT.md §5). Сам держит state, не нужен периодический wrapper.
    from src.chat.inbox import run_imap_watcher

    imap_task = asyncio.create_task(run_imap_watcher())
    _tasks.add(imap_task)
    imap_task.add_done_callback(_tasks.discard)


# --------------------------------------------------------------------------- #
# Cron-задачи (PIPELINE.md §9)
# --------------------------------------------------------------------------- #
async def recover_stuck_jobs(pool: Pool) -> None:
    """Подбирает вакансии застрявшие в pending/pre_screened > 10 минут."""
    rows = await pool.fetch(
        """
        UPDATE upwork_jobs
        SET attempts = attempts + 1
        WHERE processing_state IN ('pending', 'pre_screened')
          AND updated_at < now() - interval '10 minutes'
          AND attempts < 3
        RETURNING upwork_job_id;
        """
    )
    if rows:
        await log.emit(
            "recovery_triggered",
            count=len(rows),
            job_ids=[r["upwork_job_id"] for r in rows[:10]],
        )

    # После 3 attempts — в dead-letter
    await pool.execute(
        """
        UPDATE upwork_jobs SET processing_state = 'failed',
            last_error = 'stuck_recovery_exceeded'
        WHERE processing_state IN ('pending', 'pre_screened')
          AND updated_at < now() - interval '10 minutes'
          AND attempts >= 3;
        """
    )


async def compact_and_cleanup_jobs(pool: Pool) -> None:
    """Retention (PIPELINE.md §8). Политика: «нет истории».
    - Доставленные НЕ-избранные → удалить через 7 дней (успеваешь добавить в избранное).
    - Избранное (is_favorite=true) → хранится бессрочно.
    - failed → 14 дней. Зависшая ночная очередь (analyzed+queued) → 30 дней.
    """
    await pool.execute(
        """
        DELETE FROM upwork_jobs
        WHERE is_sent = true AND is_favorite = false
          AND created_at < now() - interval '7 days';
        """
    )
    await pool.execute(
        """
        DELETE FROM upwork_jobs
        WHERE processing_state = 'failed'
          AND created_at < now() - interval '14 days';
        """
    )
    await pool.execute(
        """
        DELETE FROM upwork_jobs
        WHERE processing_state = 'analyzed'
          AND queued_reason IS NOT NULL AND is_favorite = false
          AND created_at < now() - interval '30 days';
        """
    )


async def cleanup_inbox(pool: Pool) -> None:
    await pool.execute("DELETE FROM webhook_inbox WHERE received_at < now() - interval '7 days'")


async def cleanup_events(pool: Pool) -> None:
    await pool.execute("DELETE FROM bot_events WHERE ts < now() - interval '7 days'")


async def prompts_history_trim(pool: Pool) -> None:
    """Оставлять последние 50 записей на слот."""
    await pool.execute(
        """
        DELETE FROM prompts_history
        WHERE id IN (
          SELECT id FROM (
            SELECT id, row_number() OVER (PARTITION BY slot ORDER BY edited_at DESC) AS rn
            FROM prompts_history
          ) t
          WHERE rn > 50
        );
        """
    )


async def alert_error_burst(pool: Pool) -> None:
    """Если >= 5 событий level=error за 15 мин — отправить оператору сообщение."""
    n = await pool.fetchval(
        """
        SELECT COUNT(*) FROM bot_events
        WHERE level = 2 AND ts > now() - interval '15 minutes'
        """
    )
    if (n or 0) >= 5 and bot is not None and ALLOWED_USER_IDS:
        await bot.send_message(
            ALLOWED_USER_IDS[0],
            f"Внимание: за 15 минут зафиксировано {n} ошибок. Проверьте Логи.",
        )
