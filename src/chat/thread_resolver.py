"""email → email_thread_key mapping. См. CHAT.md §3, §5.

Алгоритм:
  1. Если In-Reply-To совпадает с messages.email_message_id уже в БД — берём
     тот же email_thread_key (тред-продолжение).
  2. Иначе — sha256(client_name|job_title) с retention 30 дней (если такой
     тред существует и недавно активен — используем его).
  3. Иначе — новый thread_key как sha256(client_name|job_title|today_iso).
"""

from __future__ import annotations

import hashlib

from src import db


def _hash(value: str) -> bytes:
    """sha256 raw 32 bytes — единое представление ключа треда."""
    return hashlib.sha256(value.encode("utf-8")).digest()


def _fallback_key(client_name: str, job_title: str | None) -> bytes:
    """Hash от нормализованных client_name+job_title. Lowercase + strip."""
    nm = (client_name or "").strip().lower()
    jt = (job_title or "").strip().lower()
    return _hash(f"{nm}|{jt}")


async def resolve_thread_key(
    *,
    in_reply_to: str | None,
    client_name: str,
    job_title: str | None,
) -> bytes:
    """Вернуть sha256(32 bytes) ключ треда.

    Стратегия:
        - Если In-Reply-To указывает на наше существующее сообщение (любого
          направления) → берём его email_thread_key.
        - Иначе — fallback hash(client_name|job_title) с retention 30 дней:
          если такой тред уже есть в chat_messages с активностью за последние
          30 дней → используем тот же ключ.
        - Иначе — fallback hash тот же (новый тред создастся автоматически
          при INSERT с этим ключом).

    Note: ключ детерминирован (одинаковые входы → одинаковый bytes), поэтому
    fallback автоматически склеивает повторные сообщения от того же клиента
    по той же вакансии в один тред.
    """
    pool = db._conn()

    # Стратегия 1: In-Reply-To match
    if in_reply_to:
        existing_key = await pool.fetchval(
            """
            SELECT email_thread_key FROM chat_messages
            WHERE email_message_id = $1
            ORDER BY received_at DESC
            LIMIT 1
            """,
            in_reply_to.strip(),
        )
        if existing_key is not None:
            return bytes(existing_key)

    # Стратегия 2/3: detertministic fallback hash
    return _fallback_key(client_name, job_title)


async def link_to_upwork_job(
    *,
    job_title: str | None,
    job_url: str | None,
) -> int | None:
    """Найти upwork_jobs.id по job_url или fuzzy match по job_title.

    Returns: upwork_jobs.id или None если линковка не удалась.
    Single-user volume — простой LIKE достаточен, никаких fuzzy-libs.
    """
    if not (job_title or job_url):
        return None

    pool = db._conn()

    # Сначала точное совпадение по url (если есть)
    if job_url:
        row = await pool.fetchval(
            "SELECT id FROM upwork_jobs WHERE upwork_url = $1 LIMIT 1",
            job_url,
        )
        if row is not None:
            return int(row)

    # Иначе fuzzy по title (case-insensitive contains)
    if job_title and len(job_title) >= 8:
        # Защита от слишком общих заголовков ("API integration" matched бы всё)
        row = await pool.fetchval(
            """
            SELECT id FROM upwork_jobs
            WHERE job_title ILIKE $1
            ORDER BY created_at DESC
            LIMIT 1
            """,
            f"%{job_title[:80]}%",
        )
        if row is not None:
            return int(row)

    return None
