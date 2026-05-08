"""Тесты thread_resolver: resolve_thread_key + link_to_upwork_job. См. CHAT.md §3."""

from __future__ import annotations

import hashlib
from unittest.mock import AsyncMock

import pytest
from src.chat import thread_resolver


# --------------------------------------------------------------------------- #
# resolve_thread_key
# --------------------------------------------------------------------------- #
class TestResolveThreadKey:
    @pytest.fixture
    def stub_pool(self, monkeypatch):
        """Stub db._conn() → fake pool с настраиваемым fetchval."""
        from src import db

        fake_pool = AsyncMock()
        fake_pool.fetchval = AsyncMock(return_value=None)

        def _fake_conn():
            return fake_pool

        monkeypatch.setattr(db, "_conn", _fake_conn)
        return fake_pool

    async def test_in_reply_to_match_returns_existing_key(self, stub_pool):
        """Если In-Reply-To совпадает с существующим — берём его thread_key."""
        existing_key = b"\xab" * 32
        stub_pool.fetchval.return_value = existing_key

        result = await thread_resolver.resolve_thread_key(
            in_reply_to="<previous@upwork.com>",
            client_name="John",
            job_title="Test Job",
        )
        assert result == existing_key

    async def test_no_in_reply_to_uses_fallback_hash(self, stub_pool):
        """Без In-Reply-To: deterministic hash(client|job)."""
        stub_pool.fetchval.return_value = None  # ничего не нашли в БД

        result = await thread_resolver.resolve_thread_key(
            in_reply_to=None,
            client_name="John Doe",
            job_title="Stripe webhook fix",
        )
        # Hash должен быть детерминированным
        expected = hashlib.sha256(b"john doe|stripe webhook fix").digest()
        assert result == expected

    async def test_fallback_normalizes_case_and_whitespace(self, stub_pool):
        """Тот же клиент + та же вакансия → одинаковый ключ независимо от case."""
        stub_pool.fetchval.return_value = None

        key1 = await thread_resolver.resolve_thread_key(
            in_reply_to=None,
            client_name="John Doe",
            job_title="API Integration",
        )
        key2 = await thread_resolver.resolve_thread_key(
            in_reply_to=None,
            client_name="  JOHN DOE  ",
            job_title="api integration",
        )
        assert key1 == key2

    async def test_in_reply_to_unknown_falls_back(self, stub_pool):
        """In-Reply-To указан но не найден → fallback hash."""
        stub_pool.fetchval.return_value = None  # не найдено

        result = await thread_resolver.resolve_thread_key(
            in_reply_to="<unknown@upwork.com>",
            client_name="John",
            job_title="Job",
        )
        expected = hashlib.sha256(b"john|job").digest()
        assert result == expected

    async def test_none_job_title_handled(self, stub_pool):
        """job_title=None не должен ломать hash."""
        stub_pool.fetchval.return_value = None

        result = await thread_resolver.resolve_thread_key(
            in_reply_to=None,
            client_name="John",
            job_title=None,
        )
        # Не падает; возвращает 32 байта
        assert isinstance(result, bytes)
        assert len(result) == 32


# --------------------------------------------------------------------------- #
# link_to_upwork_job
# --------------------------------------------------------------------------- #
class TestLinkToUpworkJob:
    @pytest.fixture
    def stub_pool(self, monkeypatch):
        from src import db

        fake_pool = AsyncMock()
        fake_pool.fetchval = AsyncMock(return_value=None)

        def _fake_conn():
            return fake_pool

        monkeypatch.setattr(db, "_conn", _fake_conn)
        return fake_pool

    async def test_no_inputs_returns_none(self, stub_pool):
        result = await thread_resolver.link_to_upwork_job(
            job_title=None, job_url=None
        )
        assert result is None
        stub_pool.fetchval.assert_not_awaited()

    async def test_url_match_returns_id(self, stub_pool):
        stub_pool.fetchval.return_value = 42

        result = await thread_resolver.link_to_upwork_job(
            job_title=None,
            job_url="https://www.upwork.com/jobs/~01abc",
        )
        assert result == 42

    async def test_short_title_skipped(self, stub_pool):
        """Title <8 chars — слишком общее, пропускаем чтобы не зацепить чужие."""
        result = await thread_resolver.link_to_upwork_job(
            job_title="API",  # 3 chars
            job_url=None,
        )
        assert result is None

    async def test_title_fuzzy_match(self, stub_pool):
        stub_pool.fetchval.return_value = 99

        result = await thread_resolver.link_to_upwork_job(
            job_title="Stripe webhook fix urgent",
            job_url=None,
        )
        assert result == 99
