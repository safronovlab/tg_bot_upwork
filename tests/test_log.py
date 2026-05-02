"""Тесты log.py — JsonFormatter, emit() и persisted events.

Соответствие ARCHITECTURE.md §6:
- JsonFormatter сериализует через msgspec.json
- emit() пишет и в stdout, и в bot_events для важных событий
- key_updated НЕ кладёт значение секрета в data
"""

from __future__ import annotations

import json
import logging
from unittest.mock import AsyncMock

from src import log as log_mod


class TestJsonFormatter:
    def test_format_includes_required_keys(self):
        fmt = log_mod.JsonFormatter()
        rec = logging.LogRecord("bot", logging.INFO, "f", 1, "job_received", None, None)
        rec.data = {"upwork_job_id": "~01abc"}
        out = fmt.format(rec)
        parsed = json.loads(out)
        assert parsed["event"] == "job_received"
        assert parsed["level"] == "info"
        assert "ts" in parsed
        assert parsed["upwork_job_id"] == "~01abc"

    def test_format_without_data_attr(self):
        fmt = log_mod.JsonFormatter()
        rec = logging.LogRecord("bot", logging.WARNING, "f", 1, "warn_msg", None, None)
        out = fmt.format(rec)
        parsed = json.loads(out)
        assert parsed["event"] == "warn_msg"
        assert parsed["level"] == "warning"


class TestEventsToPersist:
    def test_required_events_in_set(self):
        for name in [
            "job_received",
            "pipeline_finished",
            "batch_finished",
            "normalize_failed",
            "recovery_triggered",
            "llm_failed",
            "llm_fallback",
            "key_updated",
            "model_updated",
            "prompt_updated",
            "threshold_updated",
            "preset_applied",
            "db_truncated",
            "pipeline_failed",
        ]:
            assert name in log_mod.EVENTS_TO_PERSIST


class TestEmit:
    async def test_persisted_event_writes_to_db(self, monkeypatch):
        from src import db

        insert = AsyncMock()
        monkeypatch.setattr(db, "insert_event", insert, raising=False)
        await log_mod.emit("pipeline_finished", upwork_job_id="~01a", result="delivered")
        insert.assert_awaited_once()

    async def test_non_persisted_event_skips_db(self, monkeypatch):
        from src import db

        insert = AsyncMock()
        monkeypatch.setattr(db, "insert_event", insert, raising=False)
        await log_mod.emit("openrouter_http_error", level=logging.WARNING, status=500)
        insert.assert_not_awaited()

    async def test_level_int_mapping(self, monkeypatch):
        from src import db

        captured = {}

        async def fake_insert(level_int, event, data):
            captured["level"] = level_int

        monkeypatch.setattr(db, "insert_event", fake_insert, raising=False)
        await log_mod.emit("llm_failed", level=logging.ERROR, slot="pre_screen")
        assert captured["level"] == 2

    async def test_warning_level_maps_to_1(self, monkeypatch):
        from src import db

        captured = {}

        async def fake_insert(level_int, event, data):
            captured["level"] = level_int

        monkeypatch.setattr(db, "insert_event", fake_insert, raising=False)
        await log_mod.emit("llm_fallback", level=logging.WARNING, from_model="x", to_model="y")
        assert captured["level"] == 1

    async def test_info_level_maps_to_0(self, monkeypatch):
        from src import db

        captured = {}

        async def fake_insert(level_int, event, data):
            captured["level"] = level_int

        monkeypatch.setattr(db, "insert_event", fake_insert, raising=False)
        await log_mod.emit("job_received", upwork_job_id="x")
        assert captured["level"] == 0

    async def test_key_updated_does_not_log_secret(self, monkeypatch):
        """ARCHITECTURE.md §6: значение ключа НЕ кладётся в data."""
        from src import db

        captured = {}

        async def fake_insert(level_int, event, data):
            captured["data"] = data

        monkeypatch.setattr(db, "insert_event", fake_insert, raising=False)
        # API нашего emit'а: для key_updated мы передаём updated_by, не value
        await log_mod.emit("key_updated", field="openrouter_api_key", updated_by=701492865)
        # Проверка что не пытались протащить value
        assert "value" not in captured["data"]
        assert "secret" not in captured["data"]
