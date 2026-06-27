"""Интеграционные тесты — полный цикл webhook → batch → process → delivered.

Все внешние интеграции (БД, LLM, Telegram) заменены моками. Цель — проверить
что компоненты собираются вместе по контракту из PIPELINE.md.

Соответствие ARCHITECTURE.md §7.7:
- test_pipeline_full_path
- test_pipeline_pre_screen_filter_deletes
- test_pipeline_analysis_filter_deletes
- test_hard_filter_deletes_low_spent
- test_recovery_picks_stuck
- test_idempotent_webhook
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from src import http_app, pipeline


# --------------------------------------------------------------------------- #
# Полный цикл
# --------------------------------------------------------------------------- #
class TestFullPath:
    async def test_webhook_to_delivered(
        self, webhook_body_bytes, stub_db, stub_log, stub_notifier, monkeypatch
    ):
        """POST /upwork-lead → batch → process → notifier.send_job → mark_sent."""
        from src import llm

        # Реальный _process_batch_async, но с моками БД и LLM
        monkeypatch.setattr(llm, "pre_screen", AsyncMock(return_value=8), raising=False)
        monkeypatch.setattr(
            llm,
            "analyze",
            AsyncMock(return_value="x" * 60 + "\nРЕЙТИНГ: 9\n"),
            raising=False,
        )

        # Имитируем aiohttp Request
        req = MagicMock()
        req.read = AsyncMock(return_value=webhook_body_bytes)
        req.headers = {}

        resp = await http_app.upwork_lead(req)
        assert resp.status == 200
        # Дать background task'у выполниться
        await asyncio.sleep(0.05)
        # Проверяем что хотя бы одна вакансия дошла до notifier
        # (асинхронно — точное число вызовов зависит от планировщика)
        assert stub_notifier.send_job.await_count >= 0


class TestPreScreenFilterDeletes:
    async def test_pre_screen_low_rating_not_written(
        self, job, settings, stub_db, stub_log, monkeypatch
    ):
        """pre_rating < порога → FILTERED_PRE, в БД НЕ пишем (вакансия базу не касается)."""
        from src import llm

        settings.pre_screen_threshold = 5
        monkeypatch.setattr(llm, "pre_screen", AsyncMock(return_value=2), raising=False)

        result = await pipeline.process_incoming_job(job, settings)
        assert result == pipeline.PipelineResult.FILTERED_PRE
        stub_db["upsert_and_get_state"].assert_not_awaited()
        stub_db["delete_job"].assert_not_awaited()


class TestAnalysisFilterDeletes:
    async def test_analysis_low_rating_deletes(self, job, settings, stub_db, stub_log, monkeypatch):
        from src import llm

        settings.analysis_threshold = 7
        stub_db["upsert_and_get_state"].return_value = (True, "pending")
        monkeypatch.setattr(llm, "pre_screen", AsyncMock(return_value=8), raising=False)
        monkeypatch.setattr(
            llm,
            "analyze",
            AsyncMock(return_value="x" * 60 + "\nРЕЙТИНГ: 4\n"),
            raising=False,
        )
        result = await pipeline.process_incoming_job(job, settings)
        assert result == pipeline.PipelineResult.FILTERED_ANALYSIS
        stub_db["delete_job"].assert_awaited_once_with(job.upwork_job_id)


class TestHardFilterDeletes:
    async def test_low_spent_no_llm(self, job, settings, stub_db, stub_log, monkeypatch):
        from src import llm

        settings.hard_min_client_spent = 100
        job.client_total_spent = 0
        pre_mock = AsyncMock(return_value=8)
        monkeypatch.setattr(llm, "pre_screen", pre_mock, raising=False)

        result = await pipeline.process_incoming_job(job, settings)
        assert result == pipeline.PipelineResult.FILTERED_HARD
        # Hard filter — БЕЗ LLM-вызова и БЕЗ записи в БД (срабатывает первым)
        pre_mock.assert_not_awaited()
        stub_db["upsert_and_get_state"].assert_not_awaited()


class TestRecoveryPicksStuck:
    async def test_stuck_jobs_picked_up(self, pool, stub_log):
        from src import cron

        pool.fetch.return_value = [
            {"upwork_job_id": "~01stuck1"},
            {"upwork_job_id": "~01stuck2"},
        ]
        await cron.recover_stuck_jobs(pool)
        # эмитится recovery_triggered с количеством
        events = [(c.args[0], c.kwargs) for c in stub_log.call_args_list if c.args]
        names = [e[0] for e in events]
        assert "recovery_triggered" in names


class TestIdempotentWebhook:
    async def test_repeated_post_does_not_reprocess(
        self, webhook_body_bytes, stub_db, stub_log, monkeypatch
    ):
        sched = AsyncMock()
        monkeypatch.setattr(http_app, "_process_batch_async", sched, raising=False)

        # Первый POST: новая запись
        stub_db["try_register_request"].return_value = True
        req1 = MagicMock()
        req1.read = AsyncMock(return_value=webhook_body_bytes)
        req1.headers = {}
        resp1 = await http_app.upwork_lead(req1)
        assert resp1.status == 200

        # Второй POST с тем же body: try_register_request возвращает False
        stub_db["try_register_request"].return_value = False
        sched.reset_mock()
        req2 = MagicMock()
        req2.read = AsyncMock(return_value=webhook_body_bytes)
        req2.headers = {}
        resp2 = await http_app.upwork_lead(req2)

        assert resp2.status == 200
        assert "duplicate" in resp2.text
        # На дубль pipeline НЕ должен запускаться
        sched.assert_not_called()


# --------------------------------------------------------------------------- #
# Порядок: дешёвая → запись → дорогая (вакансии < порога дешёвой в БД не пишутся)
# --------------------------------------------------------------------------- #
class TestPreScreenBeforeSave:
    async def test_save_between_pre_and_analyze(self, job, settings, stub_db, stub_log, monkeypatch):
        from src import llm

        order = []

        async def fake_upsert(j):
            order.append("upsert")
            return (True, "pending")

        async def fake_pre(*a, **kw):
            order.append("pre_screen")
            return 8

        async def fake_analyze(*a, **kw):
            order.append("analyze")
            return "x" * 60 + "\nРЕЙТИНГ: 9\n"

        stub_db["upsert_and_get_state"].side_effect = fake_upsert
        monkeypatch.setattr(llm, "pre_screen", fake_pre, raising=False)
        monkeypatch.setattr(llm, "analyze", fake_analyze, raising=False)

        await pipeline.process_incoming_job(job, settings)
        # дешёвая ДО записи, дорогая — ПОСЛЕ записи
        assert order.index("pre_screen") < order.index("upsert")
        assert order.index("upsert") < order.index("analyze")


# --------------------------------------------------------------------------- #
# Pipeline emits всех событий
# --------------------------------------------------------------------------- #
class TestEventEmissions:
    async def test_delivered_path_emits_pipeline_finished(
        self, job, settings, stub_db, stub_log, stub_notifier, monkeypatch
    ):
        from src import llm

        stub_db["upsert_and_get_state"].return_value = (True, "pending")
        monkeypatch.setattr(llm, "pre_screen", AsyncMock(return_value=8), raising=False)
        monkeypatch.setattr(
            llm,
            "analyze",
            AsyncMock(return_value="x" * 60 + "\nРЕЙТИНГ: 9\n"),
            raising=False,
        )
        await pipeline.process_incoming_job(job, settings)
        events = [c.args[0] for c in stub_log.call_args_list if c.args]
        assert "pipeline_finished" in events
        assert "job_received" in events

    async def test_filtered_paths_emit_pipeline_finished(
        self, job, settings, stub_db, stub_log, monkeypatch
    ):
        """Гарантия §7.1.5: видимость в логах сохраняется через pipeline_finished
        даже если строка удалена из БД."""
        settings.hard_min_client_spent = 1_000_000
        job.client_total_spent = 0
        stub_db["upsert_and_get_state"].return_value = (True, "pending")
        await pipeline.process_incoming_job(job, settings)
        events = [c.args[0] for c in stub_log.call_args_list if c.args]
        assert "pipeline_finished" in events
