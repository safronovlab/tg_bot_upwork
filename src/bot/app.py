"""Bot, Dispatcher, регистрация router и middleware. См. BOT.md и ../../ARCHITECTURE.md §5.2."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from aiogram import Bot, Dispatcher


def build(http_session: object | None = None) -> tuple[Bot, Dispatcher]:
    """Создать Bot + Dispatcher с middleware, FSM-storage и зарегистрированными handlers.

    Параметр http_session зарезервирован под кастомный AiohttpSession (ARCHITECTURE.md §5.2).
    """
    del http_session

    from aiogram import Bot, Dispatcher
    from aiogram.client.default import DefaultBotProperties
    from aiogram.fsm.storage.memory import MemoryStorage

    from src import config
    from src.bot.auth import AllowlistMiddleware
    from src.bot.routers import build_router

    bot = Bot(
        token=config.TELEGRAM_BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=None),
    )
    dp = Dispatcher(storage=MemoryStorage())

    middleware = AllowlistMiddleware()
    dp.message.middleware(middleware)
    dp.callback_query.middleware(middleware)

    dp.include_router(build_router())

    return bot, dp
