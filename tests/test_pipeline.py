"""Тесты pipeline.py — парсеры, hard_filter, process_incoming_job, batch processor.

Соответствие PIPELINE.md:
- §3 batch processor (`_process_batch_async`, `safe_process_one`)
- §4 `process_incoming_job` (8 веток выхода)
- §5 hard_filter (6 правил)
- §6 parse_rating / parse_pre_rating
- §5 parse_hourly_budget_max / parse_fixed_budget
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from src import pipeline


# --------------------------------------------------------------------------- #
# PipelineResult enum + TERMINAL_STATES (PIPELINE.md §4)
# --------------------------------------------------------------------------- #
class TestPipelineResultEnum:
    def test_all_seven_values_present(self):
        expected = {
            "DELIVERED",
            "QUEUED_PAUSED",
            "FILTERED_HARD",
            "FILTERED_PRE",
            "FILTERED_ANALYSIS",
            "SKIPPED_DUPLICATE",
            "LLM_FAILED",
        }
        assert {m.name for m in pipeline.PipelineResult} == expected

    def test_str_enum_values(self):
        assert pipeline.PipelineResult.DELIVERED.value == "delivered"
        assert pipeline.PipelineResult.FILTERED_HARD.value == "filtered_hard"
        assert pipeline.PipelineResult.SKIPPED_DUPLICATE.value == "skipped_duplicate"

    def test_terminal_states_set(self):
        assert {"filtered", "delivered", "analyzed", "failed"} == pipeline.TERMINAL_STATES


# --------------------------------------------------------------------------- #
# parse_rating (PIPELINE.md §6)
# --------------------------------------------------------------------------- #
class TestParseRating:
    def test_basic_integer(self):
        assert pipeline.parse_rating("РЕЙТИНГ: 8") == 8

    def test_case_insensitive(self):
        assert pipeline.parse_rating("рейтинг: 7") == 7

    def test_decimal_dot_rounded(self):
        assert pipeline.parse_rating("РЕЙТИНГ: 7.6") == 8

    def test_decimal_comma_rounded(self):
        assert pipeline.parse_rating("РЕЙТИНГ: 7,4") == 7

    def test_clamp_above_ten(self):
        assert pipeline.parse_rating("РЕЙТИНГ: 99") == 10

    def test_clamp_below_zero(self):
        assert pipeline.parse_rating("РЕЙТИНГ: -3") == 0

    def test_empty_returns_zero(self):
        assert pipeline.parse_rating("") == 0

    def test_no_match_returns_zero(self):
        assert pipeline.parse_rating("без рейтинга вообще") == 0

    def test_with_surrounding_text(self):
        text = "Анализ вакансии:\n...\nРЕЙТИНГ: 6\nКомментарий..."
        assert pipeline.parse_rating(text) == 6


class TestParsePreRating:
    def test_basic(self):
        assert pipeline.parse_pre_rating("8") == 8

    def test_returns_none_for_empty(self):
        assert pipeline.parse_pre_rating("") is None

    def test_returns_none_for_unparseable(self):
        assert pipeline.parse_pre_rating("не понял") is None

    def test_returns_none_for_out_of_range(self):
        assert pipeline.parse_pre_rating("42") is None

    def test_zero_ok(self):
        assert pipeline.parse_pre_rating("0") == 0

    def test_ten_ok(self):
        assert pipeline.parse_pre_rating("10") == 10


# --------------------------------------------------------------------------- #
# Парсеры бюджета (PIPELINE.md §5)
# --------------------------------------------------------------------------- #
class TestParseHourlyBudgetMax:
    def test_range_returns_upper(self):
        assert pipeline.parse_hourly_budget_max("$5-$15") == 15.0

    def test_single_value(self):
        assert pipeline.parse_hourly_budget_max("$30") == 30.0

    def test_decimal(self):
        assert pipeline.parse_hourly_budget_max("$5.50-$12.75") == 12.75

    def test_none_or_empty(self):
        assert pipeline.parse_hourly_budget_max(None) is None
        assert pipeline.parse_hourly_budget_max("") is None

    def test_unparseable(self):
        assert pipeline.parse_hourly_budget_max("Negotiable") is None


class TestParseFixedBudget:
    def test_basic(self):
        assert pipeline.parse_fixed_budget("$500") == 500.0

    def test_with_label(self):
        assert pipeline.parse_fixed_budget("Fixed-price 250") == 250.0

    def test_with_comma(self):
        assert pipeline.parse_fixed_budget("$1,500") == 1500.0

    def test_none(self):
        assert pipeline.parse_fixed_budget(None) is None
        assert pipeline.parse_fixed_budget("") is None

    def test_unparseable(self):
        assert pipeline.parse_fixed_budget("TBD") is None


# --------------------------------------------------------------------------- #
# hard_filter (PIPELINE.md §5)
# --------------------------------------------------------------------------- #
class TestHardFilter:
    def test_passes_when_all_off(self, settings, job):
        assert pipeline.hard_filter(job, settings) is None

    def test_low_spent_blocks(self, settings, job):
        settings.hard_min_client_spent = 100
        job.client_total_spent = 30
        assert pipeline.hard_filter(job, settings) == "low_spent:$30"

    def test_low_spent_off_when_threshold_zero(self, settings, job):
        settings.hard_min_client_spent = 0
        job.client_total_spent = 0
        assert pipeline.hard_filter(job, settings) is None

    def test_low_rating_requires_min_hires(self, settings, job):
        settings.hard_min_client_rating = 4.0
        settings.hard_min_hires_for_rating = 3
        job.client_rating = 3.0
        job.client_total_hires = 10
        assert pipeline.hard_filter(job, settings) == "low_rating:3.0"

    def test_low_rating_skipped_with_few_hires(self, settings, job):
        """Маленькая выборка → 5★ ничего не значит, рейтинг не применяется."""
        settings.hard_min_client_rating = 4.0
        settings.hard_min_hires_for_rating = 5
        job.client_rating = 1.0
        job.client_total_hires = 2
        assert pipeline.hard_filter(job, settings) is None

    def test_low_hourly(self, settings, job):
        settings.hard_min_budget_hourly = 20
        job.budget_type = "Hourly"
        job.budget = "$5-$15"
        assert pipeline.hard_filter(job, settings) == "low_hourly:$15"

    def test_hourly_rule_skipped_for_fixed(self, settings, job):
        settings.hard_min_budget_hourly = 50
        job.budget_type = "Fixed"
        job.budget = "$5"
        assert pipeline.hard_filter(job, settings) is None

    def test_low_fixed(self, settings, job):
        settings.hard_min_budget_fixed = 500
        job.budget_type = "Fixed"
        job.budget = "$200"
        assert pipeline.hard_filter(job, settings) == "low_fixed:$200"

    def test_no_hires_rule(self, settings, job):
        settings.hard_reject_no_hires = True
        job.client_total_hires = 0
        assert pipeline.hard_filter(job, settings) == "no_hires"

    def test_no_hires_off(self, settings, job):
        settings.hard_reject_no_hires = False
        job.client_total_hires = 0
        assert pipeline.hard_filter(job, settings) is None

    def test_stale(self, settings, job):
        settings.hard_max_vacancy_age_h = 24
        job.published_date = datetime.now(UTC) - timedelta(hours=72)
        reason = pipeline.hard_filter(job, settings)
        assert reason is not None and reason.startswith("stale:")

    def test_recent_passes_age_check(self, settings, job):
        settings.hard_max_vacancy_age_h = 24
        job.published_date = datetime.now(UTC) - timedelta(hours=2)
        assert pipeline.hard_filter(job, settings) is None

    def test_age_check_skipped_when_no_published_date(self, settings, job):
        settings.hard_max_vacancy_age_h = 24
        job.published_date = None
        assert pipeline.hard_filter(job, settings) is None


# --------------------------------------------------------------------------- #
# process_incoming_job — все 8 ветвей выхода (PIPELINE.md §4)
# --------------------------------------------------------------------------- #
class TestProcessIncomingJob:
    async def test_skipped_duplicate_terminal_state(
        self, job, settings, stub_db, stub_log, llm_pre_ok
    ):
        stub_db["upsert_and_get_state"].return_value = (False, "delivered")
        result = await pipeline.process_incoming_job(job, settings)
        assert result == pipeline.PipelineResult.SKIPPED_DUPLICATE

    async def test_skipped_duplicate_non_terminal_state(
        self, job, settings, stub_db, stub_log, llm_pre_ok
    ):
        """Vollna шлёт ту же вакансию (пересечение фильтров). Любая существующая
        запись (inserted=False) skip'ается → в TG приходит ОДИН раз."""
        stub_db["upsert_and_get_state"].return_value = (False, "pending")
        result = await pipeline.process_incoming_job(job, settings)
        assert result == pipeline.PipelineResult.SKIPPED_DUPLICATE

    async def test_skipped_duplicate_pre_screened_state(
        self, job, settings, stub_db, stub_log, llm_pre_ok
    ):
        """Дубль уже прошедшего pre_screen — параллельная таска не делает второй analysis."""
        stub_db["upsert_and_get_state"].return_value = (False, "pre_screened")
        result = await pipeline.process_incoming_job(job, settings)
        assert result == pipeline.PipelineResult.SKIPPED_DUPLICATE

    async def test_filtered_hard_not_written(self, job, settings, stub_db, stub_log):
        """Hard-фильтр срабатывает ДО записи — в БД ничего не пишется."""
        settings.hard_min_client_spent = 1_000_000
        job.client_total_spent = 0
        result = await pipeline.process_incoming_job(job, settings)
        assert result == pipeline.PipelineResult.FILTERED_HARD
        stub_db["upsert_and_get_state"].assert_not_awaited()

    async def test_pre_screen_failed_no_db(
        self, job, settings, stub_db, stub_log, monkeypatch
    ):
        """Дешёвая упала → LLM_FAILED, в БД ничего не пишем (строки нет)."""
        from src import llm

        monkeypatch.setattr(llm, "pre_screen", AsyncMock(return_value=None), raising=False)
        result = await pipeline.process_incoming_job(job, settings)
        assert result == pipeline.PipelineResult.LLM_FAILED
        stub_db["upsert_and_get_state"].assert_not_awaited()

    async def test_filtered_pre_not_written(self, job, settings, stub_db, stub_log, monkeypatch):
        """pre_rating < порога → FILTERED_PRE, в БД НЕ пишем (вакансия базу не касается)."""
        from src import llm

        settings.pre_screen_threshold = 5
        monkeypatch.setattr(llm, "pre_screen", AsyncMock(return_value=2), raising=False)
        result = await pipeline.process_incoming_job(job, settings)
        assert result == pipeline.PipelineResult.FILTERED_PRE
        stub_db["upsert_and_get_state"].assert_not_awaited()

    async def test_analysis_failed_short(self, job, settings, stub_db, stub_log, monkeypatch):
        from src import llm

        stub_db["upsert_and_get_state"].return_value = (True, "pending")
        monkeypatch.setattr(llm, "pre_screen", AsyncMock(return_value=8), raising=False)
        monkeypatch.setattr(llm, "analyze", AsyncMock(return_value="too short"), raising=False)
        result = await pipeline.process_incoming_job(job, settings)
        assert result == pipeline.PipelineResult.LLM_FAILED
        stub_db["bump_attempts"].assert_awaited()

    async def test_analysis_failed_empty(self, job, settings, stub_db, stub_log, monkeypatch):
        from src import llm

        stub_db["upsert_and_get_state"].return_value = (True, "pending")
        monkeypatch.setattr(llm, "pre_screen", AsyncMock(return_value=8), raising=False)
        monkeypatch.setattr(llm, "analyze", AsyncMock(return_value=None), raising=False)
        result = await pipeline.process_incoming_job(job, settings)
        assert result == pipeline.PipelineResult.LLM_FAILED

    async def test_filtered_analysis_deletes(self, job, settings, stub_db, stub_log, monkeypatch):
        from src import llm

        settings.analysis_threshold = 7
        stub_db["upsert_and_get_state"].return_value = (True, "pending")
        monkeypatch.setattr(llm, "pre_screen", AsyncMock(return_value=8), raising=False)
        monkeypatch.setattr(
            llm,
            "analyze",
            AsyncMock(return_value="x" * 60 + "\nРЕЙТИНГ: 3\n"),
            raising=False,
        )
        result = await pipeline.process_incoming_job(job, settings)
        assert result == pipeline.PipelineResult.FILTERED_ANALYSIS
        stub_db["delete_job"].assert_awaited_once_with(job.upwork_job_id)

    async def test_filtered_when_float_below_threshold(
        self, job, settings, stub_db, stub_log, monkeypatch
    ):
        """4.8 < 5.0 — должен фильтроваться, несмотря на round(4.8)=5.

        Регрессия: до фикса parse_rating округлял до int=5, и сравнение
        5 < 5 = False пропускало вакансию в TG. Теперь сравнение по float."""
        from src import llm

        settings.analysis_threshold = 5
        stub_db["upsert_and_get_state"].return_value = (True, "pending")
        monkeypatch.setattr(llm, "pre_screen", AsyncMock(return_value=8), raising=False)
        monkeypatch.setattr(
            llm,
            "analyze",
            AsyncMock(return_value="x" * 60 + "\nРЕЙТИНГ: 4.8/10\n"),
            raising=False,
        )
        result = await pipeline.process_incoming_job(job, settings)
        assert result == pipeline.PipelineResult.FILTERED_ANALYSIS

    async def test_passes_when_float_equals_threshold(
        self, job, settings, stub_db, stub_log, monkeypatch
    ):
        """5.0 ≥ 5 — должен пройти. Граница включающая."""
        from src import llm

        settings.analysis_threshold = 5
        stub_db["get_settings_cached"].return_value = settings
        stub_db["upsert_and_get_state"].return_value = (True, "pending")
        monkeypatch.setattr(llm, "pre_screen", AsyncMock(return_value=8), raising=False)
        monkeypatch.setattr(
            llm,
            "analyze",
            AsyncMock(return_value="x" * 60 + "\nРЕЙТИНГ: 5.0\n"),
            raising=False,
        )
        result = await pipeline.process_incoming_job(job, settings)
        assert result == pipeline.PipelineResult.DELIVERED

    async def test_silent_uses_float_rating(
        self, job, settings, stub_db, stub_log, stub_notifier, monkeypatch
    ):
        """7.6 < 8 — silent=True, несмотря на round(7.6)=8. Параллельный
        rounding-баг для loud_notification_threshold."""
        from src import llm

        settings.analysis_threshold = 0
        settings.loud_notification_threshold = 8
        stub_db["get_settings_cached"].return_value = settings
        stub_db["upsert_and_get_state"].return_value = (True, "pending")
        monkeypatch.setattr(llm, "pre_screen", AsyncMock(return_value=8), raising=False)
        monkeypatch.setattr(
            llm,
            "analyze",
            AsyncMock(return_value="x" * 60 + "\nРЕЙТИНГ: 7.6\n"),
            raising=False,
        )
        await pipeline.process_incoming_job(job, settings)
        _, kwargs = stub_notifier.send_job.call_args
        assert kwargs.get("silent") is True

    async def test_queued_paused_manual_priority(
        self, job, settings, stub_db, stub_log, monkeypatch
    ):
        """Ручная пауза имеет приоритет над меню (см. PIPELINE.md §4)."""
        from src import llm

        settings.is_paused = True
        settings.is_paused_menu = True
        # dispatch перечитывает settings — стаб возвращает тот же объект
        stub_db["get_settings_cached"].return_value = settings
        stub_db["upsert_and_get_state"].return_value = (True, "pending")
        monkeypatch.setattr(llm, "pre_screen", AsyncMock(return_value=8), raising=False)
        monkeypatch.setattr(
            llm,
            "analyze",
            AsyncMock(return_value="x" * 60 + "\nРЕЙТИНГ: 9\n"),
            raising=False,
        )
        result = await pipeline.process_incoming_job(job, settings)
        assert result == pipeline.PipelineResult.QUEUED_PAUSED
        args, kwargs = stub_db["set_analysis_state_queued"].call_args
        assert "manual" in args or kwargs.get("queued_reason") == "manual" or args[-1] == "manual"

    async def test_queued_paused_menu(self, job, settings, stub_db, stub_log, monkeypatch):
        from src import llm

        settings.is_paused = False
        settings.is_paused_menu = True
        stub_db["get_settings_cached"].return_value = settings
        stub_db["upsert_and_get_state"].return_value = (True, "pending")
        monkeypatch.setattr(llm, "pre_screen", AsyncMock(return_value=8), raising=False)
        monkeypatch.setattr(
            llm,
            "analyze",
            AsyncMock(return_value="x" * 60 + "\nРЕЙТИНГ: 9\n"),
            raising=False,
        )
        result = await pipeline.process_incoming_job(job, settings)
        assert result == pipeline.PipelineResult.QUEUED_PAUSED

    async def test_dispatch_rereads_settings_after_analysis(
        self, job, settings, stub_db, stub_log, monkeypatch
    ):
        """Race-fix: snapshot.is_paused_menu=False, но к финалу dispatch'а в БД
        флаг уже True (юзер зашёл в подменю пока крутился LLM). Ожидаем
        queued_menu, не delivered. Проверяет fix для bug когда уведомления
        прорывались в TG несмотря на is_paused_menu=True (см. BOT.md §10)."""
        from src import llm

        # snapshot — сделан до того как пользователь вошёл в меню
        settings.is_paused = False
        settings.is_paused_menu = False
        # к моменту dispatch'а юзер уже в подменю
        fresh = type(settings)(is_paused=False, is_paused_menu=True)
        stub_db["get_settings_cached"].return_value = fresh
        stub_db["upsert_and_get_state"].return_value = (True, "pending")
        monkeypatch.setattr(llm, "pre_screen", AsyncMock(return_value=8), raising=False)
        monkeypatch.setattr(
            llm,
            "analyze",
            AsyncMock(return_value="x" * 60 + "\nРЕЙТИНГ: 9\n"),
            raising=False,
        )
        result = await pipeline.process_incoming_job(job, settings)
        assert result == pipeline.PipelineResult.QUEUED_PAUSED
        args, _ = stub_db["set_analysis_state_queued"].call_args
        assert args[-1] == "menu"

    async def test_delivered_silent_below_loud_threshold(
        self, job, settings, stub_db, stub_log, stub_notifier, monkeypatch
    ):
        from src import llm

        settings.loud_notification_threshold = 8
        stub_db["upsert_and_get_state"].return_value = (True, "pending")
        monkeypatch.setattr(llm, "pre_screen", AsyncMock(return_value=8), raising=False)
        monkeypatch.setattr(
            llm,
            "analyze",
            AsyncMock(return_value="x" * 60 + "\nРЕЙТИНГ: 6\n"),
            raising=False,
        )
        result = await pipeline.process_incoming_job(job, settings)
        assert result == pipeline.PipelineResult.DELIVERED
        # silent=True потому что rating(6) < loud_threshold(8)
        _, kwargs = stub_notifier.send_job.call_args
        assert kwargs.get("silent") is True
        stub_db["mark_sent"].assert_awaited_once()

    async def test_delivered_loud_above_threshold(
        self, job, settings, stub_db, stub_log, stub_notifier, monkeypatch
    ):
        from src import llm

        settings.loud_notification_threshold = 8
        stub_db["upsert_and_get_state"].return_value = (True, "pending")
        monkeypatch.setattr(llm, "pre_screen", AsyncMock(return_value=8), raising=False)
        monkeypatch.setattr(
            llm,
            "analyze",
            AsyncMock(return_value="x" * 60 + "\nРЕЙТИНГ: 9\n"),
            raising=False,
        )
        result = await pipeline.process_incoming_job(job, settings)
        assert result == pipeline.PipelineResult.DELIVERED
        _, kwargs = stub_notifier.send_job.call_args
        assert kwargs.get("silent") is False

    async def test_pre_screen_runs_before_save(
        self, job, settings, stub_db, stub_log, monkeypatch
    ):
        """Новый порядок: дешёвая нейронка ДО записи в БД (job_received на save)."""
        from src import llm

        stub_db["upsert_and_get_state"].return_value = (True, "pending")
        order = []

        async def mark_pre(*a, **kw):
            order.append("pre_screen")
            return 8

        async def mark_emit(event, *a, **kw):
            order.append(("emit", event))

        monkeypatch.setattr(llm, "pre_screen", AsyncMock(side_effect=mark_pre), raising=False)
        monkeypatch.setattr(
            llm,
            "analyze",
            AsyncMock(return_value="x" * 60 + "\nРЕЙТИНГ: 9\n"),
            raising=False,
        )
        stub_log.side_effect = mark_emit
        await pipeline.process_incoming_job(job, settings)
        emit_idx = next(i for i, x in enumerate(order) if x == ("emit", "job_received"))
        pre_idx = order.index("pre_screen")
        assert pre_idx < emit_idx  # дешёвая нейронка раньше записи/job_received


