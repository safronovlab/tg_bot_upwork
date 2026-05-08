"""Тесты pre-gate + post-validator. См. CHAT.md §6.3."""

from __future__ import annotations

from src.chat import escalate


# --------------------------------------------------------------------------- #
# Pre-gate (сообщение клиента → можно ли давать AI отвечать)
# --------------------------------------------------------------------------- #
class TestPreGate:
    def test_safe_message_passes(self) -> None:
        """Обычный клиентский вопрос → AI можно генерить."""
        text = "Hi Oleg, are the duplicates happening on cron or webhook trigger?"
        assert escalate.pre_gate(text) is None

    def test_empty_message_blocks(self) -> None:
        assert escalate.pre_gate("") == "empty_message"
        assert escalate.pre_gate("   \n  ") == "empty_message"

    def test_price_keyword_blocks(self) -> None:
        text = "What's your price for this work?"
        reason = escalate.pre_gate(text)
        assert reason is not None
        assert "hot_keyword" in reason

    def test_dollar_amount_blocks(self) -> None:
        text = "I have a $500 budget for this fix"
        reason = escalate.pre_gate(text)
        assert reason is not None
        # Может попасть либо в hot_keyword (budget) либо в money_pattern — оба ок
        assert "hot_keyword" in reason or "money_pattern" in reason

    def test_when_can_you_start_blocks(self) -> None:
        text = "Looks great, when can you start?"
        reason = escalate.pre_gate(text)
        assert reason is not None
        assert "hot_keyword" in reason

    def test_zoom_meeting_blocks(self) -> None:
        text = "Can we hop on a quick zoom call to discuss?"
        reason = escalate.pre_gate(text)
        assert reason is not None
        assert "hot_keyword" in reason

    def test_non_english_blocks(self) -> None:
        """Не-английский текст → escalate (AI настроен на английский)."""
        text = "Привет, можем обсудить твоё предложение по проекту?"
        reason = escalate.pre_gate(text)
        assert reason == "non_english"

    def test_too_long_blocks(self) -> None:
        text = "word " * 350  # 350 слов > 300
        reason = escalate.pre_gate(text)
        assert reason is not None
        assert "too_long" in reason

    def test_normal_length_passes(self) -> None:
        text = "Hi, " + ("nice ok " * 50) + " thanks"  # ~110 слов, всё ОК
        # Проверяем что НЕ из-за длины блокирует (могут быть другие причины — нет)
        result = escalate.pre_gate(text)
        # Не должно содержать too_long
        assert result is None or "too_long" not in (result or "")


# --------------------------------------------------------------------------- #
# Post-validator (текст AI → можно ли отправлять клиенту)
# --------------------------------------------------------------------------- #
class TestPostValidate:
    def test_safe_response_passes(self) -> None:
        text = (
            "got it on the timing. quick one before i dig in tomorrow: "
            "is the duplicate firing on the same workflow run or different ones"
        )
        assert escalate.post_validate(text) is None

    def test_empty_response_blocks(self) -> None:
        assert escalate.post_validate("") == "empty_response"

    def test_em_dash_blocks(self) -> None:
        text = "got it on the bug — will look at this in the morning"
        assert escalate.post_validate(text) == "em_dash_detected"

    def test_en_dash_blocks(self) -> None:
        text = "got it on the bug – will look in the morning"
        assert escalate.post_validate(text) == "em_dash_detected"

    def test_ai_vocabulary_blocks(self) -> None:
        text = "I'll leverage the existing code for a robust solution"
        reason = escalate.post_validate(text)
        assert reason is not None
        assert "ai_vocab" in reason

    def test_money_in_response_blocks(self) -> None:
        text = "happy to do this for $500, will look at it tomorrow"
        reason = escalate.post_validate(text)
        assert reason is not None
        assert "money_in_response" in reason

    def test_time_commit_blocks(self) -> None:
        text = "will fix this by tomorrow morning"
        reason = escalate.post_validate(text)
        assert reason is not None
        assert "time_commit" in reason

    def test_multiple_questions_blocks(self) -> None:
        text = "got it. quick one: cron or webhook? and is this on RDS or self-hosted?"
        assert escalate.post_validate(text) == "multiple_questions"

    def test_too_long_blocks(self) -> None:
        text = "x " * 400  # >600 chars
        reason = escalate.post_validate(text)
        assert reason is not None
        assert "too_long" in reason

    def test_explicit_escalate_sentinel_blocks(self) -> None:
        text = "__ESCALATE__: pricing"
        reason = escalate.post_validate(text)
        assert reason is not None
        assert "ai_escalate" in reason

    def test_escalate_in_middle_of_text_blocks(self) -> None:
        text = "Some intro __ESCALATE__: client_wants_call rest of text"
        reason = escalate.post_validate(text)
        assert reason is not None
        assert "ai_escalate" in reason
