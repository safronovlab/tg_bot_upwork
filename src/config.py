"""@dataclass(slots, frozen) — читает os.environ. См. ../ARCHITECTURE.md §4."""

from __future__ import annotations

import os
from dataclasses import dataclass


def _parse_user_ids(raw: str) -> set[int]:
    if not raw:
        return set()
    return {int(x.strip()) for x in raw.split(",") if x.strip()}


# --------------------------------------------------------------------------- #
# Только те значения, которые реально читаются из кода
# (ARCHITECTURE.md §4.2: модели и ключи живут в БД, не в env).
# --------------------------------------------------------------------------- #
TELEGRAM_BOT_TOKEN: str = os.environ.get("TELEGRAM_BOT_TOKEN", "")
ALLOWED_USER_IDS: set[int] = _parse_user_ids(os.environ.get("ALLOWED_USER_IDS", ""))
DATABASE_URL: str = os.environ.get("DATABASE_URL", "")

# Bootstrap для secrets.openrouter_api_key — читается db.get_openrouter_key
# (приоритет: secrets таблица → этот env, ARCHITECTURE.md §4.1).
OPENROUTER_API_KEY: str = os.environ.get("OPENROUTER_API_KEY", "")

LLM_CONCURRENCY: int = int(os.environ.get("LLM_CONCURRENCY", "5"))
PIPELINE_BACKGROUND_TIMEOUT: int = int(os.environ.get("PIPELINE_BACKGROUND_TIMEOUT", "120"))
LOG_LEVEL: str = os.environ.get("LOG_LEVEL", "INFO")

# Authorization header `Bearer <token>` для POST /upwork-lead. Пустая строка =
# проверка отключена (dev / тесты). В prod ОБЯЗАТЕЛЬНО задать.
WEBHOOK_BEARER_TOKEN: str = os.environ.get("WEBHOOK_BEARER_TOKEN", "")

# Безграничный fan-out внутри батча → OOM. Лимит одновременных pipeline-задач
# на batch (защита от webhook'а с 10k проектов).
BATCH_FANOUT_LIMIT: int = int(os.environ.get("BATCH_FANOUT_LIMIT", "50"))

# Circuit breaker для OpenRouter. После N подряд-failure'ов в окне WINDOW
# секунд `_call` фейлится мгновенно на TRIP_S секунд. 0 = выключен.
LLM_CIRCUIT_THRESHOLD: int = int(os.environ.get("LLM_CIRCUIT_THRESHOLD", "10"))
LLM_CIRCUIT_WINDOW_S: int = int(os.environ.get("LLM_CIRCUIT_WINDOW_S", "60"))
LLM_CIRCUIT_TRIP_S: int = int(os.environ.get("LLM_CIRCUIT_TRIP_S", "30"))

# OpenRouter HTTP-Referer / X-Title — отображаются в их leaderboard. Опционально
# (LLM.md §1) — пустая строка означает «не отправлять заголовок».
OPENROUTER_HTTP_REFERER: str = os.environ.get("OPENROUTER_HTTP_REFERER", "")
OPENROUTER_X_TITLE: str = os.environ.get("OPENROUTER_X_TITLE", "Upwork AI Pipeline")


@dataclass(slots=True, frozen=True)
class Settings:
    """Снимок env-конфигурации в одном объекте (ARCHITECTURE.md §4)."""

    TELEGRAM_BOT_TOKEN: str = TELEGRAM_BOT_TOKEN
    DATABASE_URL: str = DATABASE_URL
    OPENROUTER_API_KEY: str = OPENROUTER_API_KEY
    LLM_CONCURRENCY: int = LLM_CONCURRENCY
    PIPELINE_BACKGROUND_TIMEOUT: int = PIPELINE_BACKGROUND_TIMEOUT
    LOG_LEVEL: str = LOG_LEVEL
