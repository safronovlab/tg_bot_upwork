# DATABASE.md — Схема БД и миграции

Полный DDL — в [`schema.sql`](schema.sql). Применяется через [`src/migrations.py`](src/migrations.py) на старте, если БД пустая. Изменения после baseline — через [`migrations/`](migrations/) (правила и runner — §9).

8 таблиц вместо 14 (4 рабочих + 5 m2m + 5 системных в исходной n8n-схеме). Никаких NocoDB-полей.

| # | Таблица | Назначение |
|---|---|---|
| 1 | `upwork_jobs` | главная — вакансии и состояние pipeline (см. §1) |
| 2 | `bot_settings` | singleton, конфиг бота (см. §2) |
| 3 | `ai_prompts` | 4 слота промтов (см. §3) |
| 4 | `prompts_history` | откат правок промтов (см. §4) |
| 5 | `webhook_inbox` | идемпотентность POST /upwork-lead (см. §5) |
| 6 | `secrets` | API-ключ OpenRouter (см. §6) |
| 7 | `normalize_failures` | сырые битые payload'ы (см. §7) |
| 8 | `bot_events` | события для UI «Логи» (см. §8) |

---

## 1. `upwork_jobs` — основная таблица

```sql
CREATE TYPE proc_state AS ENUM
  ('pending','pre_screened','analyzed','delivered','filtered','failed');

CREATE TABLE upwork_jobs (
  id                   bigserial PRIMARY KEY,
  upwork_job_id        text        NOT NULL UNIQUE,
  created_at           timestamptz NOT NULL DEFAULT now(),
  updated_at           timestamptz NOT NULL DEFAULT now(),

  -- payload от скрейпера
  job_title            text,
  job_description      text,
  upwork_url           text,
  published_date       timestamptz,
  questions            text,
  job_type             text,
  budget_type          text,
  budget               text,
  client_country       text,
  client_rank          text,
  client_total_spent   numeric,
  client_total_hires   bigint,
  client_avg_rate      numeric,
  client_rating        numeric,
  client_registered_at date,
  client_reviews       text,

  -- результаты pipeline
  pre_rating           smallint,           -- 0..10
  ai_analysis          text,
  rating               smallint,           -- парсится из ai_analysis один раз

  -- состояние
  processing_state     proc_state  NOT NULL DEFAULT 'pending',
  attempts             smallint    NOT NULL DEFAULT 0,
  last_error           varchar(200),
  is_favorite          boolean     NOT NULL DEFAULT false,
  is_sent              boolean     NOT NULL DEFAULT false,

  -- queue routing: 'manual' для Отчёта, 'menu' для Синхронизации, NULL = ушло в TG сразу
  queued_reason        text        CHECK (queued_reason IN ('manual','menu'))
);

-- триггер для updated_at
CREATE OR REPLACE FUNCTION touch_updated_at() RETURNS trigger AS $$
BEGIN NEW.updated_at = now(); RETURN NEW; END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER trg_upwork_jobs_updated_at BEFORE UPDATE ON upwork_jobs
  FOR EACH ROW EXECUTE FUNCTION touch_updated_at();

-- partial индексы под реальные запросы
CREATE INDEX upwork_jobs_queue_idx
  ON upwork_jobs (created_at)
  WHERE is_sent = false AND ai_analysis IS NOT NULL;

CREATE INDEX upwork_jobs_favorites_idx
  ON upwork_jobs (published_date)
  WHERE is_favorite = true;
```

---

## 2. `bot_settings` — singleton