# --------------------------------------------------------------------------- #
# normalize_payload (PIPELINE.md §3)
# --------------------------------------------------------------------------- #
class TestNormalizePayload:
    def test_returns_job_dataclass(self):
        raw = {
            "upwork_job_id": "~01abc",
            "job_title": "Title",
            "job_description": "Desc",
            "upwork_url": "https://upwork.com/x",
            "budget_type": "Hourly",
            "budget": "$30-60",
            "client_country": "US",
            "client_total_spent": 100,
            "client_total_hires": 5,
            "client_rating": 4.5,
        }
        job = pipeline.normalize_payload(raw)
        assert job.upwork_job_id == "~01abc"
        assert job.job_title == "Title"

    def test_raises_on_missing_required(self):
        # normalize_payload должна явно валидировать обязательные поля
        assert hasattr(pipeline, "normalize_payload"), "normalize_payload отсутствует"
        with pytest.raises((KeyError, ValueError, TypeError)):
            pipeline.normalize_payload({})


# --------------------------------------------------------------------------- #
# safe_process_one (PIPELINE.md §3)
# --------------------------------------------------------------------------- #
class TestSafeProcessOne:
    async def test_normalize_failure_logs_and_returns_llm_failed(
        self, settings, stub_db, stub_log, request_id, monkeypatch
    ):
        monkeypatch.setattr(
            pipeline,
            "normalize_payload",
            lambda raw: (_ for _ in ()).throw(ValueError("bad")),
            raising=False,
        )
        result = await pipeline.safe_process_one({"x": 1}, settings, request_id)
        assert result == pipeline.PipelineResult.LLM_FAILED
        stub_db["save_normalize_failure"].assert_awaited_once()

    async def test_happy_calls_process_incoming_job(
        self, job, settings, stub_db, stub_log, request_id, monkeypatch
    ):
        monkeypatch.setattr(pipeline, "normalize_payload", lambda raw: job, raising=False)
        called = AsyncMock(return_value=pipeline.PipelineResult.DELIVERED)
        monkeypatch.setattr(pipeline, "process_incoming_job", called, raising=False)
        result = await pipeline.safe_process_one({"x": 1}, settings, request_id)
        assert result == pipeline.PipelineResult.DELIVERED
        called.assert_awaited_once_with(job, settings)


