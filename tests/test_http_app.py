"""Тесты webhook handler — POST /upwork-lead.

Соответствие PIPELINE.md §2:
- INSERT в webhook_inbox синхронно ДО ответа 200
- request_id из Idempotency-Key, иначе sha256(body)
- ошибка БД → 503 (скрейпер ретраит)
- битый JSON → save_normalize_failure → 200 accepted_unparseable
- background task для _process_batch_async
"""
from __future__ import annotations

import asyncio
import hashlib
from unittest.mock import AsyncMock, MagicMock

from src import http_app


def make_request(body: bytes, headers: dict | None = None):
    """Имитация aiohttp.web.Request."""
    req = MagicMock()
    req.read = AsyncMock(return_value=body)
    req.headers = headers or {}
    return req


class TestUpworkLeadHandler:
    async def test_accepted_for_new_request(self, webhook_body_bytes, stub_db, stub_log, monkeypatch):
        monkeypatch.setattr(http_app, "_process_batch_async", AsyncMock(), raising=False)
        req = make_request(webhook_body_bytes)
        resp = await http_app.upwork_lead(req)
        assert resp.status == 200
        body = resp.text
        assert "accepted" in body and "duplicate" not in body and "unparseable" not in body

    async def test_duplicate_short_circuit(self, webhook_body_bytes, stub_db, stub_log, monkeypatch):
        stub_db["try_register_request"].return_value = False
        monkeypatch.setattr(http_app, "_process_batch_async", AsyncMock(), raising=False)
        req = make_request(webhook_body_bytes)
        resp = await http_app.upwork_lead(req)
        assert resp.status == 200
        assert "duplicate" in resp.text

    async def test_db_error_returns_5xx(self, webhook_body_bytes, stub_db, stub_log, monkeypatch):
        stub_db["try_register_request"].side_effect = Exception("DB down")
        req = make_request(webhook_body_bytes)
        resp = await http_app.upwork_lead(req)
        assert 500 <= resp.status < 600

    async def test_request_id_from_idempotency_key(self, webhook_body_bytes, stub_db, stub_log, monkeypatch):
        monkeypatch.setattr(http_app, "_process_batch_async", AsyncMock(), raising=False)
        req = make_request(webhook_body_bytes, headers={"Idempotency-Key": "custom-key-42"})
        await http_app.upwork_lead(req)
        called_id = stub_db["try_register_request"].call_args.args[0]
        assert called_id == b"custom-key-42"

    async def test_request_id_falls_back_to_sha256(self, webhook_body_bytes, stub_db, stub_log, monkeypatch):
        monkeypatch.setattr(http_app, "_process_batch_async", AsyncMock(), raising=False)
        req = make_request(webhook_body_bytes)
        await http_app.upwork_lead(req)
        called_id = stub_db["try_register_request"].call_args.args[0]
        assert called_id == hashlib.sha256(webhook_body_bytes).digest()
        assert len(called_id) == 32

    async def test_invalid_json_saved_to_normalize_failures(self, stub_db, stub_log, monkeypatch):
        monkeypatch.setattr(http_app, "_process_batch_async", AsyncMock(), raising=False)
        req = make_request(b"{not valid json")
        resp = await http_app.upwork_lead(req)
        assert resp.status == 200
        assert "accepted_unparseable" in resp.text
        stub_db["save_normalize_failure"].assert_awaited_once()

    async def test_background_task_scheduled(self, webhook_body_bytes, stub_db, stub_log, monkeypatch):
        sched = AsyncMock()
        monkeypatch.setattr(http_app, "_process_batch_async", sched, raising=False)
        req = make_request(webhook_body_bytes)
        await http_app.upwork_lead(req)
        # дать loop'у шанс начать таск
        await asyncio.sleep(0)
        sched.assert_called()  # background, не await — но был вызван

    async def test_normalize_failed_event_emitted(self, stub_db, stub_log, monkeypatch):
        monkeypatch.setattr(http_app, "_process_batch_async", AsyncMock(), raising=False)
        req = make_request(b"{not valid")
        await http_app.upwork_lead(req)
        events = [c.args[0] for c in stub_log.call_args_list if c.args]
        assert "normalize_failed" in events

    async def test_db_failure_does_not_schedule_batch(self, webhook_body_bytes, stub_db, stub_log, monkeypatch):
        stub_db["try_register_request"].side_effect = Exception("DB down")
        sched = AsyncMock()
        monkeypatch.setattr(http_app, "_process_batch_async", sched, raising=False)
        req = make_request(webhook_body_bytes)
        await http_app.upwork_lead(req)
        sched.assert_not_called()

    async def test_duplicate_does_not_schedule_batch(self, webhook_body_bytes, stub_db, stub_log, monkeypatch):
        stub_db["try_register_request"].return_value = False
        sched = AsyncMock()
        monkeypatch.setattr(http_app, "_process_batch_async", sched, raising=False)
        req = make_request(webhook_body_bytes)
        await http_app.upwork_lead(req)
        sched.assert_not_called()


