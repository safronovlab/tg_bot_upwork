"""aiohttp.web routes: POST /upwork-lead + GET /health. См. PIPELINE.md §2 + ARCHITECTURE.md §5.3."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
from typing import TYPE_CHECKING

import msgspec
from aiohttp import web

from src import config, db, log
from src.models import WebhookBody
from src.pipeline import _process_batch_async

if TYPE_CHECKING:
    from aiogram import Bot, Dispatcher


# Хранилище ссылок на background-задачи batch-обработки (RUF006 + drain at shutdown).
_tasks: set[asyncio.Task] = set()

# Флаг graceful shutdown — webhook отвечает 503 (ARCHITECTURE.md §5.3).
_shutting_down = False


def set_shutting_down(value: bool) -> None:
    """Переключить webhook в режим shutdown (ARCHITECTURE.md §5.3)."""
    global _shutting_down
    _shutting_down = value


def _is_shutting_down() -> bool:
    return _shutting_down


def _check_bearer(request: web.Request) -> bool:
    """Проверка `Authorization: Bearer <token>`.

    Если `config.WEBHOOK_BEARER_TOKEN` пустой — auth выключен (dev / тесты).
    В prod env ОБЯЗАТЕЛЬНО задать токен. Сравнение hmac.compare_digest —
    защита от timing-атак.
    """
    expected = config.WEBHOOK_BEARER_TOKEN
    if not expected:
        return True
    raw = request.headers.get("Authorization", "")
    if not raw.startswith("Bearer "):
        return False
    return hmac.compare_digest(raw[7:], expected)


async def upwork_lead(request: web.Request) -> web.Response:
    if _is_shutting_down():
        return web.json_response({"status": "shutting_down"}, status=503)

    if not _check_bearer(request):
        return web.json_response({"status": "unauthorized"}, status=401)

    body_bytes = await request.read()
    idempotency_key = request.headers.get("Idempotency-Key", "")
    request_id = (
        idempotency_key.encode() if idempotency_key else hashlib.sha256(body_bytes).digest()
    )

    # КРИТИЧНО: синхронно регистрируем запрос ДО ответа 200.
    # БД упала → 503 → скрейпер ретраит (PIPELINE.md §2).
    try:
        inserted = await db.try_register_request(request_id)
    except Exception as e:
        log.exception("inbox_insert_failed")
        return web.json_response({"status": "error", "error": str(e)[:100]}, status=503)

    if not inserted:
        return web.json_response({"status": "duplicate"})

    # Парсим payload только для нового запроса
    try:
        payload = msgspec.json.decode(body_bytes, type=WebhookBody)
    except (msgspec.ValidationError, msgspec.DecodeError) as e:
        await db.save_normalize_failure(request_id, body_bytes, str(e))
        await log.emit(
            "normalize_failed",
            level=logging.ERROR,
            request_id=request_id.hex(),
            error=str(e)[:200],
        )
        return web.json_response({"status": "accepted_unparseable"})

    task = asyncio.create_task(_process_batch_async(payload, request_id))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return web.json_response({"status": "accepted"})


async def health(request: web.Request) -> web.Response:
    """GET /health — для Docker / Coolify health-check (ARCHITECTURE.md §5).

    Возвращает 503 если БД недоступна или идёт shutdown — ровно тот сигнал,
    которому orchestrator должен реагировать.
    """
    del request
    if _is_shutting_down():
        return web.json_response({"status": "shutting_down"}, status=503)
    try:
        await db._conn().fetchval("SELECT 1")
    except Exception as e:
        return web.json_response({"status": "db_down", "error": str(e)[:100]}, status=503)
    return web.json_response({"status": "ok", "in_flight": len(_tasks)})


async def start(bot: Bot, dp: Dispatcher, port: int = 8080) -> web.AppRunner:
    """Поднять aiohttp.web сервер с роутами POST /upwork-lead + GET /health.

    Аргументы bot, dp намеренно совпадают с ARCHITECTURE.md §5.2 — оставлены
    под будущую регистрацию webhook aiogram'а в том же AppRunner.
    Возвращает AppRunner для cleanup в lifespan.
    """
    del bot, dp  # пока не используются — см. ARCHITECTURE.md §5.2
    app = web.Application()
    app.router.add_post("/upwork-lead", upwork_lead)
    app.router.add_get("/health", health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=port)  # noqa: S104  # nosec B104 — Docker контейнер должен слушать на всех интерфейсах
    await site.start()
    return runner
