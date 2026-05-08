"""Парсинг raw email-bytes от Upwork → ParsedEmail. См. CHAT.md §5.

Формат писем Upwork (по реальному sample, May 2026):

    From: "Jesus G. via Upwork" <email@upwork.com>
    Reply-To: 6187135414865423809207175010615ver2@mg.upwork.com
    Subject: Jesus G. sent you a message
    Message-ID: <random@upwork.com>

    Body (text/plain):
        Unread message from Jesus G. about Tool Development for Bulk Image Upload...

        Jesus G.
        8:43 PM UTC, 6 May 2026

        Yea naw i can't do 3k i wasn't trying to do more then 5-700\\$

        View on Upwork: https://...

        Or, you can respond by replying to this email.

Алгоритм:
  1. email.message_from_bytes(raw)
  2. Whitelist отправителя по From-домену (только @upwork.com / @mg.upwork.com)
  3. Извлечь client_name из From "X via Upwork" → "X"
  4. Reply-To токен — для SMTP-ответа
  5. Body: предпочесть text/plain, иначе HTML→strip_tags
  6. Job title: regex `Unread message from <name> about <TITLE>` (на одной строке
     или с переносом)
  7. Тело сообщения: между timestamp-маркером и "View on Upwork" / "View this"
  8. Тип письма (client_message / hire / application_sent / digest) — по subject
"""

from __future__ import annotations

import email
import logging
import re
from dataclasses import dataclass
from email.message import Message
from html.parser import HTMLParser

# --------------------------------------------------------------------------- #
# Public types
# --------------------------------------------------------------------------- #
MESSAGE_TYPE_CLIENT_MESSAGE = "client_message"
MESSAGE_TYPE_HIRE = "hire_notification"
MESSAGE_TYPE_APPLICATION_SENT = "application_sent"
MESSAGE_TYPE_JOBS_DIGEST = "jobs_digest"
MESSAGE_TYPE_UNKNOWN = "unknown"


@dataclass(slots=True, frozen=True)
class ParsedEmail:
    """Результат парсинга raw email-bytes от Upwork.

    Если `body_text` пустая — карточка в TG показывает «открой Upwork» fallback.
    Если `message_type != client_message` — pipeline должен скипнуть (digest и
    подобные нерелевантны для chat).
    """

    message_type: str
    from_name: str
    from_email: str
    reply_to: str | None
    message_id: str | None
    in_reply_to: str | None
    subject: str
    body_text: str
    has_attachment: bool
    job_title: str | None
    job_url: str | None
    raw_email_uid: str | None


# --------------------------------------------------------------------------- #
# Whitelist Upwork-доменов
# --------------------------------------------------------------------------- #
_UPWORK_DOMAINS: frozenset[str] = frozenset({
    "upwork.com",
    "mg.upwork.com",
    "email.upwork.com",
    "notifications.upwork.com",
})


def _domain_from_email(addr: str) -> str:
    """Возвращает domain из email-address (lowercased). Пустую строку для невалидного."""
    if not addr or "@" not in addr:
        return ""
    return addr.rsplit("@", 1)[1].strip(">").lower()


def _is_upwork_sender(from_email: str) -> bool:
    domain = _domain_from_email(from_email)
    return any(domain == d or domain.endswith("." + d) for d in _UPWORK_DOMAINS)


# --------------------------------------------------------------------------- #
# Извлечение headers
# --------------------------------------------------------------------------- #
_FROM_NAME_RE = re.compile(r'^"?([^"<]+?)(?:\s+via\s+Upwork)?"?\s*<')

# Адрес внутри `<...>` или сам адрес если угловых скобок нет
_FROM_EMAIL_RE = re.compile(r"<([^>]+)>|([^\s<>]+@[^\s<>]+)")


def _extract_from_name(from_header: str) -> str:
    """Из 'Jesus G. via Upwork <email@upwork.com>' → 'Jesus G.'."""
    m = _FROM_NAME_RE.match(from_header)
    if m:
        return m.group(1).strip()
    # Fallback: если заголовок в формате только email — берём local-part
    em = _FROM_EMAIL_RE.search(from_header)
    if em:
        addr = em.group(1) or em.group(2) or ""
        return addr.split("@", 1)[0]
    return from_header.strip().strip('"')


def _extract_from_email(from_header: str) -> str:
    m = _FROM_EMAIL_RE.search(from_header)
    if m:
        return (m.group(1) or m.group(2) or "").strip()
    return ""


