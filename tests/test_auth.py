"""Тесты bot/auth.py — AllowlistMiddleware.

Соответствие ARCHITECTURE.md §4.1.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from src.bot import auth


def make_event(user_id: int):
    e = MagicMock()
    e.from_user = MagicMock()
    e.from_user.id = user_id
    return e


class TestAllowlistMiddleware:
    async def test_allowed_user_passes_through(self, monkeypatch):
        from src import config

        monkeypatch.setattr(config, "ALLOWED_USER_IDS", {701492865}, raising=False)
        m = auth.AllowlistMiddleware()
        handler = AsyncMock(return_value="OK")
        result = await m(handler, make_event(701492865), {})
        assert result == "OK"
        handler.assert_awaited_once()

    async def test_unallowed_user_blocked(self, monkeypatch):
        from src import config

        monkeypatch.setattr(config, "ALLOWED_USER_IDS", {701492865}, raising=False)
        m = auth.AllowlistMiddleware()
        handler = AsyncMock(return_value="OK")
        result = await m(handler, make_event(99999), {})
        # Не должно вызвать handler
        handler.assert_not_called()
        # Возвращает None или ничего
        assert result is None
