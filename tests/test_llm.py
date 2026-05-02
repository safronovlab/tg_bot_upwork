"""Тесты llm.py — OpenRouter клиент, fallback, семафор, prompt caching.

Соответствие LLM.md:
- §2 _build_messages (anthropic vs остальные)
- §2 _call (HTTP error → None, timeout → None, успех → content)
- §2 _with_fallback (primary упал → fallback вызван)
- §2 pre_screen, analyze (60/120 сек таймауты)
- §2 validate_model (200/401/404)
- §3 prompt caching через system+user split
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from src import llm


class TestBuildMessages:
    def test_default_split_system_user(self):
        msgs = llm._build_messages("TEMPLATE", "USER PAYLOAD", "deepseek/r1")
        assert msgs[0]["role"] == "system"
        assert msgs[0]["content"] == "TEMPLATE"
        assert msgs[1]["role"] == "user"
        assert msgs[1]["content"] == "USER PAYLOAD"

    def test_anthropic_gets_cache_control(self):
        msgs = llm._build_messages("TEMPLATE", "USER", "anthropic/claude-haiku-4-5")
        assert msgs[0]["role"] == "system"
        # для anthropic content — список с cache_control
        sys_content = msgs[0]["content"]
        assert isinstance(sys_content, list)
        assert any("cache_control" in part for part in sys_content)


class TestPayloadBuilders:
    def test_prescreen_payload_includes_14_fields(self, job):
        payload = llm.build_prescreen_payload(job)
        # Проверяем что блоки полей присутствуют
        for marker in [
            "[название вакансии]",
            "[описание вакансии]",
            "[вопросы клиента]",
            "[тип бюджета]",
            "[сумма бюджета]",
            "[тип занятости]",
            "[категория клиента (rank)]",
            "[страна клиента]",
            "[рейтинг клиента]",
            "[всего потрачено клиентом, $]",
            "[количество наймов]",
            "[средняя ставка которую платит клиент, $/час]",
            "[год регистрации клиента на Upwork]",
        ]:
            assert marker in payload

    def test_analysis_payload_includes_extra_fields(self, job):
        payload = llm.build_analysis_payload(job)
        assert "[дата публикации вакансии]" in payload
        assert "[отзывы о клиенте от других фрилансеров]" in payload


class TestCall:
    async def test_returns_content_on_200(self, http_session):
        sess = http_session(
            status=200,
            payload={
                "choices": [{"message": {"content": "RATING: 8"}}],
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 20,
                    "prompt_cache_hit_tokens": 80,
                },
            },
        )
        out = await llm._call(sess, "k", "m", "TPL", "U", timeout_s=10)
        assert out == "RATING: 8"

    async def test_returns_none_on_400(self, http_session, stub_log):
        sess = http_session(status=400)
        assert await llm._call(sess, "k", "m", "TPL", "U", timeout_s=10) is None

    async def test_returns_none_on_500(self, http_session):
        sess = http_session(status=500)
        assert await llm._call(sess, "k", "m", "TPL", "U", timeout_s=10) is None

    async def test_returns_none_on_timeout(self, http_session):
        sess = http_session(raise_exc=TimeoutError())
        assert await llm._call(sess, "k", "m", "TPL", "U", timeout_s=1) is None

    async def test_returns_none_on_client_error(self, http_session):
        import aiohttp

        sess = http_session(raise_exc=aiohttp.ClientError("conn"))
        assert await llm._call(sess, "k", "m", "TPL", "U", timeout_s=1) is None

    async def test_emits_llm_call_with_usage(self, http_session, stub_log):
        sess = http_session(
            status=200,
            payload={
                "choices": [{"message": {"content": "x"}}],
                "usage": {
                    "prompt_tokens": 1100,
                    "completion_tokens": 400,
                    "prompt_cache_hit_tokens": 1000,
                },
            },
        )
        await llm._call(sess, "k", "m", "TPL", "U", timeout_s=10)
        events = [c.args[0] for c in stub_log.call_args_list if c.args]
        assert "llm_call" in events


class TestWithFallback:
    async def test_uses_primary_when_succeeds(self, http_session, stub_log, monkeypatch):
        ok = http_session(
            status=200, payload={"choices": [{"message": {"content": "primary-ok"}}], "usage": {}}
        )
        out = await llm._with_fallback(ok, "k", "m1", "m2", "TPL", "U", timeout_s=1)
        assert out == "primary-ok"

    async def test_falls_back_when_primary_fails(self, monkeypatch, stub_log):
        sess = MagicMock()
        call_mock = AsyncMock(side_effect=[None, "fallback-ok"])
        monkeypatch.setattr(llm, "_call", call_mock, raising=False)
        out = await llm._with_fallback(sess, "k", "m1", "m2", "TPL", "U", timeout_s=10)
        assert out == "fallback-ok"
        assert call_mock.await_count == 2

    async def test_emits_llm_fallback_event(self, monkeypatch, stub_log):
        sess = MagicMock()
        monkeypatch.setattr(llm, "_call", AsyncMock(side_effect=[None, "ok"]), raising=False)
        await llm._with_fallback(sess, "k", "m1", "m2", "TPL", "U", timeout_s=10)
        events = [c.args[0] for c in stub_log.call_args_list if c.args]
        assert "llm_fallback" in events

    async def test_no_fallback_call_when_primary_ok(self, monkeypatch, stub_log):
        sess = MagicMock()
        m = AsyncMock(return_value="ok")
        monkeypatch.setattr(llm, "_call", m, raising=False)
        await llm._with_fallback(sess, "k", "m1", "m2", "TPL", "U", timeout_s=10)
        assert m.await_count == 1


class TestPreScreenAnalyze:
    async def test_pre_screen_uses_60s_timeout(self, monkeypatch, job, stub_db):
        captured = {}

        async def fake(session, key, primary, fallback, template, payload, timeout_s):
            captured["t"] = timeout_s
            captured["primary"] = primary
            return "ok"

        monkeypatch.setattr(llm, "_with_fallback", fake, raising=False)
        await llm.pre_screen(MagicMock(), job)
        assert captured["t"] == 60

    async def test_analyze_uses_120s_timeout(self, monkeypatch, job, stub_db):
        captured = {}

        async def fake(session, key, primary, fallback, template, payload, timeout_s):
            captured["t"] = timeout_s
            return "ok"

        monkeypatch.setattr(llm, "_with_fallback", fake, raising=False)
        await llm.analyze(MagicMock(), job)
        assert captured["t"] == 120


class TestValidateModel:
    async def test_200_ok(self, http_session):
        sess = http_session(status=200)
        ok, msg = await llm.validate_model(sess, "k", "vendor/model")
        assert ok is True
        assert msg == "ok"

    async def test_401_invalid_key(self, http_session):
        sess = http_session(status=401)
        ok, msg = await llm.validate_model(sess, "bad", "vendor/model")
        assert ok is False
        assert "ключ" in msg.lower()

    async def test_404_model_not_found(self, http_session):
        sess = http_session(status=404)
        ok, msg = await llm.validate_model(sess, "k", "fake/model")
        assert ok is False
        assert "модель" in msg.lower() or "not found" in msg.lower() or "найден" in msg.lower()


class TestSemaphore:
    def test_concurrency_limit_is_5(self):
        # Семафор = 5 (LLM_CONCURRENCY дефолт)
        assert llm.llm_sem._value == 5
