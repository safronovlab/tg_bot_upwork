# PIPELINE.md — Pipeline обработки вакансий (ядро)

Описывает поток приёма и обработки вакансий: webhook handler ([http_app.py](http_app.py)), batch processor и `process_incoming_job` ([pipeline.py](pipeline.py)), модели payload ([models.py](models.py)), доставку в Telegram ([notifier.py](notifier.py)), фоновые `asyncio`-задачи recovery/cleanup ([cron.py](cron.py)).

Связанные документы:
- Схема БД: [../DATABASE.md](../DATABASE.md)
- LLM-вызовы: [LLM.md](LLM.md)
- Логирование: [../ARCHITECTURE.md §6](../ARCHITECTURE.md#6-логирование)
- Надёжность и инварианты: [../ARCHITECTURE.md §7](../ARCHITECTURE.md#7-надёжность-и-стабильность)

---

## 1. Поток данных

```
Скрейпер ──POST /upwork-lead──→ Webhook
                                   │
                                   ├── INSERT webhook_inbox (sync, до 200)
                                   │
                                   └── 200 accepted ─→ скрейпер свободен
                                              │
                                              ↓ (фон)
                                   _process_batch_async(payload)
                                              │
                                              ↓ для каждой вакансии
                                   process_incoming_job(job, settings)
                                              │
                                              ├── upsert_and_get_state → (inserted, current_state)
                                              │   если уже terminal — SKIPPED_DUPLICATE
                                              │
                                              ├── emit "job_received"
                                              │
                                              ├── hard_filter (правила) — если сработал → DELETE → FILTERED_HARD
                                              │
                                              ├── pre-screen LLM
                                              │   • если failed → mark_failed → LLM_FAILED
                                              │   • если pre < threshold → DELETE → FILTERED_PRE
                                              │   • иначе UPDATE state='pre_screened'
                                              │
                                              ├── analysis LLM
                                              │   • если failed → bump_attempts → LLM_FAILED
                                              │
                                              ├── parse rating from analysis
                                              │   • если rating < threshold → DELETE → FILTERED_ANALYSIS
                                              │
                                              ├── проверка пауз
                                              │   • is_paused → state='analyzed', queued_reason='manual' → QUEUED_PAUSED
                                              │   • is_paused_menu → state='analyzed', queued_reason='menu' → QUEUED_PAUSED
                                              │
                                              ├── send to Telegram (silent или loud по rating)
                                              ├── mark_sent → DELIVERED
                                              │
                                              └── emit "pipeline_finished"
```

---

## 2. Webhook handler

```python
# src/http_app.py
import hashlib, msgspec, asyncio, logging
from aiohttp import web
from src import db, log
from src.models import WebhookBody
from src.pipeline import _process_batch_async

async def upwork_lead(request: web.Request) -> web.Response:
    body_bytes = await request.read()
    request_id = (
        request.headers.get("Idempotency-Key", "").encode()
        or hashlib.sha256(body_bytes).digest()           # 32 bytes raw
    )

    # КРИТИЧНО: синхронно регистрируем запрос ДО ответа 200.
    # При ошибке БД отвечаем 5xx → скрейпер ретраит → вакансия не теряется.
    try:
        inserted = await db.try_register_request(request_id)
    except Exception:
        log.exception("inbox_insert_failed")
        return web.json_response({"status": "error"}, status=503)

    if not inserted:
        return web.json_response({"status": "duplicate"})

    # Парсим payload только для нового запроса. Битый JSON — отдельная страховка.
    try:
        payload = msgspec.json.decode(body_bytes, type=WebhookBody)
    except msgspec.ValidationError as e:
        await db.save_normalize_failure(request_id, body_bytes, str(e))
        await log.emit("normalize_failed", level=logging.ERROR,
                       request_id=request_id.hex(), error=str(e)[:200])
        return web.json_response({"status": "accepted_unparseable"})

    asyncio.create_task(_process_batch_async(payload, request_id))
    return web.json_response({"status": "accepted"})
```

Ответ скрейперу:
- `< 5 ms` на дубле
- `< 20 ms` на новом запросе с валидным JSON
- `5xx` если БД недоступна — скрейпер ретраит
- `accepted_unparseable` если JSON битый — payload в БД для разбора

---

## 3. Batch processor

```python
# src/pipeline.py
import time, asyncio, collections, logging
from src import db, llm, log
from src.models import WebhookBody, BotSettings

PIPELINE_BACKGROUND_TIMEOUT = 120

async def _process_batch_async(payload: WebhookBody, request_id: bytes):
    started_at = time.monotonic()
    settings = await db.get_settings_cached()           # один read на батч
    n = len(payload.body.projects)

    tasks = [
        asyncio.create_task(
            asyncio.wait_for(
                safe_process_one(project, settings, request_id),
                timeout=PIPELINE_BACKGROUND_TIMEOUT,
            )
        )
        for project in payload.body.projects
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    counts = collections.Counter(
        r.value if isinstance(r, PipelineResult) else "exception" for r in results
    )
    await db.mark_request_processed(request_id)
    await log.emit("batch_finished",
                   request_id=request_id.hex(), n=n,
                   duration_ms=int((time.monotonic() - started_at) * 1000),
                   **dict(counts))

async def safe_process_one(raw_project, settings, request_id):
    try:
        job = normalize_payload(raw_project)
    except Exception as e:
        await db.save_normalize_failure(request_id, raw_project, str(e))
        await log.emit("normalize_failed", level=logging.ERROR,
                       request_id=request_id.hex(), error=str(e)[:200])
        return PipelineResult.LLM_FAILED
    return await process_incoming_job(job, settings)
```

**Важно:**
- Один `get_settings_cached()` на batch (не на каждый job)
- `asyncio.wait_for(timeout=120)` на каждую — одна зависшая не блокирует остальные
- `return_exceptions=True` в gather — исключение в одной не уносит batch
- Семафор LLM внутри `llm.*` зажимает реальный concurrency

---

## 4. process_incoming_job — обработка одной вакансии

```python
# src/pipeline.py
from enum import StrEnum

class PipelineResult(StrEnum):
    DELIVERED         = "delivered"
    QUEUED_PAUSED     = "queued_paused"
    FILTERED_HARD     = "filtered_hard"          # rule-based, ДО LLM
    FILTERED_PRE      = "filtered_pre"
    FILTERED_ANALYSIS = "filtered_analysis"
    SKIPPED_DUPLICATE = "skipped_duplicate"
    LLM_FAILED        = "llm_failed"

TERMINAL_STATES = {'filtered', 'delivered', 'analyzed', 'failed'}

async def process_incoming_job(job, settings: BotSettings) -> PipelineResult:
    inserted, current_state = await db.upsert_and_get_state(job)
    if not inserted and current_state in TERMINAL_STATES:
        return PipelineResult.SKIPPED_DUPLICATE

    await log.emit("job_received",
                   upwork_job_id=job.upwork_job_id,
                   job_title=job.job_title[:80],
                   client_country=job.client_country)

    # Hard filters — правиловый отсев очевидного мусора ДО LLM (см. §5)
    reason = hard_filter(job, settings)
    if reason:
        await log.emit("pipeline_finished",
                       upwork_job_id=job.upwork_job_id, result="filtered_hard",
                       reason=reason, job_title=job.job_title[:80])
        await db.delete_job(job.upwork_job_id)
        return PipelineResult.FILTERED_HARD

    # Pre-screen LLM
    pre = await llm.pre_screen(job)
    if pre is None:
        await db.mark_failed(job.upwork_job_id, "pre_screen_no_response")
        return PipelineResult.LLM_FAILED

    # Pre-screen filter — DELETE целиком: дешёвый отсев, хранить незачем
    if pre < settings.pre_screen_threshold:
        await log.emit("pipeline_finished",
                       upwork_job_id=job.upwork_job_id, result="filtered_pre",
                       pre_rating=pre, job_title=job.job_title[:80])
        await db.delete_job(job.upwork_job_id)
        return PipelineResult.FILTERED_PRE

    await db.set_pre_rating_and_state(job.upwork_job_id, pre, "pre_screened")

    # Analysis LLM
    analysis = await llm.analyze(job)
    if not analysis or len(analysis) < 50:
        await db.bump_attempts(job.upwork_job_id, "analysis_short_or_empty")
        return PipelineResult.LLM_FAILED

    rating = parse_rating(analysis)

    if rating < settings.analysis_threshold:
        # DELETE целиком — symmetrically с pre-screen filter
        await log.emit("pipeline_finished",
                       upwork_job_id=job.upwork_job_id, result="filtered_analysis",
                       rating=rating, job_title=job.job_title[:80])
        await db.delete_job(job.upwork_job_id)
        return PipelineResult.FILTERED_ANALYSIS

    # Любая из двух пауз → копим в очередь.
    # Ручная пауза имеет приоритет (даже если оператор открыл меню под паузой).
    if settings.is_paused:
        await db.set_analysis_state_queued(job.upwork_job_id, analysis, rating, "manual")
        await log.emit("pipeline_finished",
                       upwork_job_id=job.upwork_job_id, result="queued_manual",
                       rating=rating)
        return PipelineResult.QUEUED_PAUSED

    if settings.is_paused_menu:
        await db.set_analysis_state_queued(job.upwork_job_id, analysis, rating, "menu")
        await log.emit("pipeline_finished",
                       upwork_job_id=job.upwork_job_id, result="queued_menu",
                       rating=rating)
        return PipelineResult.QUEUED_PAUSED

    await db.set_analysis_and_state(job.upwork_job_id, analysis, rating, "delivered")
    # disable_notification=True → беззвучно. False → со звуком (топовая вакансия).
    silent = rating < settings.loud_notification_threshold
    await notifier.send_job(job, analysis, silent=silent)
    await db.mark_sent(job.upwork_job_id)
    await log.emit("pipeline_finished",
                   upwork_job_id=job.upwork_job_id, result="delivered",
                   rating=rating, silent=silent)
    return PipelineResult.DELIVERED
```

**Экономия раундтрипов в БД** через объединённые UPDATE: `set_pre_rating_and_state`, `set_analysis_and_state`, `set_analysis_state_queued`. Один SQL вместо двух на каждое изменение.

---

## 5. Hard filters — rule-based отсев ДО LLM

Цель: убрать **очевидный мусор** без любых LLM-вызовов, по числовым полям. Никаких эвристик на тексте — это работа LLM.

| Правило | Default | Логика |
|---|---|---|
| Малая трата клиента | `< $0` (off) | `client_total_spent < hard_min_client_spent` |
| Низкий рейтинг клиента | `< 0` (off) | `client_rating < threshold` AND `hires >= hard_min_hires_for_rating` |
| Низкий бюджет (Hourly) | `< $0` (off) | парсим строку «$5-$15», берём верхнюю границу |
| Низкий бюджет (Fixed) | `< $0` (off) | парсим число из строки |
| 0 наймов клиента | OFF | `hard_reject_no_hires` |
| Старая вакансия | OFF (`= 0`) | `published_date < now() - hard_max_vacancy_age_h hours` |

Все пороги в `bot_settings`, редактируются через UI (см. [bot/BOT.md](bot/BOT.md) §3.4). Все по умолчанию `0` = выключены — оператор настраивает сам.

```python
import re
from datetime import datetime, timezone
from src.models import Job, BotSettings

HOURLY_BUDGET_RE = re.compile(r"\$?(\d+(?:\.\d+)?)(?:\s*-\s*\$?(\d+(?:\.\d+)?))?")
FIXED_BUDGET_RE  = re.compile(r"\$?(\d+(?:[\.,]\d+)?)")

def parse_hourly_budget_max(s: str | None) -> float | None:
    """Из '$5-$15' возвращает 15.0. Из '$30' возвращает 30.0. None если непарсимо."""
    if not s: return None
    m = HOURLY_BUDGET_RE.search(s)
    if not m: return None
    return float(m.group(2) or m.group(1))

def parse_fixed_budget(s: str | None) -> float | None:
    """Из '$500' или 'Fixed-price 250' возвращает число."""
    if not s: return None
    m = FIXED_BUDGET_RE.search(s.replace(",", ""))
    return float(m.group(1)) if m else None

def hard_filter(job: Job, settings: BotSettings) -> str | None:
    """Возвращает короткую причину отказа или None если прошёл."""

    if settings.hard_min_client_spent > 0:
        spent = job.client_total_spent or 0
        if spent < settings.hard_min_client_spent:
            return f"low_spent:${spent:.0f}"

    if settings.hard_min_client_rating > 0:
        hires  = job.client_total_hires or 0
        rating = job.client_rating or 0
        if hires >= settings.hard_min_hires_for_rating and rating < settings.hard_min_client_rating:
            return f"low_rating:{rating:.1f}"

    if settings.hard_min_budget_hourly > 0 and job.budget_type == "Hourly":
        mx = parse_hourly_budget_max(job.budget)
        if mx is not None and mx < settings.hard_min_budget_hourly:
            return f"low_hourly:${mx:.0f}"

    if settings.hard_min_budget_fixed > 0 and job.budget_type == "Fixed":
        bg = parse_fixed_budget(job.budget)
        if bg is not None and bg < settings.hard_min_budget_fixed:
            return f"low_fixed:${bg:.0f}"

    if settings.hard_reject_no_hires and (job.client_total_hires or 0) == 0:
        return "no_hires"

    if settings.hard_max_vacancy_age_h > 0 and job.published_date:
        age_h = (datetime.now(timezone.utc) - job.published_date).total_seconds() / 3600
        if age_h > settings.hard_max_vacancy_age_h:
            return f"stale:{age_h:.0f}h"

    return None
```

---

## 6. Парсинг рейтинга

```python
import re
RATING_RE = re.compile(r"РЕЙТИНГ:\s*([0-9]+(?:[.,][0-9])?)", re.IGNORECASE)

def parse_rating(text: str) -> int:
    """Парсит РЕЙТИНГ: N из ai_analysis, clamped to 0..10. 0 если regex не нашёл."""
    if not text: return 0
    m = RATING_RE.search(text)
    if not m: return 0
    val = float(m.group(1).replace(",", "."))
    return max(0, min(10, int(round(val))))

def parse_pre_rating(text: str) -> int | None:
    """Pre-screen: None если ответ непарсимый (проходить фильтр НЕ должен).
    Защита от 'непонятный ответ → дефолт 5 → проходит'."""
    if not text: return None
    m = re.search(r"\d+", text)
    if not m: return None
    val = int(m.group(0))
    return val if 0 <= val <= 10 else None
```

---

## 7. Гарантии и инварианты

| # | Гарантия | Как обеспечивается |
|---|---|---|
| 1 | **«Получили = записали»** | INSERT в `webhook_inbox` синхронно ДО ответа 200. БД упала → 5xx → скрейпер ретраит |
| 2 | **«Save first, then process»** | `upsert_and_get_state` в `upwork_jobs` ДО любого LLM-вызова. Крэш = вакансия в БД с `processing_state='pending'` |
| 3 | **Атомарные state-переходы** | Каждое изменение `processing_state` — один SQL-statement (UPDATE). Никаких полу-состояний |
| 4 | **Recovery зависших** | Cron `recover_stuck_jobs` каждые 10 минут поднимает `pending`/`pre_screened` старше 10 мин. После 3 attempts → `failed`. Реализация — §9 |
| 5 | **Аудит каждого приёма** | Событие `job_received` пишется сразу после upsert (ДО LLM). В `/Логи` видно даже если pipeline дальше упал |
| 6 | **Защита от мусорного payload** | `normalize_failures` хранит сырой JSON для разбора. Никакой silent drop |

---

## 8. Управление объёмом БД

При 200 вакансий/день без управления БД росла бы на ~700 MB/год. Стратегия даёт устойчивые ~5 MB.

**Все три типа отбраковки → DELETE целиком.** Симметричная логика без специальных случаев:
- Hard filter (rule-based, до LLM) → `DELETE` сразу
- Pre-screen filter (LLM сказала ниже порога) → `DELETE` сразу
- Analysis filter (полная LLM сказала ниже порога) → `DELETE` сразу

Видимость в логах сохраняется через `pipeline_finished` события (с `result`, `rating`, `job_title`) — оператор увидит факт отбраковки в кнопке `Логи`, даже если строка в БД не осталась.

**Сохраняются в БД только:**
- `delivered` — полный анализ, прошло все фильтры, отправлено в TG
- `analyzed` с `queued_reason` — копится в очереди (Отчёт/Синхронизация)
- `failed` — dead-letter для разбора
- `is_favorite = true` — выбор оператора

**TTL daily-cron `compact_and_cleanup_jobs`** для долгоживущих:

```sql
-- 1. Стрип тяжёлых полей у отправленных старше 30 дней (но не избранное)
UPDATE upwork_jobs
SET job_description = NULL, ai_analysis = NULL,
    client_reviews = NULL, questions = NULL
WHERE is_sent = true AND is_favorite = false
  AND created_at < now() - interval '30 days'
  AND ai_analysis IS NOT NULL;

-- 2. Удаление failed старше 14 дней
DELETE FROM upwork_jobs
WHERE processing_state = 'failed'
  AND created_at < now() - interval '14 days';

-- 3. Удаление старых отправленных (90+ дней, не избранное)
DELETE FROM upwork_jobs
WHERE is_sent = true AND is_favorite = false
  AND created_at < now() - interval '90 days';

-- 4. Зависшие в очереди > 30 дней — DELETE целиком (оператор не разобрал)
DELETE FROM upwork_jobs
WHERE processing_state = 'analyzed'
  AND queued_reason IS NOT NULL
  AND created_at < now() - interval '30 days';
```

**Избранное** (`is_favorite = true`) — никогда не удаляется.

**Цифры** (200 вакансий/день, ~85% отбраковано фильтрами, ~15% доходят до TG, 5 в избранное):
- Отбракованные → DELETE сразу = **0 байт в БД**
- Полные delivered (30/день × 30 дней × 5 KB) = ~4.5 MB
- Стрипованные delivered (30/день × 60 дней × 0.15 KB) = ~270 KB
- Избранное (5/день × 365 дней × 5 KB, не удаляется) = ~9 MB/год
- **Итого устойчивое состояние: ~5 MB**, растёт только избранное

vs ~700 MB/год без управления — уменьшение в **~140×**.

---

## 9. Cron-задачи

Реализованы как 6 фоновых `asyncio.create_task` циклов в том же процессе ([cron.py](cron.py)) — без APScheduler.

```python
# src/cron.py
import asyncio
from src import db, log

async def _loop(coro, period_s: int):
    while True:
        try:
            await coro()
        except Exception:
            log.exception("cron_failed", task=coro.__name__)
        await asyncio.sleep(period_s)

def start_cron(pool):
    asyncio.create_task(_loop(lambda: recover_stuck_jobs(pool),       600))
    asyncio.create_task(_loop(lambda: compact_and_cleanup_jobs(pool), 86400))
    asyncio.create_task(_loop(lambda: cleanup_inbox(pool),            3600))
    asyncio.create_task(_loop(lambda: cleanup_events(pool),           86400))
    asyncio.create_task(_loop(lambda: prompts_history_trim(pool),     86400))
    asyncio.create_task(_loop(lambda: alert_error_burst(pool),        900))
```

| Задача | Период | Что делает |
|---|---|---|
| `recover_stuck_jobs` | 10 мин | подобрать застрявшие в `pending`/`pre_screened` старше 10 мин, вернуть в обработку |
| `compact_and_cleanup_jobs` | сутки | стрип/удаление по retention-таблице (см. §8) |
| `cleanup_inbox` | час | `DELETE FROM webhook_inbox WHERE received_at < now() - interval '7 days'` (FK CASCADE чистит и `normalize_failures`) |
| `cleanup_events` | сутки | `DELETE FROM bot_events WHERE ts < now() - interval '7 days'` |
| `prompts_history_trim` | сутки | оставлять последние 50 записей на слот |
| `alert_error_burst` | 15 мин | если > 5 событий level=error за 15 мин — отправить оператору сообщение |

```python
async def recover_stuck_jobs(pool):
    """Подбирает вакансии, застрявшие в pending/pre_screened > 10 минут.
    Это происходит при крэше процесса посередине pipeline."""
    rows = await pool.fetch("""
        UPDATE upwork_jobs
        SET attempts = attempts + 1
        WHERE processing_state IN ('pending', 'pre_screened')
          AND updated_at < now() - interval '10 minutes'
          AND attempts < 3
        RETURNING upwork_job_id;
    """)
    if not rows:
        return
    await log.emit("recovery_triggered", count=len(rows),
                   job_ids=[r["upwork_job_id"] for r in rows[:10]])
    for r in rows:
        asyncio.create_task(reprocess_job(r["upwork_job_id"]))

    # После 3 attempts — в dead-letter
    await pool.execute("""
        UPDATE upwork_jobs SET processing_state = 'failed',
            last_error = 'stuck_recovery_exceeded'
        WHERE processing_state IN ('pending', 'pre_screened')
          AND updated_at < now() - interval '10 minutes'
          AND attempts >= 3;
    """)


async def alert_error_burst(pool):
    n = await pool.fetchval("""
        SELECT COUNT(*) FROM bot_events
        WHERE level = 2 AND ts > now() - interval '15 minutes'
    """)
    if n >= 5:
        await bot.send_message(
            ALLOWED_USER_IDS[0],
            f"Внимание: за 15 минут зафиксировано {n} ошибок. Проверьте Логи."
        )
```