# --------------------------------------------------------------------------- #
# HTML stripper (без bleach — стандартная библиотека)
# --------------------------------------------------------------------------- #
class _HTMLStripper(HTMLParser):
    """Минималистичный HTML→text для писем Upwork.

    Преобразует HTML в plain-text сохраняя \\n границы между блочными тегами
    (p, br, div, tr) — иначе всё слипается в одну строку и regex-маркеры
    («View on Upwork») перестают работать.
    """

    _BLOCK_TAGS: frozenset[str] = frozenset(
        {"p", "div", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6", "br"}
    )
    _SKIP_TAGS: frozenset[str] = frozenset({"script", "style", "head", "title"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        if tag in self._SKIP_TAGS:
            self._skip_depth += 1
        elif tag in self._BLOCK_TAGS:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1
        elif tag in self._BLOCK_TAGS:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0:
            self._parts.append(data)

    def get_text(self) -> str:
        joined = "".join(self._parts)
        # Нормализация: убрать множественные переносы и trailing whitespace
        return re.sub(r"\n{3,}", "\n\n", joined).strip()


def _html_to_text(html: str) -> str:
    parser = _HTMLStripper()
    try:
        parser.feed(html)
    except Exception:
        # Bullet-proof: возвращаем сырой HTML если парсер споткнулся (лучше
        # показать оператору то что есть чем потерять сообщение).
        logging.getLogger("bot").warning(
            "html_parser_failed", extra={"data": {"len": len(html)}}
        )
        return html
    return parser.get_text()


# --------------------------------------------------------------------------- #
# Извлечение body (предпочтительно text/plain)
# --------------------------------------------------------------------------- #
def _decode_part_payload(part: Message) -> str:
    """Декодировать одну MIME-часть в str (utf-8 with replace)."""
    payload = part.get_payload(decode=True)
    if payload is None:
        return ""
    if isinstance(payload, bytes):
        charset = part.get_content_charset() or "utf-8"
        try:
            return payload.decode(charset, errors="replace")
        except (LookupError, UnicodeDecodeError):
            return payload.decode("utf-8", errors="replace")
    if isinstance(payload, str):
        return payload
    # Multipart payload — список Message; нерелевантно здесь
    return ""


def _extract_body(msg: Message) -> tuple[str, bool]:
    """Возвращает (body_text, has_attachment).

    Predilection: text/plain → text/html → пустая строка.
    has_attachment: True если есть Content-Disposition: attachment.
    """
    plain_parts: list[str] = []
    html_parts: list[str] = []
    has_attachment = False

    for part in msg.walk():
        if part.is_multipart():
            continue
        ctype = (part.get_content_type() or "").lower()
        cdisp = (part.get("Content-Disposition") or "").lower()

        if "attachment" in cdisp:
            has_attachment = True
            continue

        if ctype == "text/plain":
            plain_parts.append(_decode_part_payload(part))
        elif ctype == "text/html":
            html_parts.append(_decode_part_payload(part))

    if plain_parts:
        body = "\n\n".join(p for p in plain_parts if p).strip()
    elif html_parts:
        body = _html_to_text("\n\n".join(p for p in html_parts if p))
    else:
        body = ""

    return body, has_attachment


# --------------------------------------------------------------------------- #
# Очистка тела от Upwork-template
# --------------------------------------------------------------------------- #
# Маркеры конца сообщения — всё ниже отрезаем
_END_MARKERS_RE = re.compile(
    r"(View on Upwork|View this message on Upwork|"
    r"Or, you can respond by replying|Best,\s*The Upwork Team|"
    r"This message was sent by|©\s*\d{4}\s*Upwork|"
    r"Unsubscribe|"
    r"Manage email preferences)",
    re.IGNORECASE,
)

# Маркер начала сообщения клиента: после строки timestamp вида
# "8:43 PM UTC, 6 May 2026" или ISO. Разбиваем по этой границе.
_TIMESTAMP_LINE_RE = re.compile(
    r"^\s*\d{1,2}:\d{2}\s*(?:AM|PM)?\s*UTC,?\s*\d{1,2}\s+\w+\s+\d{4}\s*$",
    re.IGNORECASE | re.MULTILINE,
)

# Header-маркер: "Unread message from <name> about <TITLE>"
# Захватываем title (может быть на нескольких строках до пустой строки).
_HEADER_TITLE_RE = re.compile(
    r"Unread message from\s+(?P<name>[^.]+?)\.?\s+about\s+(?P<title>.+?)(?:\n\s*\n|$)",
    re.IGNORECASE | re.DOTALL,
)

# Альтернативный header (некоторые письма): "<name> sent you a message about <TITLE>"
_HEADER_ALT_RE = re.compile(
    r"(?P<name>[A-Za-z][^.\n]*?)\s+sent you a message\s+about\s+(?P<title>.+?)(?:\n\s*\n|$)",
    re.IGNORECASE | re.DOTALL,
)

# Ссылка на conversation в Upwork (https://www.upwork.com/messages/...).
_UPWORK_URL_RE = re.compile(
    r"https?://(?:www\.)?upwork\.com/(?:messages|nx/messages|fl/inbox)[^\s<>\"]*",
    re.IGNORECASE,
)

# Цитированные строки (наши предыдущие сообщения)
_QUOTED_LINE_RE = re.compile(r"^\s*>.*$", re.MULTILINE)
_MULTI_NEWLINE_RE = re.compile(r"\n{3,}")


def _strip_upwork_chrome(body: str, sender_name: str | None) -> str:
    """Очистить body от Upwork header/footer/cite. Возвращает чистый текст
    клиента или пустую строку если ничего извлечь не удалось.
    """
    if not body:
        return ""

    # 1. Обрезаем от первого end-маркера (footer)
    end_m = _END_MARKERS_RE.search(body)
    if end_m:
        body = body[: end_m.start()]

    # 2. Если есть header «Unread message from X about TITLE» — отрезаем header
    #    (заголовок занимает первые ~2 строки до пустой строки)
    h = _HEADER_TITLE_RE.search(body)
    if h:
        body = body[h.end() :]

    # 3. Найти строку timestamp — настоящее сообщение начинается после неё
    ts_m = _TIMESTAMP_LINE_RE.search(body)
    if ts_m:
        body = body[ts_m.end() :]
    elif sender_name:
        # Fallback: если timestamp не нашли — отрезаем до строки с именем отправителя
        # (часто именно так структурировано в HTML-версии)
        idx = body.find(sender_name)
        if idx >= 0:
            after = body[idx + len(sender_name) :]
            # Пропускаем переносы строк
            after = after.lstrip("\n").lstrip()
            body = after

    # 4. Снять цитированные строки (>)
    body = _QUOTED_LINE_RE.sub("", body)

    # 5. Нормализация whitespace
    body = _MULTI_NEWLINE_RE.sub("\n\n", body)

    return body.strip()


def _extract_job_title(body: str, subject: str) -> str | None:
    """Job title: предпочтительно из header `Unread message... about TITLE`,
    иначе из subject если он содержит маркер.
    """
    m = _HEADER_TITLE_RE.search(body)
    if m:
        title = m.group("title")
    else:
        m = _HEADER_ALT_RE.search(body)
        if m:
            title = m.group("title")
        else:
            return None
    # Чистим: одна строка, max 200 chars
    return re.sub(r"\s+", " ", title).strip()[:200]


def _extract_job_url(body: str) -> str | None:
    m = _UPWORK_URL_RE.search(body)
    return m.group(0) if m else None


# --------------------------------------------------------------------------- #
# Классификация типа письма по Subject
# --------------------------------------------------------------------------- #
def _classify_message(subject: str, body: str) -> str:
    s = subject.lower()
    if "sent you a message" in s or "new message" in s or "unread message" in s:
        return MESSAGE_TYPE_CLIENT_MESSAGE
    if "hired you" in s or "you've been hired" in s or "you have been hired" in s:
        return MESSAGE_TYPE_HIRE
    if "application sent" in s or "your application to" in s:
        return MESSAGE_TYPE_APPLICATION_SENT
    if "saved search" in s or "weekly summary" in s or "jobs you might like" in s:
        return MESSAGE_TYPE_JOBS_DIGEST
    # Fallback: если в body есть header «Unread message» — вероятно client_message
    if _HEADER_TITLE_RE.search(body) or _HEADER_ALT_RE.search(body):
        return MESSAGE_TYPE_CLIENT_MESSAGE
    return MESSAGE_TYPE_UNKNOWN


# --------------------------------------------------------------------------- #
# Public entrypoint
# --------------------------------------------------------------------------- #
def parse_email(raw_bytes: bytes, raw_email_uid: str | None = None) -> ParsedEmail | None:
    """Парсинг raw RFC 822 email от Upwork.

    Returns None если sender не Upwork (не наш домен) или письмо нельзя
    распарсить даже как RFC 822. Иначе всегда возвращает ParsedEmail —
    даже если body_text пустой (тогда оператор пойдёт в Upwork увидеть).
    """
    try:
        msg = email.message_from_bytes(raw_bytes)
    except Exception:
        return None

    from_header = msg.get("From", "") or ""
    from_email_addr = _extract_from_email(from_header)

    # Whitelist по домену — игнорируем всё что не от Upwork (защита от phishing)
    if not _is_upwork_sender(from_email_addr):
        return None

    from_name = _extract_from_name(from_header)
    reply_to = msg.get("Reply-To")
    message_id = msg.get("Message-ID") or msg.get("Message-Id")
    in_reply_to = msg.get("In-Reply-To")
    subject = (msg.get("Subject") or "").strip()

    body_raw, has_attachment = _extract_body(msg)
    job_title = _extract_job_title(body_raw, subject)
    job_url = _extract_job_url(body_raw)
    body_clean = _strip_upwork_chrome(body_raw, from_name)
    message_type = _classify_message(subject, body_raw)

    return ParsedEmail(
        message_type=message_type,
        from_name=from_name,
        from_email=from_email_addr,
        reply_to=reply_to.strip() if reply_to else None,
        message_id=message_id.strip() if message_id else None,
        in_reply_to=in_reply_to.strip() if in_reply_to else None,
        subject=subject,
        body_text=body_clean,
        has_attachment=has_attachment,
        job_title=job_title,
        job_url=job_url,
        raw_email_uid=raw_email_uid,
    )
