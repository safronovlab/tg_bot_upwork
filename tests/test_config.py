"""Тесты config.py — чтение os.environ в Settings.

Соответствие ARCHITECTURE.md §4.1.
"""

from __future__ import annotations

import importlib

import pytest


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setenv("ALLOWED_USER_IDS", "701492865,123456789")
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@h:5432/db")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-from-env")
    monkeypatch.setenv("LLM_MODEL_PRESCREEN_DEFAULT", "x/y")
    monkeypatch.setenv("LLM_MODEL_ANALYSIS_DEFAULT", "a/b")
    monkeypatch.setenv("LLM_MODEL_PRESCREEN_FALLBACK_DEFAULT", "p/q")
    monkeypatch.setenv("LLM_MODEL_ANALYSIS_FALLBACK_DEFAULT", "r/s")
    monkeypatch.setenv("LLM_CONCURRENCY", "5")
    monkeypatch.setenv("PIPELINE_BACKGROUND_TIMEOUT", "120")
    monkeypatch.setenv("LOG_LEVEL", "INFO")
    return monkeypatch


class TestSettings:
    def test_loads_token(self, env):
        from src import config

        importlib.reload(config)
        assert config.TELEGRAM_BOT_TOKEN == "123:abc"

    def test_database_url(self, env):
        from src import config

        importlib.reload(config)
        assert config.DATABASE_URL.startswith("postgresql://")

    def test_allowed_user_ids_parsed_to_set_of_ints(self, env):
        from src import config

        importlib.reload(config)
        assert 701492865 in config.ALLOWED_USER_IDS
        assert 123456789 in config.ALLOWED_USER_IDS
        assert all(isinstance(x, int) for x in config.ALLOWED_USER_IDS)

    def test_llm_concurrency_int(self, env):
        from src import config

        importlib.reload(config)
        assert config.LLM_CONCURRENCY == 5
        assert isinstance(config.LLM_CONCURRENCY, int)

    def test_pipeline_timeout_int(self, env):
        from src import config

        importlib.reload(config)
        assert config.PIPELINE_BACKGROUND_TIMEOUT == 120

    def test_settings_dataclass_frozen(self, env):
        from src import config

        importlib.reload(config)
        # Settings должен быть определён как dataclass(slots, frozen)
        assert hasattr(config, "Settings"), "config.Settings dataclass отсутствует"
        s = config.Settings()
        with pytest.raises((AttributeError, Exception)):
            s.LLM_CONCURRENCY = 999
