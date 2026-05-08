"""Pre-gate (до LLM) + post-validator (после LLM). См. CHAT.md §6.3.

Цель — гарантировать что AI **никогда** не закроет сделку, не назовёт цену
и не обещает сроки клиенту. Три слоя защиты:
  1. Pre-gate: hot keywords в сообщении клиента → AI не вызывается, escalate
  2. Sentinel: AI вернул `__ESCALATE__: <reason>` → не отправляем
  3. Post-validate: запрещённые слова/паттерны в тексте AI → не отправляем
"""

from __future__ import annotations

import re

# --------------------------------------------------------------------------- #
# Pre-gate: триггеры в сообщении клиента
# --------------------------------------------------------------------------- #
# Hot keywords — деньги/контракт/готовность к hire.
# Совпадение по word boundary (re.IGNORECASE), любое одно → escalate.
_HOT_KEYWORDS: tuple[str, ...] = (
    # Деньги / ставки
    "price", "budget", "quote", "rate", "cost",
    "hourly", "fixed", "$", "€", "£",
    # Готовность к hire / контракт
    "proposal", "contract", "ready to hire",
    "let's get started", "let me know when",
    "when can you start", "send me a", "sign", "deal",
    "agreement", "scope of work", "sow",
    # Созвон / встреча
    "call", "zoom", "meeting", "meet up", "google meet",
    "skype", "teams", "discord call",
)

# Скомпилированные регексы (word boundary + IGNORECASE)
_HOT_KEYWORDS_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(k) for k in _HOT_KEYWORDS) + r")\b",
    re.IGNORECASE,
)
# Долларовый знак — отдельно (он не word-character, не работает с \b)
_MONEY_SYMBOL_RE = re.compile(r"[$€£]\s*\d|\d+\s*(?:USD|EUR|GBP|/hour|/hr)\b", re.IGNORECASE)

# Не-английский текст — простая heuristic: >25% символов в Cyrillic / CJK / Arabic
_NON_LATIN_RE = re.compile(r"[Ѐ-ӿԀ-ԯ一-鿿؀-ۿ]")

# Длина: >300 слов = вероятно сложный кейс, AI не справится
_MAX_WORDS = 300


def pre_gate(text: str) -> str | None:
    """До LLM-вызова: проверка триггеров escalate.

    Returns:
        None — можно дать AI генерить.
        text — короткая причина escalate (для записи в DB и логов).
    """
    if not text or not text.strip():
        return "empty_message"

    # 1. Hot keywords
    m = _HOT_KEYWORDS_RE.search(text)
    if m:
        return f"hot_keyword:{m.group(0).lower()[:30]}"

    # 2. Money pattern (символ + цифра, "$5", "5 USD")
    m = _MONEY_SYMBOL_RE.search(text)
    if m:
        return f"money_pattern:{m.group(0).strip()[:30]}"

    # 3. Не-английский текст (>25% не-латинских букв)
    non_latin = len(_NON_LATIN_RE.findall(text))
    total_letters = sum(1 for c in text if c.isalpha())
    if total_letters > 0 and non_latin / total_letters > 0.25:
        return "non_english"

    # 4. Длина
    word_count = len(text.split())
    if word_count > _MAX_WORDS:
        return f"too_long:{word_count}_words"

    return None


# --------------------------------------------------------------------------- #
# Post-validator: проверка ответа AI перед отправкой
# --------------------------------------------------------------------------- #
# Banlist слов — AI-маркеры, ловятся клиентом / детекторами.
_AI_VOCABULARY_BANLIST: tuple[str, ...] = (
    "robust", "seamless", "delve", "tailored", "comprehensive",
    "leverage", "utilize", "optimize", "navigate", "foster",
    "align", "streamline", "ensure", "facilitate", "ecosystem",
    "synergy", "thrilled", "embark", "bespoke", "intricate",
    "nuanced", "holistic", "paramount", "pivotal",
)
_AI_VOCAB_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(w) for w in _AI_VOCABULARY_BANLIST) + r")\b",
    re.IGNORECASE,
)

# Em-dash / en-dash — главный AI-маркер (CHAT.md §6.2)
_DASH_RE = re.compile(r"[–—]")

# Деньги в ответе AI — блокируем
_AI_MONEY_RE = re.compile(r"[$€£]\s*\d|\d+\s*(?:USD|EUR|GBP|/hour|/hr)\b", re.IGNORECASE)

# Time commits — обязательства которые AI не вправе давать
_TIME_COMMIT_RE = re.compile(
    r"\b(?:will\s+(?:fix|deliver|finish|complete|do|get)|"
    r"can\s+(?:wrap|finish|complete|deliver)|"
    r"in\s+\d+\s+(?:hour|day|week|month)|"
    r"by\s+(?:tomorrow|monday|tuesday|wednesday|thursday|friday|"
    r"saturday|sunday|EOD|end of day)|"
    r"\d+\s*(?:h|hr|hrs|hours?|d|days?|w|weeks?|m|months?))",
    re.IGNORECASE,
)

# Multiple questions — AI должен задавать максимум один вопрос
def _question_count(text: str) -> int:
    return text.count("?")


# Sentinel — если AI вернул специальную строку, это явный escalate-сигнал
_ESCALATE_SENTINEL = "__ESCALATE__"


def post_validate(text: str) -> str | None:
    """После LLM-генерации: проверка что AI не сказал запрещённое.

    Returns:
        None — текст ok, можно отправлять.
        str — короткая причина почему текст НЕ отправляется.
    """
    if not text or not text.strip():
        return "empty_response"

    # 1. AI explicit escalate
    if _ESCALATE_SENTINEL in text:
        # Извлекаем причину если есть, иначе обобщённая
        m = re.search(rf"{re.escape(_ESCALATE_SENTINEL)}:\s*(\w+)", text)
        return f"ai_escalate:{m.group(1) if m else 'unknown'}"

    # 2. Em-dash / en-dash
    if _DASH_RE.search(text):
        return "em_dash_detected"

    # 3. AI vocabulary banlist
    m = _AI_VOCAB_RE.search(text)
    if m:
        return f"ai_vocab:{m.group(0).lower()}"

    # 4. Money in response (AI не имеет права называть цены)
    m = _AI_MONEY_RE.search(text)
    if m:
        return f"money_in_response:{m.group(0).strip()[:30]}"

    # 5. Time commits
    m = _TIME_COMMIT_RE.search(text)
    if m:
        return f"time_commit:{m.group(0).lower()[:30]}"

    # 6. Multiple questions
    if _question_count(text) > 1:
        return "multiple_questions"

    # 7. Длина 2-3 строки максимум (по dialog_night.md). Жёсткий cap.
    if len(text) > 600:
        return f"too_long:{len(text)}_chars"

    return None
