"""Тесты Upwork email parser. Калибровка по реальному sample (May 2026).

Sample структура (из скриншота):
    From: "Jesus G. via Upwork" <email@upwork.com>
    Reply-To: 6187135414865423809207175010615ver2@mg.upwork.com
    Subject: Jesus G. sent you a message
    Body: "Unread message from Jesus G. about Tool Development for Bulk Image
           Upload and Color Management

           Jesus G.
           8:43 PM UTC, 6 May 2026

           Yea naw i can't do 3k i wasn't trying to do more then 5-700\\$

           View on Upwork: ...
           Or, you can respond by replying to this email."
"""

from __future__ import annotations

from src.chat import parser


def _build_raw_email(
    *,
    from_header: str,
    subject: str,
    body: str,
    message_id: str = "<test123@upwork.com>",
    in_reply_to: str | None = None,
    reply_to: str | None = None,
) -> bytes:
    """Собрать минимальный RFC 822 email для тестов парсера."""
    headers = [
        f"From: {from_header}",
        f"Subject: {subject}",
        f"Message-ID: {message_id}",
        "MIME-Version: 1.0",
        'Content-Type: text/plain; charset="utf-8"',
    ]
    if in_reply_to:
        headers.append(f"In-Reply-To: {in_reply_to}")
    if reply_to:
        headers.append(f"Reply-To: {reply_to}")
    return ("\r\n".join(headers) + "\r\n\r\n" + body).encode("utf-8")


# --------------------------------------------------------------------------- #
# Sender whitelist
# --------------------------------------------------------------------------- #
class TestSenderWhitelist:
    def test_upwork_com_passes(self) -> None:
        raw = _build_raw_email(
            from_header='"Jesus G. via Upwork" <noreply@upwork.com>',
            subject="Jesus G. sent you a message",
            body="Unread message from Jesus G. about Test Job\n\nHi there",
        )
        result = parser.parse_email(raw)
        assert result is not None

    def test_mg_upwork_com_passes(self) -> None:
        raw = _build_raw_email(
            from_header="<notifications@mg.upwork.com>",
            subject="Jesus G. sent you a message",
            body="Unread message from Jesus G. about Test\n\nHi",
        )
        result = parser.parse_email(raw)
        assert result is not None

    def test_external_sender_rejected(self) -> None:
        """Phishing protection: только @upwork.com / @mg.upwork.com."""
        raw = _build_raw_email(
            from_header='"Phisher" <attacker@evil.com>',
            subject="Jesus G. sent you a message",
            body="Click here to claim",
        )
        result = parser.parse_email(raw)
        assert result is None

    def test_lookalike_domain_rejected(self) -> None:
        raw = _build_raw_email(
            from_header="<spam@upwork.evil.com>",
            subject="anything",
            body="anything",
        )
        result = parser.parse_email(raw)
        assert result is None


# --------------------------------------------------------------------------- #
# From-name extraction
# --------------------------------------------------------------------------- #
class TestFromNameExtraction:
    def test_via_upwork_suffix_stripped(self) -> None:
        raw = _build_raw_email(
            from_header='"Jesus G. via Upwork" <noreply@upwork.com>',
            subject="msg",
            body="Hi\n",
        )
        result = parser.parse_email(raw)
        assert result is not None
        assert result.from_name == "Jesus G."

    def test_no_quotes_no_via(self) -> None:
        raw = _build_raw_email(
            from_header="John Doe <john@upwork.com>",
            subject="msg",
            body="Hi\n",
        )
        result = parser.parse_email(raw)
        assert result is not None
        assert result.from_name == "John Doe"


# --------------------------------------------------------------------------- #
# Reply-To and Message-ID extraction
# --------------------------------------------------------------------------- #
class TestHeadersExtraction:
    def test_reply_to_token_preserved(self) -> None:
        raw = _build_raw_email(
            from_header='"X via Upwork" <noreply@upwork.com>',
            subject="msg",
            body="Hi\n",
            reply_to="6187135414865423809207175010615ver2@mg.upwork.com",
        )
        result = parser.parse_email(raw)
        assert result is not None
        assert result.reply_to == "6187135414865423809207175010615ver2@mg.upwork.com"

    def test_message_id_preserved(self) -> None:
        raw = _build_raw_email(
            from_header='"X via Upwork" <noreply@upwork.com>',
            subject="msg",
            body="Hi\n",
            message_id="<abc-123@upwork.com>",
        )
        result = parser.parse_email(raw)
        assert result is not None
        assert result.message_id == "<abc-123@upwork.com>"

    def test_in_reply_to_preserved(self) -> None:
        raw = _build_raw_email(
            from_header='"X via Upwork" <noreply@upwork.com>',
            subject="msg",
            body="Hi\n",
            in_reply_to="<previous-msg@upwork.com>",
        )
        result = parser.parse_email(raw)
        assert result is not None
        assert result.in_reply_to == "<previous-msg@upwork.com>"


