"""Entrypoint: lifespan, gc.freeze(), запуск aiohttp + aiogram. См. ../ARCHITECTURE.md §5."""

from __future__ import annotations

import asyncio
import contextlib
import gc
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import aiohttp
import asyncpg

try:
    import uvloop
except ImportError:  # uvloop недоступен на Windows
    uvloop = None

from src import config, cron, db, http_app, llm, log, migrations, notifier
from src.bot import app as bot_app

GRACEFUL_SHUTDOWN_TIMEOUT_S = 30


@asynccontextmanager
async def lifespan() -> AsyncIterator[None]:
    pool = await asyncpg.create_pool(
        config.DATABASE_URL,
        min_size=2,
        max_size=10,
        command_timeout=10,
        max_inactive_connection_lifetime=300,
    )
    await migrations.init_schema(pool)
    await db.init(pool)

    http_session = aiohttp.ClientSession()
    llm.set_session(http_session)

    bot, dp = bot_app.build(http_session)
    notifier.set_bot(bot)
    cron.set_bot(bot)

    cron.start_cron(pool)
    runner = await http_app.start(bot, dp, port=8080)

    # aiogram polling — единственный legitimate way отвечать на сообщения (ARCHITECTURE.md §5.2)
    polling_task = asyncio.create_task(dp.start_polling(bot, handle_signals=False))

    # gc.freeze() убирает базовые объекты из GC-цикла (ARCHITECTURE.md §5.2)
    gc.collect()
    gc.freeze()

    try:
        yield
    finally:
        await _graceful_shutdown(dp, polling_task, runner, pool, http_session, bot)


async def _graceful_shutdown(
    dp: object,
    polling_task: asyncio.Task,
    runner: object,
    pool: object,
    http_session: aiohttp.ClientSession,
    bot: object,
) -> None:
    """Drain in-flight tasks → close pools → cleanup HTTP runner (ARCHITECTURE.md §5.3)."""
    # 1. webhook начинает отвечать 503
    http_app.set_shutting_down(True)

    # 2. Останавливаем polling. stop_polling() кидает RuntimeError если оно никогда
    # не стартовало (fake token, network error при первом polling-цикле и т.п.).
    with contextlib.suppress(RuntimeError):
        await dp.stop_polling()  # type: ignore[attr-defined]
    polling_task.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await polling_task

    # 3. Drain in-flight pipeline-задач (PIPELINE.md §3 / ARCHITECTURE.md §5.3)
    in_flight = list(http_app._tasks)
    if in_flight:
        await log.emit("shutdown_drain_started", level=20, n=len(in_flight))
        try:
            await asyncio.wait_for(
                asyncio.gather(*in_flight, return_exceptions=True),
                timeout=GRACEFUL_SHUTDOWN_TIMEOUT_S,
            )
        except TimeoutError:
            await log.emit("shutdown_drain_timeout", level=30, n_left=len(http_app._tasks))

    # 4. Закрытие пулов и сессий
    await pool.close()  # type: ignore[attr-defined]
    await http_session.close()
    await runner.cleanup()  # type: ignore[attr-defined]
    await bot.session.close()  # type: ignore[attr-defined]


async def main() -> None:
    """Lifespan + блокирующее ожидание сигнала.

    SIGTERM/SIGINT настраиваются через asyncio.Event — без них контейнер просто
    бы убивался без вызова `_graceful_shutdown` (ARCHITECTURE.md §5.3).
    """
    import signal

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):  # Windows может не поддерживать
            loop.add_signal_handler(sig, stop_event.set)

    async with lifespan():
        await stop_event.wait()


def run() -> None:
    """Точка входа (`python -m src.main`).

    Вынесено из `if __name__ == "__main__":` чтобы было покрываемо тестами.
    `uvloop.run()` — современный API (uvloop>=0.18); заменяет deprecated
    `install()` + `asyncio.run()` и сам выставляет политику цикла.
    """
    if uvloop is not None:
        uvloop.run(main())
    else:
        asyncio.run(main())


if __name__ == "__main__":  # pragma: no cover
    run()
