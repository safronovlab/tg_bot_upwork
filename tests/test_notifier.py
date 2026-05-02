"""Тесты notifier.py — отправка карточек вакансий с silent/loud.

Соответствие BOT.md §9 и PIPELINE.md §4.
"""

from __future__ import annotations

from src import notifier


class TestSendJob:
    async def test_silent_true_means_disable_notification(self, bot, job, monkeypatch):
        monkeypatch.setattr(notifier, "bot", bot, raising=False)
        await notifier.send_job(job, "Анализ", silent=True)
        kwargs = bot.send_message.call_args.kwargs
        assert kwargs.get("disable_notification") is True

    async def test_silent_false_means_loud(self, bot, job, monkeypatch):
        monkeypatch.setattr(notifier, "bot", bot, raising=False)
        await notifier.send_job(job, "Анализ", silent=False)
        kwargs = bot.send_message.call_args.kwargs
        assert kwargs.get("disable_notification") is False

    async def test_text_includes_job_title(self, bot, job, monkeypatch):
        monkeypatch.setattr(notifier, "bot", bot, raising=False)
        await notifier.send_job(job, "Анализ R1", silent=True)
        kwargs = bot.send_message.call_args.kwargs
        text = kwargs.get("text", "")
        assert job.job_title.upper() in text or job.job_title in text

    async def test_includes_inline_buttons(self, bot, job, monkeypatch):
        monkeypatch.setattr(notifier, "bot", bot, raising=False)
        await notifier.send_job(job, "Анализ", silent=True)
        kwargs = bot.send_message.call_args.kwargs
        kb = kwargs.get("reply_markup")
        assert kb is not None


class TestSendJobFromRow:
    async def test_sends_from_db_row(self, bot, monkeypatch):
        monkeypatch.setattr(notifier, "bot", bot, raising=False)
        row = {
            "upwork_job_id": "~01a",
            "job_title": "T",
            "ai_analysis": "Анализ\nРЕЙТИНГ: 8",
            "upwork_url": "https://u",
            "rating": 8,
        }
        await notifier.send_job_from_row(row)
        bot.send_message.assert_awaited()
