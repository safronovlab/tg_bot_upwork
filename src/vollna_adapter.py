"""Адаптер vollna.com → внутренний формат WebhookBody.

Vollna шлёт payload c корнем `{total, filter, projects[], results_url}`,
без `body`-обёртки и без поля `upwork_job_id`. ID извлекаем из go-link
(`url=https%253A%252F%252Fwww.upwork.com%252Fjobs%252F%257E021234...`),
делая двойной URL-decode и беря последний сегмент пути.

Клиентские поля у Vollna вложены в `client_details.*` — раскладываем на плоские.
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse


class VollnaAdapterError(ValueError):
    """Невалидный payload — нет projects[] или невозможно извлечь upwork_job_id."""


def _extract_upwork_url_and_id(go_url: str) -> tuple[str, str]:
    """Из vollna go-link достать (упомянутый Upwork URL, upwork_job_id).

    Vollna кодирует URL дважды: `parse_qs` снимает первый слой, `unquote` —
    второй. ID — последний сегмент пути после `/jobs/`.
    """
    if not go_url:
        raise VollnaAdapterError("project.url is empty")

    qs = parse_qs(urlparse(go_url).query)
    inner = qs.get("url", [""])[0]
    if not inner:
        raise VollnaAdapterError(f"no `url` query param in go-link: {go_url[:120]}")

    upwork_url = unquote(inner)
    path = urlparse(upwork_url).path.rstrip("/")
    job_id = path.rsplit("/", 1)[-1] if path else ""
    if not job_id:
        raise VollnaAdapterError(f"cannot extract job_id from {upwork_url[:120]}")

    return upwork_url, job_id


def _truncate_to_date(value: Any) -> str | None:
    """ISO-datetime `2025-11-19T00:00:00+00:00` → `2025-11-19`. None → None."""
    if not value or not isinstance(value, str):
        return None
    return value.split("T", 1)[0]


def _stringify(value: Any) -> str | None:
    """Vollna кладёт `reviews` как int → у нас text. None пропускаем."""
    if value is None:
        return None
    return str(value)


def _map_project(p: dict[str, Any]) -> dict[str, Any]:
    """Один Vollna-project → словарь полей WebhookProject."""
    upwork_url, job_id = _extract_upwork_url_and_id(p.get("url", ""))

    cd = p.get("client_details") or {}
    country = cd.get("country") or {}
    country_name = country.get("name") if isinstance(country, dict) else None

    return {
        "upwork_job_id": job_id,
        "job_title": p.get("title"),
        "job_description": p.get("description"),
        "upwork_url": upwork_url,
        "published_date": p.get("published"),
        "questions": p.get("questions"),
        "job_type": p.get("job_type"),
        "budget_type": p.get("budget_type"),
        "budget": p.get("budget"),
        "client_country": country_name,
        "client_rank": cd.get("rank"),
        "client_total_spent": cd.get("total_spent"),
        "client_total_hires": cd.get("total_hires"),
        "client_avg_rate": cd.get("avg_hourly_rate_paid"),
        "client_rating": cd.get("rating"),
        "client_registered_at": _truncate_to_date(cd.get("registered_at")),
        "client_reviews": _stringify(cd.get("reviews")),
    }


def vollna_to_internal_bytes(body_bytes: bytes) -> bytes:
    """Vollna JSON bytes → bytes в нашем формате `{"body":{"projects":[...]}}`.

    Битые projects[] (например, без url) пропускаем с ValueError, чтобы caller
    мог положить весь payload в normalize_failures и отдать 200 accepted_unparseable
    в стиле существующего /upwork-lead.
    """
    try:
        raw: Any = json.loads(body_bytes)
    except json.JSONDecodeError as e:
        raise VollnaAdapterError(f"invalid JSON: {e}") from e

    if not isinstance(raw, dict):
        raise VollnaAdapterError("payload root must be an object")

    projects = raw.get("projects")
    if not isinstance(projects, list):
        raise VollnaAdapterError("`projects` must be an array")

    mapped: list[dict[str, Any]] = []
    errors: list[str] = []
    for i, p in enumerate(projects):
        if not isinstance(p, dict):
            errors.append(f"projects[{i}]: not an object")
            continue
        try:
            mapped.append(_map_project(p))
        except VollnaAdapterError as e:
            errors.append(f"projects[{i}]: {e}")

    if not mapped:
        details = "; ".join(errors[:5]) if errors else "no valid projects"
        raise VollnaAdapterError(f"no projects parsed ({details})")

    return json.dumps({"body": {"projects": mapped}}).encode()
