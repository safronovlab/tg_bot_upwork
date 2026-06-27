"""JSON-формат анализа: parse_analysis (+ legacy-fallback), render_analysis_card,
и сквозной путь через process_incoming_job."""

from __future__ import annotations

from unittest.mock import AsyncMock

from src import llm, notifier, pipeline

VALID_JSON = (
    '{"rating": 8, "summary": "Бэкенд на FastAPI", '
    '"stack_match": "FastAPI + Postgres — наше", '
    '"risks": ["размытое ТЗ"], "verdict": "брать", "reason": "точное совпадение"}'
)


class TestParseAnalysisJson:
    def test_valid_json(self):
        p = pipeline.parse_analysis(VALID_JSON)
        assert p is not None
        assert p["rating"] == 8.0
        assert p["verdict"] == "брать"
        assert p["risks"] == ["размытое ТЗ"]
        assert p["legacy_text"] is None

    def test_json_in_code_fences(self):
        p = pipeline.parse_analysis("```json\n" + VALID_JSON + "\n```")
        assert p is not None and p["rating"] == 8.0 and p["legacy_text"] is None

    def test_json_with_surrounding_prose(self):
        p = pipeline.parse_analysis("Вот результат:\n" + VALID_JSON + "\nКонец.")
        assert p is not None and p["rating"] == 8.0

    def test_rating_clamped(self):
        assert pipeline.parse_analysis('{"rating": 99}')["rating"] == 10.0
        assert pipeline.parse_analysis('{"rating": -5}')["rating"] == 0.0

    def test_float_rating_preserved(self):
        assert pipeline.parse_analysis('{"rating": 4.8}')["rating"] == 4.8

    def test_missing_optional_fields_ok(self):
        p = pipeline.parse_analysis('{"rating": 7}')
        assert p is not None
        assert p["summary"] == "" and p["risks"] == []
        assert p["verdict"] == "брать"  # выведено из rating >= 7

    def test_verdict_derived_when_invalid(self):
        assert pipeline.parse_analysis('{"rating": 3, "verdict": "maybe"}')["verdict"] == "скип"

    def test_risks_string_coerced_to_list(self):
        assert pipeline.parse_analysis('{"rating": 7, "risks": "один риск"}')["risks"] == ["один риск"]

    def test_garbage_returns_none(self):
        assert pipeline.parse_analysis("мусор без числа") is None
        assert pipeline.parse_analysis("") is None
        assert pipeline.parse_analysis(None) is None

    def test_json_without_rating_returns_none(self):
        assert pipeline.parse_analysis('{"foo": "bar"}') is None

    def test_legacy_prose_fallback(self):
        text = "Длинный анализ...\nРЕЙТИНГ: 6\n"
        p = pipeline.parse_analysis(text)
        assert p is not None
        assert p["rating"] == 6.0
        assert p["legacy_text"] == text  # проза сохраняется как есть
        assert p["verdict"] == "скип"