```sql
CREATE TABLE bot_settings (
  id                       smallint PRIMARY KEY DEFAULT 1
    CHECK (id = 1),                        -- ровно одна строка

  -- паузы (см. src/bot/BOT.md §2 "Авто-пауза")
  is_paused                boolean  NOT NULL DEFAULT false,   -- ручная: Запустить/Остановить
  is_paused_menu           boolean  NOT NULL DEFAULT false,   -- авто: пока юзер в меню

  -- пороги LLM-фильтрации (0 = фильтр выключен, всё проходит дальше)
  pre_screen_threshold     smallint NOT NULL DEFAULT 0
    CHECK (pre_screen_threshold BETWEEN 0 AND 10),
  analysis_threshold       smallint NOT NULL DEFAULT 0
    CHECK (analysis_threshold  BETWEEN 0 AND 10),

  -- жёсткие правиловые фильтры ДО любого LLM (см. src/PIPELINE.md §5 "Hard filters").
  -- 0 = фильтр выключен. Оператор настраивает вручную через меню «Пороги».
  hard_min_client_spent    numeric  NOT NULL DEFAULT 0,
  hard_min_client_rating   numeric  NOT NULL DEFAULT 0
                           CHECK (hard_min_client_rating BETWEEN 0 AND 5),
  hard_min_hires_for_rating smallint NOT NULL DEFAULT 3,
  hard_min_budget_hourly   numeric  NOT NULL DEFAULT 0,
  hard_min_budget_fixed    numeric  NOT NULL DEFAULT 0,
  hard_reject_no_hires     boolean  NOT NULL DEFAULT false,
  hard_max_vacancy_age_h   smallint NOT NULL DEFAULT 0,

  -- модели OpenRouter (4 штуки, редактируются из бота)
  prescreen_model          text     NOT NULL DEFAULT 'xiaomi/mimo-v2-flash',
  analysis_model           text     NOT NULL DEFAULT 'deepseek/deepseek-r1-0528',
  prescreen_fallback_model text     NOT NULL DEFAULT 'deepseek/deepseek-v4-flash',
  analysis_fallback_model  text     NOT NULL DEFAULT 'minimax/minimax-m2.5',

  -- уведомления: rating >= порога → громкое (со звуком), иначе беззвучное
  loud_notification_threshold smallint NOT NULL DEFAULT 8
                              CHECK (loud_notification_threshold BETWEEN 0 AND 10),

  updated_at               timestamptz NOT NULL DEFAULT now()
);

INSERT INTO bot_settings (id) VALUES (1);
```

**Условие доставки в TG:** `NOT is_paused AND NOT is_paused_menu`. Иначе вакансия копится в очереди с `queued_reason`.

---

## 3. `ai_prompts` — промты по слотам

```sql
CREATE TYPE prompt_slot AS ENUM ('pre_screen','analysis','cover','night_report');

CREATE TABLE ai_prompts (
  slot       prompt_slot PRIMARY KEY,
  content    text        NOT NULL,
  updated_at timestamptz NOT NULL DEFAULT now()
);
```

Ровно 4 строки навсегда. Bootstrap-значения вставляются на старте сервиса из env или дефолтов. Промт `pre_screen` обновляется под обогащённый payload (14 полей вместо 9 — см. [src/LLM.md](src/LLM.md) §2).

---

## 4. `prompts_history` — откат правок

```sql
CREATE TABLE prompts_history (
  id               bigserial PRIMARY KEY,
  slot             prompt_slot NOT NULL,
  content_before   text NOT NULL,
  edited_by        bigint,                      -- telegram user id
  edited_at        timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX prompts_history_slot_idx ON prompts_history (slot, edited_at DESC);
```

Перед `UPDATE ai_prompts SET content` — `INSERT INTO prompts_history`.

---

## 5. `webhook_inbox` — идемпотентность

```sql
CREATE TABLE webhook_inbox (
  request_id    bytea       PRIMARY KEY,        -- sha256 raw 32 bytes
  received_at   timestamptz NOT NULL DEFAULT now(),
  processed_at  timestamptz
);
```

`bytea(32)` (sha256 raw) — в 2× меньше места чем hex-строка, индекс компактнее.

---

## 6. `secrets` — секреты

```sql
CREATE TABLE secrets (
  name        text         PRIMARY KEY,         -- сейчас один: 'openrouter_api_key'
  value       text         NOT NULL,
  updated_at  timestamptz  NOT NULL DEFAULT now(),
  updated_by  bigint
);
```

---

## 7. `normalize_failures` — битые payload'ы

```sql
CREATE TABLE normalize_failures (
  id          bigserial   PRIMARY KEY,
  request_id  bytea       NOT NULL REFERENCES webhook_inbox(request_id) ON DELETE CASCADE,
  raw_payload jsonb       NOT NULL,
  error       varchar(500) NOT NULL,
  ts          timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX normalize_failures_ts_idx ON normalize_failures (ts DESC);
```

Хранит сырые JSON, которые скрейпер прислал в неожиданном формате. FK CASCADE — чистится автоматически вместе с inbox.

---

## 8. `bot_events` — события для UI «Логи»