# --- Vollna endpoint --------------------------------------------------------- #


def _vollna_payload_bytes() -> bytes:
    """Минимальный валидный Vollna webhook — 1 проект."""
    import json

    project = {
        "url": (
            "https://www.vollna.com/go?module=webhook&pid=1&url="
            "https%253A%252F%252Fwww.upwork.com%252Fjobs%252F%257E01abc"
        ),
        "title": "T",
        "description": "D",
        "published": "2026-05-03T08:00:00+00:00",
        "job_type": "Hourly",
        "budget_type": "hourly",
        "budget": "$10-$20",
        "client_details": {
            "rank": "Plus",
            "rating": 4.9,
            "country": {"name": "US", "iso_code2": "US"},
            "reviews": 5,
            "total_hires": 3,
            "total_spent": 100.0,
            "registered_at": "2024-01-01T00:00:00+00:00",
            "avg_hourly_rate_paid": 30.0,
        },
    }
    return json.dumps({"projects": [project]}).encode()


class TestUpworkLeadVollnaHandler:
    async def test_accepted_for_valid_vollna_payload(self, stub_db, stub_log, monkeypatch):
        monkeypatch.setattr(http_app, "_process_batch_async", AsyncMock(), raising=False)
        req = make_request(_vollna_payload_bytes())
        resp = await http_app.upwork_lead_vollna(req)
        assert resp.status == 200
        assert "accepted" in resp.text and "unparseable" not in resp.text

    async def test_unauthorized_without_bearer(self, stub_db, stub_log, monkeypatch):
        from src import config

        monkeypatch.setattr(config, "WEBHOOK_BEARER_TOKEN", "secret-token", raising=False)
        req = make_request(_vollna_payload_bytes())
        resp = await http_app.upwork_lead_vollna(req)
        assert resp.status == 401

    async def test_authorized_with_bearer(self, stub_db, stub_log, monkeypatch):
        from src import config

        monkeypatch.setattr(config, "WEBHOOK_BEARER_TOKEN", "secret-token", raising=False)
        monkeypatch.setattr(http_app, "_process_batch_async", AsyncMock(), raising=False)
        req = make_request(_vollna_payload_bytes(), headers={"Authorization": "Bearer secret-token"})
        resp = await http_app.upwork_lead_vollna(req)
        assert resp.status == 200

    async def test_vollna_invalid_format_saved_to_normalize_failures(self, stub_db, stub_log, monkeypatch):
        monkeypatch.setattr(http_app, "_process_batch_async", AsyncMock(), raising=False)
        # Наш собственный формат отправлен в Vollna-endpoint — нет `projects` массива
        # после преобразования через адаптер → VollnaAdapterError
        req = make_request(b'{"body": {"projects": []}}')
        resp = await http_app.upwork_lead_vollna(req)
        assert resp.status == 200
        assert "accepted_unparseable" in resp.text
        stub_db["save_normalize_failure"].assert_awaited_once()

    async def test_vollna_duplicate_short_circuit(self, stub_db, stub_log, monkeypatch):
        stub_db["try_register_request"].return_value = False
        monkeypatch.setattr(http_app, "_process_batch_async", AsyncMock(), raising=False)
        req = make_request(_vollna_payload_bytes())
        resp = await http_app.upwork_lead_vollna(req)
        assert resp.status == 200
        assert "duplicate" in resp.text

    async def test_background_task_scheduled_for_vollna(self, stub_db, stub_log, monkeypatch):
        sched = AsyncMock()
        monkeypatch.setattr(http_app, "_process_batch_async", sched, raising=False)
        req = make_request(_vollna_payload_bytes())
        await http_app.upwork_lead_vollna(req)
        await asyncio.sleep(0)
        sched.assert_called()