class TestRenderAnalysisCard:
    def test_card_has_rating_and_fields(self, job):
        card = notifier.render_analysis_card(pipeline.parse_analysis(VALID_JSON), job)
        assert "РЕЙТИНГ 8" in card
        assert "брать" not in card  # вердикт убран — только рейтинг
        assert "FastAPI + Postgres — наше" in card
        assert "размытое ТЗ" in card
        assert job.budget in card  # факт из вакансии
        assert job.client_country in card

    def test_card_emoji_by_rating(self, job):
        # редкость WoW: 7→🟢 Uncommon, 8→🔵 Rare, 9→🟣 Epic, 10→🟠 Legendary, ≤4→⚫ Poor
        assert notifier.render_analysis_card({"rating": 10, "verdict": "брать"}, job).startswith("🟠")
        assert notifier.render_analysis_card({"rating": 9, "verdict": "брать"}, job).startswith("🟣")
        assert notifier.render_analysis_card({"rating": 8, "verdict": "брать"}, job).startswith("🔵")
        assert notifier.render_analysis_card({"rating": 7, "verdict": "брать"}, job).startswith("🟢")
        assert notifier.render_analysis_card({"rating": 6, "verdict": "скип"}, job).startswith("⚪")
        assert notifier.render_analysis_card({"rating": 2, "verdict": "скип"}, job).startswith("⚫")

    def test_card_omits_empty_fields(self, job):
        card = notifier.render_analysis_card(
            {"rating": 7, "verdict": "брать", "summary": "", "stack_match": "", "risks": [], "reason": ""},
            job,
        )
        non_empty = [l for l in card.split("\n") if l.strip()]
        # rating + 💰budget + 👤client = 3 непустых строки (между ними пустые)
        assert "РЕЙТИНГ 7" in non_empty[0]
        assert "💰 " in card and "👤 " in card
        assert "📝 " not in card  # нет summary → нет строки
        assert any(job.client_country in ln for ln in non_empty)
        assert len(non_empty) == 3

    def test_field_emoji_markers(self, job):
        card = notifier.render_analysis_card(pipeline.parse_analysis(VALID_JSON), job)
        # цвет рейтинга в шапке + эмодзи-маркер у каждого поля
        assert card.startswith(("🟠", "🟣", "🔵", "🟢", "⚪", "⚫"))
        for marker in ("📝 ", "💰 ", "🧩 ", "👤 ", "⚠️ ", "💬 "):
            assert marker in card


class TestCardOrder:
    async def test_rating_then_title_then_rest(self, bot, job, monkeypatch):
        monkeypatch.setattr(notifier, "bot", bot, raising=False)
        card = "🟢 РЕЙТИНГ 8\n\n📝 суть задачи\n\n💬 причина"
        await notifier.send_job(job, card, silent=True)
        text = bot.send_message.call_args.kwargs["text"]
        i_rating = text.index("РЕЙТИНГ")
        i_title = text.index(job.job_title.upper())
        i_rest = text.index("суть задачи")
        assert i_rating < i_title < i_rest


class TestStageAnalyzeJson:
    async def test_delivered_via_json(
        self, job, settings, stub_db, stub_log, stub_notifier, monkeypatch
    ):
        settings.analysis_threshold = 7
        stub_db["get_settings_cached"].return_value = settings
        stub_db["upsert_and_get_state"].return_value = (True, "pending")
        monkeypatch.setattr(llm, "pre_screen", AsyncMock(return_value=8), raising=False)
        monkeypatch.setattr(llm, "analyze", AsyncMock(return_value=VALID_JSON), raising=False)
        result = await pipeline.process_incoming_job(job, settings)
        assert result == pipeline.PipelineResult.DELIVERED
        args, kwargs = stub_notifier.send_job.call_args
        sent_card = args[1] if len(args) > 1 else kwargs.get("analysis")
        assert "РЕЙТИНГ 8" in sent_card  # карточка собрана из JSON

    async def test_filtered_via_json_below_threshold(
        self, job, settings, stub_db, stub_log, monkeypatch
    ):
        settings.analysis_threshold = 7
        stub_db["upsert_and_get_state"].return_value = (True, "pending")
        monkeypatch.setattr(llm, "pre_screen", AsyncMock(return_value=8), raising=False)
        monkeypatch.setattr(
            llm, "analyze", AsyncMock(return_value='{"rating": 4, "verdict": "скип"}'), raising=False
        )
        result = await pipeline.process_incoming_job(job, settings)
        assert result == pipeline.PipelineResult.FILTERED_ANALYSIS
        stub_db["delete_job"].assert_awaited_once_with(job.upwork_job_id)

    async def test_unparseable_is_llm_failed(self, job, settings, stub_db, stub_log, monkeypatch):
        stub_db["upsert_and_get_state"].return_value = (True, "pending")
        monkeypatch.setattr(llm, "pre_screen", AsyncMock(return_value=8), raising=False)
        monkeypatch.setattr(
            llm, "analyze", AsyncMock(return_value="no json no rating here"), raising=False
        )
        result = await pipeline.process_incoming_job(job, settings)
        assert result == pipeline.PipelineResult.LLM_FAILED
        stub_db["bump_attempts"].assert_awaited()