# --------------------------------------------------------------------------- #
# _process_batch_async (PIPELINE.md §3)
# --------------------------------------------------------------------------- #
class TestProcessBatchAsync:
    async def test_processes_each_project(self, stub_db, stub_log, monkeypatch, request_id):
        # Заглушка payload
        payload = type("P", (), {"body": type("B", (), {"projects": [{}, {}, {}]})()})()
        called = AsyncMock(return_value=pipeline.PipelineResult.DELIVERED)
        monkeypatch.setattr(pipeline, "safe_process_one", called, raising=False)
        await pipeline._process_batch_async(payload, request_id)
        assert called.await_count == 3

    async def test_marks_request_processed(self, stub_db, stub_log, monkeypatch, request_id):
        payload = type("P", (), {"body": type("B", (), {"projects": []})()})()
        await pipeline._process_batch_async(payload, request_id)
        stub_db["mark_request_processed"].assert_awaited_once_with(request_id)

    async def test_emits_batch_finished(self, stub_db, stub_log, monkeypatch, request_id):
        payload = type("P", (), {"body": type("B", (), {"projects": []})()})()
        await pipeline._process_batch_async(payload, request_id)
        events = [c.args[0] for c in stub_log.call_args_list if c.args]
        assert "batch_finished" in events

    async def test_isolated_failure_does_not_kill_batch(
        self, stub_db, stub_log, monkeypatch, request_id
    ):
        """return_exceptions=True — одна вакансия упала, остальные обработались."""
        payload = type("P", (), {"body": type("B", (), {"projects": [{}, {}]})()})()
        side = [Exception("boom"), pipeline.PipelineResult.DELIVERED]
        monkeypatch.setattr(
            pipeline,
            "safe_process_one",
            AsyncMock(side_effect=side),
            raising=False,
        )
        # не должно бросать
        await pipeline._process_batch_async(payload, request_id)
        stub_db["mark_request_processed"].assert_awaited_once()

    async def test_uses_one_settings_read_per_batch(
        self, stub_db, stub_log, monkeypatch, request_id
    ):
        payload = type("P", (), {"body": type("B", (), {"projects": [{}, {}, {}]})()})()
        monkeypatch.setattr(
            pipeline,
            "safe_process_one",
            AsyncMock(return_value=pipeline.PipelineResult.DELIVERED),
            raising=False,
        )
        await pipeline._process_batch_async(payload, request_id)
        assert stub_db["get_settings_cached"].await_count == 1

    async def test_pipeline_background_timeout_constant(self):
        assert pipeline.PIPELINE_BACKGROUND_TIMEOUT == 120
