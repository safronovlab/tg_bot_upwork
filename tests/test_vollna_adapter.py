"""Тесты адаптера vollna.com → внутренний WebhookBody.

Проверяем:
- извлечение upwork_job_id из двойного URL-encode go-link
- маппинг плоских и вложенных полей
- округление client_registered_at до date
- conversion reviews int → str
- невалидные пейлоады → VollnaAdapterError
- результат проходит msgspec-валидацию WebhookBody
"""

from __future__ import annotations

import json

import msgspec
import pytest
from src.models import WebhookBody
from src.vollna_adapter import (
    VollnaAdapterError,
    _extract_upwork_url_and_id,
    vollna_to_internal_bytes,
)

VOLLNA_GO_URL = (
    "https://www.vollna.com/go?module=webhook&uid=37640&tid=32790&pid=74433688"
    "&url=https%253A%252F%252Fwww.upwork.com%252Fjobs%252F%257E022050861570460031848"
)


def _sample_project(**overrides):
    base = {
        "url": VOLLNA_GO_URL,
        "site": "Upwork.com",
        "title": "Pipedrive CRM Automation Specialist",
        "budget": "5 USD",
        "skills": "Zapier, CRM",
        "duration": "Less than 1 month",
        "job_type": "One-time project",
        "published": "2026-05-03T08:56:18+00:00",
        "questions": None,
        "categories": ["Sales & Marketing"],
        "budget_type": "fixed",
        "description": "We need help with Pipedrive automation.",
        "client_details": {
            "rank": "Excellent",
            "rating": 5.0,
            "country": {"name": "Nigeria", "iso_code2": "NG"},
            "reviews": 191,
            "total_hires": 185,
            "total_spent": 1272.8,
            "registered_at": "2025-11-19T00:00:00+00:00",
            "avg_hourly_rate_paid": None,
        },
        "experience_level": "Expert",
    }
    base.update(overrides)
    return base


def _sample_payload(*projects):
    return {
        "total": len(projects),
        "filter": {"id": 0, "url": "https://www.vollna.com/dashboard/filter", "name": "Test"},
        "projects": list(projects) if projects else [_sample_project()],
        "results_url": "https://www.vollna.com/results",
    }


class TestExtractUpworkUrlAndId:
    def test_double_encoded_go_link(self):
        upwork_url, job_id = _extract_upwork_url_and_id(VOLLNA_GO_URL)
        assert upwork_url == "https://www.upwork.com/jobs/~022050861570460031848"
        assert job_id == "~022050861570460031848"

    def test_empty_url_raises(self):
        with pytest.raises(VollnaAdapterError, match="empty"):
            _extract_upwork_url_and_id("")

    def test_go_link_without_url_param_raises(self):
        with pytest.raises(VollnaAdapterError, match="no `url`"):
            _extract_upwork_url_and_id("https://www.vollna.com/go?module=webhook&pid=1")

    def test_url_with_trailing_slash(self):
        url = (
            "https://www.vollna.com/go?url="
            "https%253A%252F%252Fwww.upwork.com%252Fjobs%252F%257E01abc%252F"
        )
        _, job_id = _extract_upwork_url_and_id(url)
        assert job_id == "~01abc"


class TestVollnaToInternal:
    def test_happy_path_produces_valid_webhook_body(self):
        raw = json.dumps(_sample_payload()).encode()
        out = vollna_to_internal_bytes(raw)
        # Парсится msgspec — это и есть наш контракт
        body = msgspec.json.decode(out, type=WebhookBody)
        assert len(body.body.projects) == 1
        p = body.body.projects[0]
        assert p.upwork_job_id == "~022050861570460031848"
        assert p.job_title == "Pipedrive CRM Automation Specialist"
        assert p.upwork_url == "https://www.upwork.com/jobs/~022050861570460031848"
        assert p.client_country == "Nigeria"
        assert p.client_rating == 5.0
        assert p.client_total_spent == 1272.8
        assert p.client_total_hires == 185
        assert p.client_reviews == "191"
        assert str(p.client_registered_at) == "2025-11-19"
        assert str(p.published_date).startswith("2026-05-03")

    def test_multiple_projects_all_mapped(self):
        p1 = _sample_project()
        p2_url = VOLLNA_GO_URL.replace("022050861570460031848", "01xyz1234567890abcde")
        p2 = _sample_project(url=p2_url, title="Other job")
        raw = json.dumps(_sample_payload(p1, p2)).encode()
        body = msgspec.json.decode(vollna_to_internal_bytes(raw), type=WebhookBody)
        assert [p.upwork_job_id for p in body.body.projects] == [
            "~022050861570460031848",
            "~01xyz1234567890abcde",
        ]

    def test_missing_client_details_does_not_crash(self):
        p = _sample_project()
        del p["client_details"]
        raw = json.dumps(_sample_payload(p)).encode()
        body = msgspec.json.decode(vollna_to_internal_bytes(raw), type=WebhookBody)
        proj = body.body.projects[0]
        assert proj.client_country is None
        assert proj.client_rating is None
        assert proj.client_registered_at is None

    def test_country_field_can_be_none(self):
        p = _sample_project()
        p["client_details"]["country"] = None
        raw = json.dumps(_sample_payload(p)).encode()
        body = msgspec.json.decode(vollna_to_internal_bytes(raw), type=WebhookBody)
        assert body.body.projects[0].client_country is None

    def test_invalid_json_raises(self):
        with pytest.raises(VollnaAdapterError, match="invalid JSON"):
            vollna_to_internal_bytes(b"{not json")

    def test_root_not_object_raises(self):
        with pytest.raises(VollnaAdapterError, match="root must be an object"):
            vollna_to_internal_bytes(b"[]")

    def test_no_projects_array_raises(self):
        with pytest.raises(VollnaAdapterError, match="`projects` must be an array"):
            vollna_to_internal_bytes(b'{"total": 0}')

    def test_empty_projects_raises(self):
        with pytest.raises(VollnaAdapterError, match="no projects parsed"):
            vollna_to_internal_bytes(b'{"projects": []}')

    def test_partial_failure_skipped_others_mapped(self):
        good = _sample_project()
        bad = _sample_project(url="not-a-vollna-link")
        raw = json.dumps(_sample_payload(good, bad)).encode()
        body = msgspec.json.decode(vollna_to_internal_bytes(raw), type=WebhookBody)
        assert len(body.body.projects) == 1
        assert body.body.projects[0].upwork_job_id == "~022050861570460031848"

    def test_all_projects_invalid_raises_with_diagnostics(self):
        bad = _sample_project(url="")
        raw = json.dumps(_sample_payload(bad)).encode()
        with pytest.raises(VollnaAdapterError, match="projects\\[0\\]"):
            vollna_to_internal_bytes(raw)