# --------------------------------------------------------------------------- #
# Job title extraction
# --------------------------------------------------------------------------- #
class TestJobTitleExtraction:
    def test_extracts_title_from_unread_message_header(self) -> None:
        body = (
            "Unread message from Jesus G. about Tool Development for Bulk Image "
            "Upload and Color Management\n\n"
            "Jesus G.\n8:43 PM UTC, 6 May 2026\n\n"
            "Yea naw i can't do 3k\n"
        )
        raw = _build_raw_email(
            from_header='"Jesus G. via Upwork" <noreply@upwork.com>',
            subject="Jesus G. sent you a message",
            body=body,
        )
        result = parser.parse_email(raw)
        assert result is not None
        assert result.job_title is not None
        assert "Tool Development for Bulk Image Upload" in result.job_title

    def test_title_empty_for_no_marker(self) -> None:
        raw = _build_raw_email(
            from_header='"X via Upwork" <noreply@upwork.com>',
            subject="something",
            body="Just a body without the marker.",
        )
        result = parser.parse_email(raw)
        assert result is not None
        assert result.job_title is None


# --------------------------------------------------------------------------- #
# Body cleaning
# --------------------------------------------------------------------------- #
class TestBodyCleaning:
    def test_extracts_message_after_timestamp(self) -> None:
        """Тело сообщения = что между timestamp и 'View on Upwork'."""
        body = (
            "Unread message from Jesus G. about Tool Development for Bulk Image\n\n"
            "Jesus G.\n8:43 PM UTC, 6 May 2026\n\n"
            "Yea naw i can't do 3k i wasn't trying to do more then 5-700\\$\n\n"
            "View on Upwork: https://www.upwork.com/messages/...\n\n"
            "Or, you can respond by replying to this email."
        )
        raw = _build_raw_email(
            from_header='"Jesus G. via Upwork" <noreply@upwork.com>',
            subject="Jesus G. sent you a message",
            body=body,
        )
        result = parser.parse_email(raw)
        assert result is not None
        assert "Yea naw i can't do 3k" in result.body_text
        # Footer и header чистятся
        assert "View on Upwork" not in result.body_text
        assert "Or, you can respond" not in result.body_text
        assert "Unread message from" not in result.body_text

    def test_quoted_lines_removed(self) -> None:
        body = (
            "Unread message from John about Stripe webhook fix\n\n"
            "John\n10:00 AM UTC, 1 January 2026\n\n"
            "Thanks for your reply.\n"
            "> Yes, the cron trigger fires duplicate events\n"
            "> we should look at idempotency keys\n\n"
            "Are you available next week?\n\n"
            "View on Upwork"
        )
        raw = _build_raw_email(
            from_header='"John via Upwork" <noreply@upwork.com>',
            subject="msg",
            body=body,
        )
        result = parser.parse_email(raw)
        assert result is not None
        assert "Thanks for your reply" in result.body_text
        assert "Are you available next week" in result.body_text
        assert "Yes, the cron trigger" not in result.body_text


# --------------------------------------------------------------------------- #
# Message classification
# --------------------------------------------------------------------------- #
class TestMessageClassification:
    def test_client_message_subject(self) -> None:
        raw = _build_raw_email(
            from_header='"X via Upwork" <noreply@upwork.com>',
            subject="John D. sent you a message",
            body="Unread message from John D. about Test\n\nHi",
        )
        result = parser.parse_email(raw)
        assert result is not None
        assert result.message_type == parser.MESSAGE_TYPE_CLIENT_MESSAGE

    def test_jobs_digest_subject(self) -> None:
        raw = _build_raw_email(
            from_header="<digest@upwork.com>",
            subject="Your weekly summary of saved searches",
            body="Some digest content",
        )
        result = parser.parse_email(raw)
        assert result is not None
        assert result.message_type == parser.MESSAGE_TYPE_JOBS_DIGEST

    def test_hire_notification(self) -> None:
        raw = _build_raw_email(
            from_header='"Sarah via Upwork" <noreply@upwork.com>',
            subject="You've been hired by Sarah for the FastAPI audit",
            body="Congratulations!",
        )
        result = parser.parse_email(raw)
        assert result is not None
        assert result.message_type == parser.MESSAGE_TYPE_HIRE


# --------------------------------------------------------------------------- #
# HTML fallback
# --------------------------------------------------------------------------- #
class TestHtmlFallback:
    def test_html_only_email_decoded(self) -> None:
        """Когда text/plain отсутствует — fallback на text/html→strip_tags."""
        # Полный multipart RFC 822 с правильным Content-Type на outer message
        raw = (
            b'From: "John via Upwork" <noreply@upwork.com>\r\n'
            b"Subject: msg\r\n"
            b"Message-ID: <html@upwork.com>\r\n"
            b"MIME-Version: 1.0\r\n"
            b'Content-Type: multipart/alternative; boundary="bd"\r\n'
            b"\r\n"
            b"--bd\r\n"
            b'Content-Type: text/html; charset="utf-8"\r\n'
            b"\r\n"
            b"<html><body>"
            b"<p>Unread message from John about Test Job</p>"
            b"<p>John<br>10:00 AM UTC, 1 January 2026</p>"
            b"<p>This is the actual message text</p>"
            b"<p>View on Upwork</p>"
            b"</body></html>\r\n"
            b"--bd--\r\n"
        )
        result = parser.parse_email(raw)
        assert result is not None
        # Тело должно содержать текст из <p>
        assert "actual message text" in result.body_text


# --------------------------------------------------------------------------- #
# Attachments
# --------------------------------------------------------------------------- #
class TestAttachments:
    def test_no_attachment_default(self) -> None:
        raw = _build_raw_email(
            from_header='"X via Upwork" <noreply@upwork.com>',
            subject="msg",
            body="Hi\n",
        )
        result = parser.parse_email(raw)
        assert result is not None
        assert result.has_attachment is False
