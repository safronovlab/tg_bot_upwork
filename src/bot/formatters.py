"""split_for_telegram, escape_html, format_job, format_log_rows. См. BOT.md §9, §11."""

from __future__ import annotations

from typing import Any

TELEGRAM_MESSAGE_LIMIT = 4096


def escape_html(text: str | None) -> str:
    if not text:
        return ""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def split_for_telegram(text: str, limit: int = TELEGRAM_MESSAGE_LIMIT) -> list[str]:
    """Делит длинный текст на части по границе строк, не разрывая слова."""
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    while text:
        if len(text) <= limit:
            parts.append(text)
            break
        cut = text.rfind("\n", 0, limit)
        if cut == -1:
            cut = text.rfind(" ", 0, limit)
        if cut == -1:
            cut = limit
        parts.append(text[:cut])
        text = text[cut:].lstrip()
    return parts


def format_job(job: Any, analysis: str) -> str:
    title = (job.job_title or "").upper()
    return f"{analysis}\n\n{title}"


def format_log_rows(rows: list[dict], page: int, total_pages: int) -> str:
    """Форматирует строки bot_events для UI Логи (HTML, parse_mode=HTML).

    Формат:
        📋 <b>Логи</b> · стр N/M

        <code>HH:MM:SS</code> <b>event_name</b>
           <i>k1=v1 · k2=v2</i>

    Лимит вывода — 3500 символов (TG потолок 4096 минус запас под HTML)."""
    import msgspec

    lvl_prefix = {0: "", 1: "⚠️ ", 2: "❌ "}
    header = f"📋 <b>Логи</b> · стр {page + 1}/{max(total_pages, 1)}"
    parts: list[str] = [header, ""]
    total_chars = len(header) + 2

    for r in rows:
        ts = r.get("ts")
        # `ts` приходит как datetime — берём только время HH:MM:SS из str().
        ts_str = str(ts)[11:19] if ts is not None else "?"
        lvl = int(r.get("level", 0))
        prefix = lvl_prefix.get(lvl, "")
        event = escape_html(str(r.get("event") or ""))
        # asyncpg отдаёт jsonb как строку — парсим
        raw = r.get("data") or {}
        if isinstance(raw, str):
            try:
                raw = msgspec.json.decode(raw.encode())
            except Exception:
                raw = {}
        if not isinstance(raw, dict):
            raw = {}

        kv_pieces: list[str] = []
        for k, v in raw.items():
            if v is None:
                continue
            # Пропускаем шумные/длинные технические поля
            if k in ("request_id",):
                continue
            v_str = str(v)
            if len(v_str) > 50:
                v_str = v_str[:50] + "…"
            kv_pieces.append(f"{escape_html(k)}={escape_html(v_str)}")
        kv = " · ".join(kv_pieces)
        if len(kv) > 250:
            kv = kv[:250] + "…"

        line1 = f"<code>{ts_str}</code> {prefix}<b>{event}</b>"
        block_len = len(line1) + 1
        line2 = ""
        if kv:
            line2 = f"   <i>{kv}</i>"
            block_len += len(line2) + 1
        block_len += 1  # blank line separator

        if total_chars + block_len > 3500:
            parts.append("…")
            break
        parts.append(line1)
        if line2:
            parts.append(line2)
        parts.append("")
        total_chars += block_len
    return "\n".join(parts)
