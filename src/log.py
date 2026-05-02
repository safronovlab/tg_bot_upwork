"""stdlib logging + msgspec.json + emit() в bot_events. См. ../ARCHITECTURE.md §6."""

from __future__ import annotations

import logging
import sys

import msgspec


class JsonFormatter(logging.Formatter):
    def format(self, r: logging.LogRecord) -> str:
        # r.getMessage() интерполирует args в шаблон msg (иначе видим литеральный %s).
        try:
            event = r.getMessage()
        except Exception:
            event = str(r.msg)
        rec: dict = {
            "ts": r.created,
            "level": r.levelname.lower(),
            "event": event,
        }
        if hasattr(r, "data"):
            rec.update(r.data)
        if r.exc_info:
            rec["exc"] = self.formatException(r.exc_info)
        return msgspec.json.encode(rec).decode()


_handler = logging.StreamHandler(sys.stdout)
_handler.setFormatter(JsonFormatter())
logging.basicConfig(handlers=[_handler], level=logging.INFO)
log = logging.getLogger("bot")


EVENTS_TO_PERSIST: set[str] = {
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
}


_LEVEL_TO_INT: dict[int, int] = {
    logging.INFO: 0,
    logging.WARNING: 1,
    logging.ERROR: 2,
}


async def emit(event: str, level: int = logging.INFO, **data) -> None:
    """Записать событие и в stdout, и (если важное) в bot_events для UI Логи."""
    log.log(level, event, extra={"data": data})
    if event in EVENTS_TO_PERSIST:
        from src import db

        lvl_int = _LEVEL_TO_INT.get(level, 0)
        try:
            await db.insert_event(lvl_int, event, data)
        except Exception:
            log.exception("insert_event_failed")


def exception(event: str, **data) -> None:
    """Удобный shortcut для исключений в синхронном контексте."""
    log.exception(event, extra={"data": data})