```sql
CREATE TABLE bot_events (
  id        serial      PRIMARY KEY,            -- ~25K строк за 7 дней — int4 хватит
  ts        timestamptz NOT NULL DEFAULT now(),
  level     smallint    NOT NULL,               -- 0=info, 1=warn, 2=error
  event     text        NOT NULL,
  data      jsonb
);

CREATE INDEX bot_events_ts_idx ON bot_events (ts DESC);
```

Что туда пишется (полный список — [ARCHITECTURE.md §6](ARCHITECTURE.md#6-логирование)):
- `job_received` — вакансия сохранена в БД (до LLM)
- `pipeline_finished` — итог обработки одной вакансии
- `batch_finished` — итог по всему POST'у
- `normalize_failed` — битый payload отложен в normalize_failures
- `recovery_triggered` — recovery подобрал зависшие
- `llm_failed` / `llm_fallback`
- `key_updated` / `model_updated` / `prompt_updated` / `threshold_updated` / `preset_applied` (без значений секретов)
- `db_truncated` / `pipeline_failed`

---

## 9. Миграции БД

Самописный мини-runner вместо Alembic. Один [`schema.sql`](schema.sql) для свежей установки + нумерованные `migrations/NNN_name.sql` для последующих изменений. Состояние трекается в `schema_version`.

Реализация runner'а — в [`src/migrations.py`](src/migrations.py).

### 9.1 Структура

```
schema.sql                        # вся актуальная схема одним файлом (для свежей БД)
migrations/
  001_add_outcome_field.sql       # будущие изменения по числу в имени
  002_add_llm_cache.sql
```

### 9.2 Таблица версий

```sql
CREATE TABLE IF NOT EXISTS schema_version (
  version    integer     PRIMARY KEY,
  name       text        NOT NULL,
  applied_at timestamptz NOT NULL DEFAULT now()
);
```

### 9.3 Runner (`src/migrations.py`)

```python
import asyncpg
from pathlib import Path
from src import log

ROOT          = Path(__file__).parent.parent
SCHEMA_PATH   = ROOT / "schema.sql"
MIGRATIONS    = ROOT / "migrations"

async def init_schema(pool: asyncpg.Pool) -> None:
    """
    На каждом старте:
      1. Создать schema_version если её нет
      2. Если БД пустая (нет upwork_jobs) — применить schema.sql, отметить v0
      3. Применить все pending миграции в порядке возрастания номера
    Идемпотентно — безопасно перезапускать.
    """
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS schema_version (
                version integer PRIMARY KEY,
                name text NOT NULL,
                applied_at timestamptz NOT NULL DEFAULT now()
            );
        """)

        bootstrap_done = await conn.fetchval("""
            SELECT EXISTS (
                SELECT 1 FROM information_schema.tables
                WHERE table_name = 'upwork_jobs'
            )
        """)
        if not bootstrap_done:
            await log.emit("schema_bootstrap_started")
            async with conn.transaction():
                await conn.execute(SCHEMA_PATH.read_text())
                await conn.execute(
                    "INSERT INTO schema_version (version, name) VALUES (0, 'baseline')"
                )
            await log.emit("schema_bootstrap_done")

        applied = {
            r["version"] for r in await conn.fetch("SELECT version FROM schema_version")
        }
        pending = []
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
                    version, f.stem,
                )
            await log.emit("migration_applied", version=version, name=f.stem)
```

### 9.4 Правила написания миграций

- **Имя файла:** `NNN_short_description.sql`, NNN — целое число с ведущими нулями (001, 002, 010, 100)
- **Каждая миграция атомарная** — runner оборачивает в транзакцию
- **Только additive по умолчанию** (ADD COLUMN, CREATE INDEX, INSERT)
- **Никаких `BEGIN`/`COMMIT` внутри файла** — runner оборачивает сам
- **Идемпотентность желательна** — `CREATE TABLE IF NOT EXISTS`, `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`

### 9.5 Пример миграции

`migrations/001_add_outcome_field.sql`:
```sql
ALTER TABLE upwork_jobs
  ADD COLUMN IF NOT EXISTS outcome text
  CHECK (outcome IN ('applied','invited','hired','rejected','ghosted')),
  ADD COLUMN IF NOT EXISTS outcome_at timestamptz;
```

### 9.6 Rollback

Откат миграций **не реализуется** — это single-user single-environment проект, проще:
- Восстановить из `pg_dump` (делать перед рискованными миграциями вручную)
- Или написать обратную миграцию `XXX_revert_*.sql`
