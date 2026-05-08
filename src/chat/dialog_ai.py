"""LLM-генерация ответа клиенту в режиме is_paused. См. CHAT.md §6.

Тонкая обёртка над `src.llm._with_fallback` с промтом `dialog_night` из БД.
"""

from __future__ import annotations

from src import db, llm

# Timeout — короткие ответы, R1 не нужен на полную (60s). LLM.md §2.
_LLM_TIMEOUT_S = 60

# Сколько последних сообщений треда подавать как контекст для LLM
_HISTORY_LIMIT = 10


def _format_history_for_llm(history: list[dict], current_message: str) -> str:
    """Превратить список chat_messages-rows в plain-text для подачи в LLM.

    Формат:
        [history-сообщения по очереди]
        Клиент (только что): <current_message>
    """
    lines: list[str] = []
    for row in history:
        direction = row.get("direction") or "?"
        body = (row.get("body_text") or "").strip()
        if not body:
            continue
        ts = row.get("received_at")
        ts_str = ts.strftime("%Y-%m-%d %H:%M") if ts is not None else "?"
        speaker = "Клиент" if direction == "in" else "Я"
        if row.get("ai_generated"):
            speaker = "Я (AI ночью)"
        lines.append(f"[{ts_str}] {speaker}:\n{body}")
    lines.append(f"\n[just now] Клиент:\n{current_message.strip()}")
    return "\n\n".join(lines)


async def generate_reply(
    *,
    email_thread_key: bytes,
    current_message: str,
) -> str | None:
    """Сгенерировать ответ клиенту через `dialog_night` промт.

    Returns:
        Текст ответа (str) если LLM вернул валидный непустой результат.
        None при любой ошибке (таймаут, пустой ответ, LLM не доступен).

    Note: post-validation НЕ делается здесь — это ответственность escalate.py
    после получения текста (CHAT.md §6.3).
    """
    if not current_message.strip():
        return None

    settings = await db.get_settings_cached()
    api_key = await db.get_openrouter_key()
    template = await db.get_prompt_cached("dialog_night")

    if not template or not api_key:
        return None

    history = await db.get_thread_history(email_thread_key, limit=_HISTORY_LIMIT)
    user_payload = _format_history_for_llm(history, current_message)

    session = llm._resolve_session(None)
    return await llm._with_fallback(
        session,
        api_key,
        # Используем те же модели что для analysis (R1 → minimax fallback).
        # Для chat можно отдельный slot но для MVP — не плодим колонок.
        settings.analysis_model,
        settings.analysis_fallback_model,
        template,
        user_payload,
        timeout_s=_LLM_TIMEOUT_S,
    )
