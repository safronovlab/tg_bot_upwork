"""msgspec.Struct (payload скрейпера) + dataclass(slots, frozen) (внутренние). См. PIPELINE.md §2."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

import msgspec


# --------------------------------------------------------------------------- #
# Внешний payload скрейпера — msgspec.Struct, frozen, gc=False
# --------------------------------------------------------------------------- #
class WebhookProject(msgspec.Struct, frozen=True, gc=False, kw_only=True):
    upwork_job_id: str
    job_title: str | None = None
    job_description: str | None = None
    upwork_url: str | None = None
    published_date: datetime | None = None
    questions: str | None = None
    job_type: str | None = None
    budget_type: str | None = None
    budget: str | None = None
    client_country: str | None = None
    client_rank: str | None = None
    client_total_spent: float | None = None
    client_total_hires: int | None = None
    client_avg_rate: float | None = None
    client_rating: float | None = None
    client_registered_at: date | None = None
    client_reviews: str | None = None


class WebhookInner(msgspec.Struct, frozen=True, gc=False):
    projects: list[WebhookProject]


class WebhookBody(msgspec.Struct, frozen=True, gc=False):
    body: WebhookInner


# --------------------------------------------------------------------------- #
# Внутренние модели — dataclass(slots=True, frozen=True)
# --------------------------------------------------------------------------- #
@dataclass(slots=True, frozen=True)
class Job:
    upwork_job_id: str
    job_title: str
    job_description: str
    upwork_url: str
    published_date: datetime | None = None
    questions: str | None = None
    job_type: str | None = None
    budget_type: str | None = None
    budget: str | None = None
    client_country: str | None = None
    client_rank: str | None = None
    client_total_spent: float | None = None
    client_total_hires: int | None = None
    client_avg_rate: float | None = None
    client_rating: float | None = None
    client_registered_at: date | None = None
    client_reviews: str | None = None


@dataclass(slots=True, frozen=True)
class BotSettings:
    is_paused: bool = False
    is_paused_menu: bool = False

    pre_screen_threshold: int = 0
    analysis_threshold: int = 0

    hard_min_client_spent: float = 0
    hard_min_client_rating: float = 0
    hard_min_hires_for_rating: int = 3
    hard_min_budget_hourly: float = 0
    hard_min_budget_fixed: float = 0
    hard_reject_no_hires: bool = False
    hard_max_vacancy_age_h: int = 0

    prescreen_model: str = "xiaomi/mimo-v2-flash"
    analysis_model: str = "deepseek/deepseek-r1-0528"
    prescreen_fallback_model: str = "deepseek/deepseek-v4-flash"
    analysis_fallback_model: str = "minimax/minimax-m2.5"

    loud_notification_threshold: int = 8
