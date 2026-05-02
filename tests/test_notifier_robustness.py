"""Тесты на устойчивость notifier.send_job: Telegram-ошибка не валит batch.

Соответствие ARCHITECTURE.md §7.2 — известные ошибки → return None / event.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

from aiogram.exceptions import TelegramAPIError
from src import notifier


class TestSendJobRobustness:
    async def test_telegram_error_does_not_propagate(self, bot, job, stub_log, monkeypatch):
        bot.send_message = AsyncMock(
            side_effect=TelegramAPIError(method=None, message="429 too many")
        )
        monkeypatch.setattr(notifier, "bot", bot, raising=False)
        # Не должно бросать наружу
        await notifier.send_job(job, "Анализ", silent=True)

    async def test_telegram_error_emits_event(self, bot, job, stub_log, monkeypatch):
        bot.send_message = AsyncMock(side_effect=TelegramAPIError(method=None, message="429"))
        monkeypatch.setattr(notifier, "bot", bot, raising=False)
        await notifier.send_job(job, "Анализ", silent=True)
        events = [c.args[0] for c in stub_log.call_args_list if c.args]
        assert "telegram_send_failed" in events

    async def test_send_job_from_row_robust_too(self, bot, stub_log, monkeypatch):
        bot.send_message = AsyncMock(side_effect=TelegramAPIError(method=None, message="403"))
        monkeypatch.setattr(notifier, "bot", bot, raising=False)
        row = {
            "upwork_job_id": "~01x",
            "job_title": "T",
            "ai_analysis": "A",
            "upwork_url": "u",
            "rating": 8,
        }
        await notifier.send_job_from_row(row)
        events = [c.args[0] for c in stub_log.call_args_list if c.args]
        assert "telegram_send_failed" in events
