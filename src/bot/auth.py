"""AllowlistMiddleware: фильтр по ALLOWED_USER_IDS. См. ../../ARCHITECTURE.md §4.1."""

from __future__ import annotations

from typing import Any


class AllowlistMiddleware:
    """aiogram BaseMiddleware-совместимый фильтр.

    Не наследуем aiogram.BaseMiddleware напрямую — упрощает тесты и убирает жёсткую
    зависимость импорта при отсутствии aiogram в окружении.
    """

    async def __call__(self, handler: Any, event: Any, data: dict) -> Any:
        from src import config

        user = getattr(event, "from_user", None)
        user_id = getattr(user, "id", None) if user is not None else None
        if user_id is None or user_id not in config.ALLOWED_USER_IDS:
            return None
        return await handler(event, data)
