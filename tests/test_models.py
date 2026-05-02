"""Тесты models.py — msgspec.Struct (WebhookBody) и dataclass (Job, BotSettings).

Соответствие ARCHITECTURE.md §1: msgspec для парсинга payload, dataclass(slots,frozen)
для внутренних моделей.
"""

from __future__ import annotations

import msgspec
import pytest
from src import models


class TestWebhookBody:
    def test_decodes_valid_payload(self, webhook_body_bytes):
        body = msgspec.json.decode(webhook_body_bytes, type=models.WebhookBody)
        assert hasattr(body, "body")
        assert hasattr(body.body, "projects")
        assert len(body.body.projects) == 1

    def test_raises_on_missing_required(self):
        with pytest.raises(msgspec.ValidationError):
            msgspec.json.decode(b'{"body": "wrong type"}', type=models.WebhookBody)

    def test_struct_is_frozen(self):
        body = msgspec.json.decode(b'{"body":{"projects":[]}}', type=models.WebhookBody)
        # msgspec frozen — попытка set должна падать
        with pytest.raises((AttributeError, TypeError)):
            body.body = "x"


class TestJobDataclass:
    def test_has_required_fields(self):
        from dataclasses import fields

        names = {f.name for f in fields(models.Job)}
        for must in [
            "upwork_job_id",
            "job_title",
            "job_description",
            "upwork_url",
            "published_date",
            "questions",
            "job_type",
            "budget_type",
            "budget",
            "client_country",
            "client_rank",
            "client_total_spent",
            "client_total_hires",
            "client_avg_rate",
            "client_rating",
            "client_registered_at",
            "client_reviews",
        ]:
            assert must in names

    def test_is_frozen(self):
        j = models.Job(upwork_job_id="~01a", job_title="x", job_description="y", upwork_url="u")
        with pytest.raises((AttributeError, Exception)):
            j.upwork_job_id = "~01b"

    def test_uses_slots(self):
        # slots=True означает что нет __dict__
        j = models.Job(upwork_job_id="~01a", job_title="x", job_description="y", upwork_url="u")
        assert not hasattr(j, "__dict__")


class TestBotSettings:
    def test_has_paused_flags(self):
        from dataclasses import fields

        names = {f.name for f in fields(models.BotSettings)}
        assert "is_paused" in names
        assert "is_paused_menu" in names

    def test_has_threshold_fields(self):
        from dataclasses import fields

        names = {f.name for f in fields(models.BotSettings)}
        for f in [
            "pre_screen_threshold",
            "analysis_threshold",
            "loud_notification_threshold",
            "hard_min_client_spent",
            "hard_min_client_rating",
            "hard_min_hires_for_rating",
            "hard_min_budget_hourly",
            "hard_min_budget_fixed",
            "hard_reject_no_hires",
            "hard_max_vacancy_age_h",
        ]:
            assert f in names

    def test_has_model_fields(self):
        from dataclasses import fields

        names = {f.name for f in fields(models.BotSettings)}
        for f in [
            "prescreen_model",
            "analysis_model",
            "prescreen_fallback_model",
            "analysis_fallback_model",
        ]:
            assert f in names
