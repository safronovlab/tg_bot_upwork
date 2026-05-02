"""pytest fixtures: моки pool, settings, job, http_session, bot. См. ARCHITECTURE.md §7.7."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest


# --------------------------------------------------------------------------- #
# Settings stub — повторяет поля bot_settings из DATABASE.md §2
# --------------------------------------------------------------------------- #
@dataclass
class FakeSettings:
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


@dataclass
class FakeJob:
    upwork_job_id: str = "~01abc"
    job_title: str = "Senior Python dev"
    job_description: str = "Build us an async pipeline"
    upwork_url: str = "https://www.upwork.com/jobs/~01abc"
    published_date: datetime | None = field(default_factory=lambda: datetime.now(UTC))
    questions: str | None = None
    job_type: str | None = "Full time"
    budget_type: str | None = "Hourly"
    budget: str | None = "$30-$60"
    client_country: str | None = "US"
    client_rank: str | None = "Plus"
    client_total_spent: float | None = 5000.0
    client_total_hires: int | None = 12
    client_avg_rate: float | None = 35.0
    client_rating: float | None = 4.8
    client_registered_at: Any = None
    client_reviews: str | None = None


# --------------------------------------------------------------------------- #
# Async-aware fakes for asyncpg pool / connection
# --------------------------------------------------------------------------- #
class FakePool:
    """Минимальная имитация asyncpg.Pool. Все методы — AsyncMock."""

    def __init__(self):
        self.execute = AsyncMock(return_value="EXECUTE 1")
        self.fetch = AsyncMock(return_value=[])
        self.fetchrow = AsyncMock(return_value=None)
        self.fetchval = AsyncMock(return_value=None)

    def acquire(self):
        conn = MagicMock()
        conn.execute = AsyncMock()
        conn.fetch = AsyncMock(return_value=[])
        conn.fetchrow = AsyncMock(return_value=None)
        conn.fetchval = AsyncMock(return_value=None)

        class _CM:
            async def __aenter__(self_inner):
                return conn

            async def __aexit__(self_inner, *args):
                return False

            def transaction(self_inner):
                class _Tx:
                    async def __aenter__(s):
                        return s

                    async def __aexit__(s, *a):
                        return False

                return _Tx()

        cm = _CM()
        cm.transaction = conn  # для совместимости — тесты могут проверять conn.transaction
        return cm

    async def close(self):
        return None


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture
def settings():
    return FakeSettings()


@pytest.fixture
def job():
    return FakeJob()


@pytest.fixture
def pool():
    return FakePool()


@pytest.fixture
def http_session():
    """Mock aiohttp.ClientSession — `.post(...)` возвращает контекст-менеджер с `status` и `.json()`."""

    def make(status: int = 200, payload: dict | None = None, raise_exc: Exception | None = None):
        sess = MagicMock()
        resp = MagicMock()
        resp.status = status
        resp.json = AsyncMock(return_value=payload or {})
        resp.text = AsyncMock(return_value="")

        class _PostCM:
            async def __aenter__(self_inner):
                if raise_exc is not None:
                    raise raise_exc
                return resp

            async def __aexit__(self_inner, *a):
                return False

        sess.post = MagicMock(return_value=_PostCM())
        return sess

    return make


@pytest.fixture
def bot():
    """Mock aiogram Bot."""
    b = MagicMock()
    b.send_message = AsyncMock()
    b.download = AsyncMock()
    return b


@pytest.fixture
def message():
    """Mock aiogram Message."""
    m = MagicMock()
    m.from_user = MagicMock()
    m.from_user.id = 701492865
    m.text = ""
    m.answer = AsyncMock()
    m.delete = AsyncMock()
    return m


@pytest.fixture
def state():
    """Mock aiogram FSMContext."""
    s = MagicMock()
    data: dict = {}
    s.get_data = AsyncMock(side_effect=lambda: dict(data))
    s.update_data = AsyncMock(side_effect=lambda **kw: data.update(kw))
    s.set_state = AsyncMock()
    s.clear = AsyncMock(side_effect=data.clear)
    s.get_state = AsyncMock(return_value=None)
    s._data = data
    return s


@pytest.fixture
def llm_pre_ok(monkeypatch):
    """Stub llm.pre_screen / llm.analyze возвращают валидные результаты."""
    from src import llm

    monkeypatch.setattr(llm, "pre_screen", AsyncMock(return_value=8), raising=False)
    monkeypatch.setattr(
        llm, "analyze", AsyncMock(return_value="Анализ\nРЕЙТИНГ: 9\n" + "x" * 60), raising=False
    )
    return llm


@pytest.fixture
def stub_db(monkeypatch):
    """Stub в src.db — все CRUD AsyncMock. Возвращает namespace для настройки.

    Дополнительно патчит `db._pool` на FakePool — handlers могут вызывать
    `db._conn()` напрямую (например, для рендера счётчиков в главном меню).
    """
    from src import db

    monkeypatch.setattr(db, "_pool", FakePool(), raising=False)

    stubs = {
        "try_register_request": AsyncMock(return_value=True),
        "save_normalize_failure": AsyncMock(),
        "mark_request_processed": AsyncMock(),
        "upsert_and_get_state": AsyncMock(return_value=(True, "pending")),
        "delete_job": AsyncMock(),
        "mark_failed": AsyncMock(),
        "mark_sent": AsyncMock(),
        "bump_attempts": AsyncMock(),
        "set_pre_rating_and_state": AsyncMock(),
        "set_analysis_and_state": AsyncMock(),
        "set_analysis_state_queued": AsyncMock(),
        "set_favorite": AsyncMock(),
        "get_analysis": AsyncMock(return_value=""),
        "get_card": AsyncMock(return_value=("", "")),
        "get_job_full": AsyncMock(return_value=None),
        "list_favorites": AsyncMock(return_value=[]),
        "clear_all_favorites": AsyncMock(return_value=0),
        "get_settings_cached": AsyncMock(return_value=FakeSettings()),
        "get_prompt_cached": AsyncMock(return_value="<TEMPLATE>"),
        "get_openrouter_key": AsyncMock(return_value="sk-or-v1-test"),
        "insert_event": AsyncMock(),
        "invalidate_settings_cache": AsyncMock(),
        "invalidate_prompt_cache": AsyncMock(),
        "invalidate_secrets_cache": AsyncMock(),
        "get_settings_full": AsyncMock(return_value=FakeSettings()),
        "set_secret": AsyncMock(),
        "get_prompt": AsyncMock(return_value="old prompt"),
        "insert_prompt_history": AsyncMock(),
        "update_prompt": AsyncMock(),
        "get_model": AsyncMock(return_value="vendor/old"),
        "set_model": AsyncMock(),
        "get_setting": AsyncMock(return_value=0),
        "set_setting": AsyncMock(),
        "set_paused_menu": AsyncMock(),
        "drain_queued_by_reason": AsyncMock(return_value=[]),
        "peek_queued_by_reason": AsyncMock(return_value=[]),
        "mark_queued_as_sent": AsyncMock(return_value=0),
        "truncate_jobs": AsyncMock(),
        "count_queued_by_reason_cached": AsyncMock(return_value=0),
        "count_favorites_cached": AsyncMock(return_value=0),
        "count_events": AsyncMock(return_value=0),
        "fetch_events": AsyncMock(return_value=[]),
    }
    for name, mock in stubs.items():
        monkeypatch.setattr(db, name, mock, raising=False)
    return stubs


@pytest.fixture
def stub_log(monkeypatch):
    from src import log as log_mod

    emit = AsyncMock()
    monkeypatch.setattr(log_mod, "emit", emit, raising=False)
    return emit


@pytest.fixture
def stub_notifier(monkeypatch):
    from src import notifier

    send_job = AsyncMock()
    send_job_from_row = AsyncMock()
    send_favorite_card = AsyncMock()
    monkeypatch.setattr(notifier, "send_job", send_job, raising=False)
    monkeypatch.setattr(notifier, "send_job_from_row", send_job_from_row, raising=False)
    monkeypatch.setattr(notifier, "send_favorite_card", send_favorite_card, raising=False)
    return MagicMock(
        send_job=send_job,
        send_job_from_row=send_job_from_row,
        send_favorite_card=send_favorite_card,
    )


@pytest.fixture
def webhook_body_bytes():
    """Сырой webhook payload скрейпера — один проект."""
    import json

    payload = {
        "body": {
            "projects": [
                {
                    "upwork_job_id": "~01abc",
                    "job_title": "Senior Python dev",
                    "job_description": "Build us an async pipeline " * 20,
                    "upwork_url": "https://www.upwork.com/jobs/~01abc",
                    "published_date": "2026-05-01T10:00:00Z",
                    "questions": None,
                    "job_type": "Full time",
                    "budget_type": "Hourly",
                    "budget": "$30-$60",
                    "client_country": "US",
                    "client_rank": "Plus",
                    "client_total_spent": 5000,
                    "client_total_hires": 12,
                    "client_avg_rate": 35,
                    "client_rating": 4.8,
                    "client_registered_at": "2020-03-01",
                    "client_reviews": None,
                }
            ]
        }
    }
    return json.dumps(payload).encode()


@pytest.fixture
def request_id(webhook_body_bytes):
    return hashlib.sha256(webhook_body_bytes).digest()
