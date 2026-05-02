"""Runner для schema.sql + pending миграций + bootstrap дефолтов. См. ../DATABASE.md §9."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from src import log

ROOT = Path(__file__).parent.parent
SCHEMA_PATH = ROOT / "schema.sql"
MIGRATIONS = ROOT / "migrations"


# Дефолтные промты для bootstrap'а ai_prompts на первом старте (DATABASE.md §3).
# Минимальный «работоспособный» текст — оператор переопределит через бота позже.
DEFAULT_PROMPTS: dict[str, str] = {
    "pre_screen": (
        "Ты — фильтр Upwork-вакансий для опытного фрилансера-разработчика. "
        "Оцени вакансию числом 0-10 по релевантности и качеству клиента. "
        "Ответь ТОЛЬКО одним числом без пояснений."
    ),
    "analysis": (
        "Ты — аналитик Upwork-вакансий. Дай развёрнутый разбор: суть задачи, "
        "сильные и слабые стороны клиента, риски, ставка, оценка по 10-балльной шкале. "
        "В конце обязательно строка `РЕЙТИНГ: N` где N — целое 0-10."
    ),
    "cover": (
        "Напиши кратко (под 800 символов) персональное cover-letter под эту вакансию. "
        "Без воды, по делу, упоминая 1-2 конкретных пункта из описания."
    ),
}


async def init_schema(pool: Any) -> None:
    """На каждом старте:
    1. Создать schema_version если её нет
    2. Если БД пустая (нет upwork_jobs) — применить schema.sql, отметить v0
    3. Применить все pending миграции в порядке возрастания номера
    4. Bootstrap'нуть дефолты для ai_prompts если пусто (idempotent).
    """
    async with pool.acquire() as conn:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_version (
                version integer PRIMARY KEY,
                name text NOT NULL,
                applied_at timestamptz NOT NULL DEFAULT now()
            );
            """
        )

        bootstrap_done = await conn.fetchval(
            """
            SELECT EXISTS (
                SELECT 1 FROM information_schema.tables
                WHERE table_name = 'upwork_jobs'
            )
            """
        )
        if not bootstrap_done:
            await log.emit("schema_bootstrap_started")
            async with conn.transaction():
                schema_sql = SCHEMA_PATH.read_text()
                await conn.execute(schema_sql)
                await conn.execute(
                    "INSERT INTO schema_version (version, name) VALUES (0, 'baseline')"
                )
            await log.emit("schema_bootstrap_done")

        applied = {r["version"] for r in await conn.fetch("SELECT version FROM schema_version")}
        pending: list[tuple[int, Path]] = []
        if MIGRATIONS.exists():
            for f in sorted(MIGRATIONS.glob("*.sql")):
                try:
                    version = int(f.stem.split("_", 1)[0])
                except ValueError:
                    continue
                if version not in applied:
                    pending.append((version, f))

        for version, f in pending:
            await log.emit("migration_applying", version=version, name=f.stem)
            async with conn.transaction():
                await conn.execute(f.read_text())
                await conn.execute(
                    "INSERT INTO schema_version (version, name) VALUES ($1, $2)",
                    version,
                    f.stem,
                )
            await log.emit("migration_applied", version=version, name=f.stem)

    await _bootstrap_prompts(pool)


async def _bootstrap_prompts(pool: Any) -> None:
    """INSERT дефолтных промтов для слотов где строки нет (DATABASE.md §3).

    Идемпотентно — `ON CONFLICT DO NOTHING` гарантирует что пользовательские
    значения не перезатираются повторным стартом.
    """
    inserted_slots: list[str] = []
    for slot, content in DEFAULT_PROMPTS.items():
        result = await pool.fetchval(
            """
            INSERT INTO ai_prompts (slot, content) VALUES ($1, $2)
            ON CONFLICT (slot) DO NOTHING
            RETURNING slot
            """,
            slot,
            content,
        )
        if result is not None:
            inserted_slots.append(slot)
    if inserted_slots:
        await log.emit("prompts_bootstrap_done", slots=inserted_slots)
