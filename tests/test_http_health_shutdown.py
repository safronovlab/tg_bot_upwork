"""Тесты GET /health и graceful shutdown semantics.

Соответствие ARCHITECTURE.md §5.3 (graceful shutdown) + §7.6 (мониторинг).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from src import http_app


def make_request(body: bytes = b"", headers: dict | None = None):
    """Имитация aiohttp.web.Request."""
    req = MagicMock()
    req.read = AsyncMock(return_value=body)
    req.headers = headers or {}
    return req


class TestHealthEndpoint:
    async def test_returns_ok_when_db_alive(self, stub_db, stub_log, monkeypatch):
        # _conn() читает _pool, заведём фейковый, который возвращает 1
        from src import db

        pool = MagicMock()
        pool.fetchval = AsyncMock(return_value=1)
        monkeypatch.setattr(db, "_pool", pool, raising=False)
        http_app.set_shutting_down(False)

        resp = await http_app.health(make_request())
        assert resp.status == 200
        assert "ok" in resp.text

    async def test_returns_503_when_db_down(self, stub_db, stub_log, monkeypatch):
        from src import db

        pool = MagicMock()
        pool.fetchval = AsyncMock(side_effect=RuntimeError("db unreachable"))
        monkeypatch.setattr(db, "_pool", pool, raising=False)
        http_app.set_shutting_down(False)

        resp = await http_app.health(make_request())
        assert resp.status == 503
        assert "db_down" in resp.text

    async def test_returns_503_during_shutdown(self, stub_db, stub_log, monkeypatch):
        http_app.set_shutting_down(True)
        try:
            resp = await http_app.health(make_request())
            assert resp.status == 503
            assert "shutting_down" in resp.text
        finally:
            http_app.set_shutting_down(False)


class TestShutdownGate:
    async def test_webhook_returns_503_when_shutting_down(
        self, webhook_body_bytes, stub_db, stub_log
    ):
        http_app.set_shutting_down(True)
        try:
            resp = await http_app.upwork_lead(make_request(webhook_body_bytes))
            assert resp.status == 503
            assert "shutting_down" in resp.text
            # БД не трогаем при shutdown
            stub_db["try_register_request"].assert_not_awaited()
        finally:
            http_app.set_shutting_down(False)

    async def test_webhook_works_after_clearing_shutdown(
        self, webhook_body_bytes, stub_db, stub_log, monkeypatch
    ):
        from src import http_app as http_mod

        monkeypatch.setattr(http_mod, "_process_batch_async", AsyncMock(), raising=False)
        http_app.set_shutting_down(True)
        http_app.set_shutting_down(False)
        resp = await http_app.upwork_lead(make_request(webhook_body_bytes))
        assert resp.status == 200

    async def test_in_flight_tasks_tracked(
        self, webhook_body_bytes, stub_db, stub_log, monkeypatch
    ):
        """При успешном accept в _tasks добавляется ссылка на background-task."""
        from src import http_app as http_mod

        # медленный stub чтобы task ещё не успел отметиться как done
        async def slow_batch(payload, request_id):
            import asyncio

            await asyncio.sleep(0.5)

        monkeypatch.setattr(http_mod, "_process_batch_async", slow_batch, raising=False)
        http_app.set_shutting_down(False)

        before = len(http_app._tasks)
        await http_app.upwork_lead(make_request(webhook_body_bytes))
        # task ещё не завершился — должен быть в _tasks
        assert len(http_app._tasks) == before + 1


class TestShutdownFlag:
    def test_set_and_clear(self):
        http_app.set_shutting_down(False)
        assert http_app._is_shutting_down() is False
        http_app.set_shutting_down(True)
        assert http_app._is_shutting_down() is True
        http_app.set_shutting_down(False)
        assert http_app._is_shutting_down() is False
