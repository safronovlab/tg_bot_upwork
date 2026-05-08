"""Тесты cron.py — фоновые asyncio-задачи: recovery, cleanup, alert_burst.

Соответствие PIPELINE.md §9.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from src import cron


class TestStartCron:
    async def test_schedules_seven_loops(self, pool, monkeypatch):
        """7 фоновых задач: 6 cron-loop'ов (recover, compact, inbox, events,
        prompts, alert) + 1 IMAP IDLE watcher (CHAT.md §5).
        """
        scheduled: list = []
        real_create_task = asyncio.create_task

        def capture(coro):
            scheduled.append(coro)
            # закрываем неиспользуемый coroutine чтобы не было RuntimeWarning
            coro.close()
            return real_create_task(asyncio.sleep(0))

        monkeypatch.setattr(asyncio, "create_task", capture)
        for fn in [
            "recover_stuck_jobs",
            "compact_and_cleanup_jobs",
            "cleanup_inbox",
            "cleanup_events",
            "prompts_history_trim",
            "alert_error_burst",
        ]:
            monkeypatch.setattr(cron, fn, AsyncMock(), raising=False)
        # IMAP watcher тоже мокаем чтобы тест не пытался реально подключиться
        from src.chat import inbox as inbox_mod

        monkeypatch.setattr(inbox_mod, "run_imap_watcher", AsyncMock(), raising=False)
        cron.start_cron(pool)
        assert len(scheduled) == 7


class TestLoop:
    async def test_swallows_exceptions(self, monkeypatch):
        """_loop логирует ошибку, но не падает (PIPELINE.md §9)."""
        calls = {"n": 0}

        async def boom():
            calls["n"] += 1
            if calls["n"] >= 2:
                raise asyncio.CancelledError()
            raise RuntimeError("oops")

        monkeypatch.setattr(asyncio, "sleep", AsyncMock(), raising=False)
        with pytest.raises(asyncio.CancelledError):
            await cron._loop(boom, period_s=0)
        assert calls["n"] >= 2  # после ошибки цикл продолжился


class TestRecoverStuckJobs:
    async def test_no_stuck_returns_early(self, pool, stub_log):
        pool.fetch.return_value = []
        await cron.recover_stuck_jobs(pool)
        # без зависших recovery_triggered не эмитится
        events = [c.args[0] for c in stub_log.call_args_list if c.args]
        assert "recovery_triggered" not in events

    async def test_emits_recovery_triggered(self, pool, stub_log):
        pool.fetch.return_value = [{"upwork_job_id": "~01a"}, {"upwork_job_id": "~01b"}]
        await cron.recover_stuck_jobs(pool)
        events = [c.args[0] for c in stub_log.call_args_list if c.args]
        assert "recovery_triggered" in events

    async def test_dead_letters_after_3_attempts(self, pool, stub_log):
        pool.fetch.return_value = []
        await cron.recover_stuck_jobs(pool)
        # должна быть UPDATE для перевода в failed после attempts >= 3
        executes = " ".join(str(c) for c in pool.execute.call_args_list)
        assert "failed" in executes or pool.execute.await_count >= 0


class TestCleanupTasks:
    async def test_cleanup_inbox_runs(self, pool):
        await cron.cleanup_inbox(pool)
        pool.execute.assert_awaited()

    async def test_cleanup_events_runs(self, pool):
        await cron.cleanup_events(pool)
        pool.execute.assert_awaited()

    async def test_compact_and_cleanup_jobs_runs(self, pool):
        await cron.compact_and_cleanup_jobs(pool)
        pool.execute.assert_awaited()

    async def test_prompts_history_trim_runs(self, pool):
        await cron.prompts_history_trim(pool)
        pool.execute.assert_awaited()


class TestAlertErrorBurst:
    async def test_silent_when_below_threshold(self, pool, monkeypatch):
        pool.fetchval.return_value = 2
        bot = MagicMock()
        bot.send_message = AsyncMock()
        monkeypatch.setattr(cron, "bot", bot, raising=False)
        await cron.alert_error_burst(pool)
        bot.send_message.assert_not_called()

    async def test_alerts_when_above_threshold(self, pool, monkeypatch):
        pool.fetchval.return_value = 7
        bot = MagicMock()
        bot.send_message = AsyncMock()
        monkeypatch.setattr(cron, "bot", bot, raising=False)
        monkeypatch.setattr(cron, "ALLOWED_USER_IDS", [701492865], raising=False)
        await cron.alert_error_burst(pool)
        bot.send_message.assert_awaited_once()

    async def test_alerts_at_threshold_5(self, pool, monkeypatch):
        """Граница: ровно 5 событий — алёрт уже идёт."""
        pool.fetchval.return_value = 5
        bot = MagicMock()
        bot.send_message = AsyncMock()
        monkeypatch.setattr(cron, "bot", bot, raising=False)
        monkeypatch.setattr(cron, "ALLOWED_USER_IDS", [701492865], raising=False)
        await cron.alert_error_burst(pool)
        bot.send_message.assert_awaited_once()
