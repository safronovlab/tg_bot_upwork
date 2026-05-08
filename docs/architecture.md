# ARCHITECTURE.md — Upwork Telegram Bot

Implementation-ready спецификация single-user Telegram-бота для приёма, AI-анализа и фильтрации Upwork-вакансий.

**Главная задача** — надёжный приём вакансий от внешнего скрейпера, два прохода LLM-анализа (отсев → полный анализ), доставка в Telegram отфильтрованных результатов с UI для управления очередью и настройками.

Этот файл — **обзорный** + cross-cutting (стек, runtime, конфиг, логи, надёжность). Подсистемы — в [§3 Карта документации](#3-карта-документации).

---

## Содержание

1. [Технологический стек](#1-технологический-стек)
2. [Структура репозитория](#2-структура-репозитория)
3. [Карта документации](#3-карта-документации)
4. [Конфигурация и секреты](#4-конфигурация-и-секреты)
5. [Запуск и runtime](#5-запуск-и-runtime)
6. [Логирование](#6-логирование)
7. [Надёжность и стабильность](#7-надёжность-и-стабильность)
8. [MVP план реализации](#8-mvp-план-реализации)
9. [Что НЕ делается (зафиксировано)](#9-что-не-делается-зафиксировано)

---

## 1. Технологический стек

| Слой | Решение | Назначение |
|---|---|---|
| Runtime | Python 3.12 + uvloop | быстрый async event loop |
| Telegram + Webhook | aiogram 3.x (встроенный `aiohttp.web` сервер) | один HTTP-стек на bot polling и POST `/upwork-lead` |
| HTTP-клиент | `aiohttp.ClientSession` (один общий) | вызовы OpenRouter |
| БД | asyncpg + prepared statements (без ORM), пул `min=2, max=10` | прямой SQL, бинарный протокол |
| Парсинг webhook payload | msgspec.Struct (frozen, gc=False) | в 3× быстрее Pydantic v2 на parse |
| Внутренние модели | `dataclass(slots=True, frozen=True)` | минимальный per-instance overhead |
| Cron | 5 `asyncio.create_task` циклов в том же процессе | без APScheduler |
| Логи | stdlib `logging` + JSON-форматтер на `msgspec.json.encode` | без structlog |
| Конфиг | `os.environ` + `dataclass(slots=True, frozen=True)` | без pydantic-settings |
| Тесты | pytest + pytest-asyncio + временная БД на сервере | без testcontainers |
| Деплой | Docker (multi-stage, `python:3.12-slim`, `uv` для install) | под Coolify |

**Целевой бюджет ресурсов** (стационарный режим):
- RAM: ~50 MB resident (с `MALLOC_ARENA_MAX=2`)
- Cold start: < 2 сек
- Docker image: ~80 MB
- VPS: 1 vCPU / 256 MB достаточно с запасом

**Что НЕ берём (с обоснованием):**
- FastAPI + uvicorn — лишний HTTP-стек ради одного route. aiogram даёт `aiohttp.web` встроенно
- httpx — дублирует aiohttp, который уже есть от aiogram
- Pydantic для внутренних моделей — для 4 dataclass-ов оверхед 4-5 MB не оправдан
- structlog — для нашего объёма stdlib `logging` + 30 строк JSON-форматтера хватает
- APScheduler — pulls in pytz и tzlocal; 5 cron-задач = 5 простых async-loop'ов
- pydantic-settings — `dataclass` + `os.environ` делает то же
- SQLAlchemy / Alembic — для 8 таблиц и ~20 запросов ORM добавляет ~50 MB и магию
- Redis — single-user, FSM в памяти. Минус контейнер
- LangChain — дополнительные ~80 MB. OpenRouter — обычный HTTPS POST через aiohttp

---

## 2. Структура репозитория

```
tg_bot/
├── pyproject.toml
├── Dockerfile
├── compose.yml
├── .env.example
├── .gitignore
├── ARCHITECTURE.md             # этот файл — обзор + cross-cutting
├── DATABASE.md                 # схема БД (8 таблиц) + миграции
├── schema.sql                  # вся актуальная схема одним файлом
├── migrations/                 # последующие изменения по числу в имени
│   └── .gitkeep
├── src/
│   ├── PIPELINE.md             # обработка вакансий: webhook, batch, process_one, hard_filter, cron
│   ├── LLM.md                  # OpenRouter, retry/fallback, prompt caching
│   ├── __init__.py
│   ├── main.py                 # entrypoint (см. §5)
│   ├── config.py               # @dataclass(slots, frozen), читает os.environ
│   ├── db.py
│   ├── models.py
│   ├── pipeline.py
│   ├── llm.py
│   ├── http_app.py
│   ├── log.py                  # JSON-форматтер + emit() (см. §6)
│   ├── cron.py
│   ├── migrations.py
│   ├── notifier.py
│   └── bot/
│       ├── BOT.md              # весь UI: меню, FSM, карточки, отчёты, логи
│       ├── __init__.py
│       ├── app.py
│       ├── auth.py
│       ├── states.py
│       ├── keyboards.py
│       ├── formatters.py
│       └── handlers/
│           ├── menu.py
│           ├── reports.py
│           ├── favorites.py
│           ├── prompts.py
│           ├── secrets.py
│           ├── models.py
│           ├── thresholds.py
│           ├── logs.py
│           └── cleanup.py
└── tests/
    ├── conftest.py
    ├── test_pipeline.py
    └── test_handlers.py
```

---

## 3. Карта документации

5 документов. Cross-cutting (этот файл) + 4 модульных, лежащих рядом со своим кодом.

| Документ | Что покрывает | Какие модули |
|---|---|---|
| ARCHITECTURE.md (этот) | стек, структура, конфиг, runtime, логирование, надёжность, MVP, не-цели | [src/main.py](src/main.py), [src/config.py](src/config.py), [src/log.py](src/log.py), [src/bot/auth.py](src/bot/auth.py), [Dockerfile](Dockerfile) |
| [DATABASE.md](DATABASE.md) | 8 таблиц, ENUM'ы, индексы; runner миграций и правила | [schema.sql](schema.sql), [src/db.py](src/db.py), [src/migrations.py](src/migrations.py), [migrations/](migrations/) |
| [src/PIPELINE.md](src/PIPELINE.md) | поток данных, webhook, batch, `process_incoming_job`, hard filters, парсинг, гарантии, retention, cron-задачи | [src/http_app.py](src/http_app.py), [src/pipeline.py](src/pipeline.py), [src/models.py](src/models.py), [src/notifier.py](src/notifier.py), [src/cron.py](src/cron.py) |
| [src/LLM.md](src/LLM.md) | OpenRouter, system+user split, retry/fallback, семафор, prompt caching | [src/llm.py](src/llm.py) |
| [src/bot/BOT.md](src/bot/BOT.md) | главное меню, авто-пауза, 3 уровня настроек, универсальный FSM-flow, карточки, Отчёт vs Синхронизация, Логи, Очистка | [src/bot/](src/bot/) целиком |

Принцип границ: модульный документ покрывает одну подсистему с минимумом «чужих» концептов. Cross-cutting (логи, конфиг, runtime) живёт в корне, потому что используется отовсюду — иначе расщепляется на 3-4 места.

---

## 4. Конфигурация и секреты

`Settings` — `dataclass(slots=True, frozen=True)`, читает `os.environ` на старте. Реализация — [src/config.py](src/config.py).

### 4.1 .env

```env
TELEGRAM_BOT_TOKEN=...
ALLOWED_USER_IDS=701492865                          # comma-separated
DATABASE_URL=postgresql://user:pass@host:5432/dbname

# bootstrap значения (после первого редактирования через бот — берутся из БД)
OPENROUTER_API_KEY=sk-or-v1-...
LLM_MODEL_PRESCREEN_DEFAULT=xiaomi/mimo-v2-flash
LLM_MODEL_ANALYSIS_DEFAULT=deepseek/deepseek-r1-0528
LLM_MODEL_PRESCREEN_FALLBACK_DEFAULT=deepseek/deepseek-v4-flash
LLM_MODEL_ANALYSIS_FALLBACK_DEFAULT=minimax/minimax-m2.5

LLM_CONCURRENCY=5
PIPELINE_BACKGROUND_TIMEOUT=120
LOG_LEVEL=INFO
```

**Auth middleware** (реализуется в [src/bot/auth.py](src/bot/auth.py)):
```python
class AllowlistMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        user_id = event.from_user.id
        if user_id not in settings.allowed_user_ids:
            return
        return await handler(event, data)
```

**Приоритет источников:**
- API-ключ: `secrets` (БД) → `OPENROUTER_API_KEY` (env)
- Имена моделей: `bot_settings.*_model` (БД) → `LLM_MODEL_*_DEFAULT` (env)

После первого редактирования через бот значения живут в БД. Restart контейнера ничего не теряет.

**Все секреты в `.gitignore`.** В Coolify хранятся как env-переменные сервиса.

### 4.2 Что меняется через Telegram, что только через env

Принцип: **всё что влияет на повседневную работу и качество — редактируется из бота.** Через env только то, что относится к деплою и безопасности контейнера.

**Через Telegram (не подключаясь к серверу):**
- API ключ OpenRouter
- Все 4 модели (Pre-Screen, Анализ, фолбэки)
- Все промты (Pre-Screen, Анализ, Cover, Ночной отчёт) с историей и откатом
- Все пороги (LLM-фильтры, hard-фильтры, громкость уведомления)
- Пресеты порогов одной кнопкой
- Пауза / Запустить
- Очистка БД
- Просмотр логов

**Только через env (при деплое):**
- `TELEGRAM_BOT_TOKEN` — секрет бота
- `DATABASE_URL` — адрес и пароль БД
- `ALLOWED_USER_IDS` — список Telegram-юзеров с доступом (security)
- `LLM_CONCURRENCY` — semaphore-лимит параллельных LLM-вызовов
- `PIPELINE_BACKGROUND_TIMEOUT` — таймаут на одну вакансию
- `LOG_LEVEL` — уровень stdout-логирования

Это разделение намеренное — критичные для безопасности значения **не должны** меняться через тот же UI что и пороги.

---

## 5. Запуск и runtime

### 5.1 Dockerfile (multi-stage)

```dockerfile
FROM python:3.12-slim AS builder
RUN pip install --no-cache-dir uv
WORKDIR /app
COPY pyproject.toml ./
RUN uv pip install --system --no-cache-dir -r pyproject.toml

FROM python:3.12-slim
WORKDIR /app
COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY src ./src
COPY schema.sql migrations ./
RUN python -m compileall src

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONOPTIMIZE=2 \
    MALLOC_ARENA_MAX=2

CMD ["python", "-m", "src.main"]
```

### 5.2 Lifespan (`src/main.py`)

```python
import asyncio, gc, uvloop, asyncpg, aiohttp
from contextlib import asynccontextmanager
from aiogram import Bot, Dispatcher
from src import config, db, cron, log, http_app, migrations
from src.bot import app as bot_app

@asynccontextmanager
async def lifespan():
    # DB pool: min=2 (warm коннекты для UI), max=10 (запас на burst-batch'и)
    pool = await asyncpg.create_pool(
        config.DATABASE_URL,
        min_size=2, max_size=10,
        command_timeout=10,
        max_inactive_connection_lifetime=300,
    )
    await migrations.init_schema(pool)           # см. DATABASE.md §9
    await db.init(pool)                          # prepared statements + bootstrap-значения

    http_session = aiohttp.ClientSession()       # один общий
    bot, dp = bot_app.build(http_session)

    cron.start_cron(pool)                        # см. PIPELINE.md §9

    runner = await http_app.start(bot, dp, port=8080)

    # gc.freeze() убирает базовые объекты из GC-цикла
    gc.collect()
    gc.freeze()

    try:
        yield
    finally:
        await dp.stop_polling()
        await asyncio.wait_for(_drain_inflight(), timeout=30)
        await pool.close()
        await http_session.close()
        await runner.cleanup()


async def main():
    async with lifespan():
        await asyncio.Event().wait()             # держим процесс


if __name__ == "__main__":
    uvloop.install()
    asyncio.run(main())
```

### 5.3 Graceful shutdown

При SIGTERM (deploy):
1. Webhook начинает отвечать `503` (флаг `app.state.shutting_down = True`)
2. `dispatcher.stop_polling()` — aiogram доигрывает текущие хендлеры
3. Ожидание in-flight pipeline-задач до 30 сек
4. Закрытие пулов (asyncpg, aiohttp)

---

## 6. Логирование

```python
# src/log.py
import logging, msgspec, sys
from src import db

class JsonFormatter(logging.Formatter):
    def format(self, r):
        rec = {"ts": r.created, "level": r.levelname.lower(), "event": r.msg}
        if hasattr(r, "data"):
            rec.update(r.data)
        return msgspec.json.encode(rec).decode()

h = logging.StreamHandler(sys.stdout)
h.setFormatter(JsonFormatter())
logging.basicConfig(handlers=[h], level=logging.INFO)
log = logging.getLogger("bot")

EVENTS_TO_PERSIST = {
    "job_received", "pipeline_finished", "batch_finished",
    "normalize_failed", "recovery_triggered",
    "llm_failed", "llm_fallback",
    "key_updated", "model_updated", "prompt_updated", "threshold_updated",
    "preset_applied",
    "db_truncated", "pipeline_failed",
}

async def emit(event: str, level=logging.INFO, **data):
    """Записать событие и в stdout, и (если важное) в bot_events для UI Логи."""
    log.log(level, event, extra={"data": data})
    if event in EVENTS_TO_PERSIST:
        lvl_int = {logging.INFO: 0, logging.WARNING: 1, logging.ERROR: 2}.get(level, 0)
        await db.insert_event(lvl_int, event, data)
```

**Безопасность:** при `key_updated` значение ключа **не** кладётся в `data` — только `updated_by`.

Из stdout события подхватывает Dozzle и Coolify-логи. По ключам впоследствии легко строится Grafana Loki.

UI просмотра событий — [src/bot/BOT.md](src/bot/BOT.md) §11. Схема таблицы — [DATABASE.md](DATABASE.md) §8.

---

## 7. Надёжность и стабильность

Сводный чек-лист инвариантов и практик. Большинство уже описано в подсистемных документах — здесь чтобы ничего не забыть при имплементации.

### 7.1 Гарантии данных

| Инвариант | Реализация |
|---|---|
| Принятый webhook → запись в `webhook_inbox` синхронно | [src/PIPELINE.md](src/PIPELINE.md) §2, ответ 5xx если БД упала |
| Save first, then process | [src/PIPELINE.md](src/PIPELINE.md) §4 — upsert ДО любого LLM-вызова |
| Атомарные state-переходы | один SQL-statement с CHECK-constraint enum |
| Recovery после крэша | [src/PIPELINE.md](src/PIPELINE.md) §9 — `recover_stuck_jobs` каждые 10 минут |
| Защита от мусорного payload | [DATABASE.md](DATABASE.md) §7 `normalize_failures` + событие `normalize_failed` |
| Idempotency на webhook ретраях | [DATABASE.md](DATABASE.md) §5 `webhook_inbox` PK = sha256 |
| TRUNCATE требует Yes | [src/bot/BOT.md](src/bot/BOT.md) §12 FSM `CleanupConfirm` |
| Промт-history | [DATABASE.md](DATABASE.md) §4 `prompts_history` пишется ДО UPDATE |

### 7.2 Обработка ошибок (паттерн на каждый внешний вызов)

```python
async def safe_external_call(...):
    try:
        return await some_call()
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        await log.emit("external_call_failed", level=logging.WARNING,
                       service="openrouter", err=str(e)[:200])
        return None
    except Exception as e:
        await log.emit("unexpected_error", level=logging.ERROR,
                       where="safe_external_call", err=repr(e)[:500])
        raise   # пробрасываем дальше, чтобы recovery cron подхватил
```

Принцип: **известные ошибки → return None / fallback. Неизвестные → raise + событие.** Recovery cron подберёт что зависло.

### 7.3 Retry-стратегии

| Тип вызова | Retry-стратегия | Где |
|---|---|---|
| LLM (OpenRouter) | 1 попытка primary → 1 попытка fallback (другая модель) | [src/LLM.md](src/LLM.md) §2 `_with_fallback` |
| Telegram send | aiogram сам ретраит на `RetryAfter` | встроено |
| asyncpg запросы | Pool сам пересоздаёт соединение при `ConnectionDoesNotExistError` | встроено |
| Webhook от скрейпера | мы отвечаем 5xx → скрейпер ретраит | его задача |
| Зависшие в pipeline | recovery cron — до 3 attempts, потом dead-letter | [src/PIPELINE.md](src/PIPELINE.md) §9 |

**Намеренно НЕ ретраим:** один и тот же LLM-вызов на одной модели. Если упало 1 раз — проблема устойчива, второй вызов с тем же — пустая трата денег. Идём в fallback.

### 7.4 Таймауты везде

| Операция | Таймаут | Где |
|---|---|---|
| Любой SQL запрос | 10 сек | `command_timeout` в asyncpg pool |
| LLM pre-screen | 60 сек | `_call(timeout_s=60)` |
| LLM analysis | 120 сек | `_call(timeout_s=120)` |
| Pipeline на одну вакансию | 120 сек | `asyncio.wait_for` в batch processor |
| Graceful shutdown drain | 30 сек | [§5.3](#53-graceful-shutdown) |

### 7.5 Память и утечки

| Источник утечки | Защита |
|---|---|
| `aiohttp.ClientSession` per call | один общий session на весь процесс |
| asyncpg connection per call | pool с `max=10` и `max_inactive_connection_lifetime=300` |
| Расширение FSM состояний | MemoryStorage очищается на cancel/завершение |
| `bot_events` рост | cleanup cron раз в сутки + 7-day retention |
| `upwork_jobs` рост | стрип + TTL по retention-таблице ([src/PIPELINE.md](src/PIPELINE.md) §8) |
| Прогрев Python кэшей | `gc.freeze()` после lifespan-startup ([§5.2](#52-lifespan-srcmainpy)) |

### 7.6 Минимальный мониторинг

Оператору достаточно периодически проверять кнопку `Логи` → `Только ошибки`. Если за сутки пусто — система здорова.

Для проактивного алёрта работает cron `alert_error_burst` каждые 15 минут ([src/PIPELINE.md](src/PIPELINE.md) §9): если > 5 событий level=error — отправляет оператору сообщение.

### 7.7 Минимальные тесты перед деплоем

```python
# test_pipeline.py
async def test_pipeline_full_path(test_pool):
    """Полный цикл от webhook до доставки в TG (с моком LLM и notifier)."""

async def test_pipeline_pre_screen_filter_deletes(test_pool):
    """Pre-screen filter → строка удалена из БД."""

async def test_pipeline_analysis_filter_deletes(test_pool):
    """Analysis filter → строка тоже удалена."""

async def test_hard_filter_deletes_low_spent(test_pool):
    """Hard filter (low_spent) — DELETE без LLM."""

async def test_recovery_picks_stuck(test_pool):
    """Зависшая в pending > 10 мин подбирается recovery cron."""

async def test_idempotent_webhook(test_client):
    """Повторный POST с тем же body → duplicate, второй LLM-вызов не делается."""

# test_handlers.py
async def test_pause_resume_persists():
    """Запустить/Остановить — флаг is_paused сохраняется в БД."""

async def test_menu_auto_pause():
    """Вход в Избранное → is_paused_menu=true. Назад → false."""

async def test_drain_report_only_manual_queue():
    """Отчёт выгружает только queued_reason='manual'."""

async def test_universal_save_button_flow():
    """ThresholdEdit: ввод → preview → Сохранить → UPDATE → Сохранено."""
```

Не нужно покрыть 100% — достаточно happy path + ключевые edge cases.

---

## 8. MVP план реализации

### День 1 — каркас и приём вакансий
1. Скелет: aiogram + aiohttp.web в одном процессе с lifespan ([§5.2](#52-lifespan-srcmainpy))
2. `AllowlistMiddleware` (1 user из env) ([§4.1](#41-env))
3. asyncpg pool (`min=2, max=10`)
4. **Migration runner** ([DATABASE.md](DATABASE.md) §9) — применяет `schema.sql` если БД пустая, потом pending миграции
5. Bootstrap: вставить дефолтные значения в `bot_settings`, `ai_prompts`, `secrets` из env
6. Webhook `POST /upwork-lead` с idempotency ([src/PIPELINE.md](src/PIPELINE.md) §2)
7. `process_incoming_job` end-to-end с pre-screen + analysis (без pause/queue ветки)
8. `/start` + главное меню (без счётчиков пока)
9. `log.py` ([§6](#6-логирование)) + минимальные события: `webhook_received`, `job_received`, `pipeline_finished`, `llm_call`

### День 2 — UI и очереди
10. Inline-кнопки `Открыть на Upwork` + `Избранное` на карточке
11. Меню `Избранное` со списком + inline `Анализ`/`Карточка`/`Удалить`
12. Ручная пауза `is_paused` + кнопка `Запустить`/`Остановить`
13. Авто-пауза `is_paused_menu` при входе в Избранное/Отчёт/Настройки
14. `queued_reason` в pipeline (`manual` / `menu`)
15. Хендлер `Отчёт` (drain manual с дайджестом) + `Синхронизация` (drain menu сразу)
16. Счётчики на `Отчёт` и `Избранное` в главном меню

### День 3 — настройки и расширение
17. Меню настроек (3 уровня: Меню → Подменю → Карточка)
18. Универсальный FSM-паттерн с `Сохранить` ([src/bot/BOT.md](src/bot/BOT.md) §4)
19. Редактирование 4 моделей через FSM
20. Редактирование API-ключа OpenRouter (с удалением сообщения)
21. Меню «Изменить промт» (4 слота) + история в `prompts_history` + загрузка `.txt`
22. Подменю «Пороги» (9 порогов + toggle)
23. Подменю «Пресеты» (3 пресета)
24. Блок «Последние изменения» в карточках
25. Кнопка `Логи` с пагинацией и фильтром «Только ошибки»
26. Кнопка `Очистить БД` с Да/Нет
27. Hard filters в pipeline

### День 4 — надёжность и оптимизация
28. Recovery cron (`recover_stuck_jobs`)
29. Compact & cleanup cron (`compact_and_cleanup_jobs`)
30. Все остальные cleanup-задачи
31. `alert_error_burst` cron ([§7.6](#76-минимальный-мониторинг))
32. Семафор LLM, prompt caching через system+user split
33. Громкость уведомлений (silent/loud по rating)
34. Graceful shutdown с drain
35. Тесты `test_pipeline.py` + `test_handlers.py` ([§7.7](#77-минимальные-тесты-перед-деплоем))
36. Dockerfile, deploy под Coolify

---

## 9. Что НЕ делается (зафиксировано)

| Не делаем | Причина |
|---|---|
| Multi-user / команда | Single-user в архитектуре, переделка ломает state-логику |
| Веб-морда | Telegram уже UI; контейнер + auth + фронтенд = over-engineering |
| Auto-генерация cover letter | Промт `cover` остаётся как редактируемый, генерация — будущая итерация (качество AI пока не дотягивает) |
| Stats / метрики / dashboards | События в `bot_events` достаточно, дашборд не нужен MVP |
| Время сна / автоматический ночной режим | Удалено; pause через `is_paused` + меню |
| Отправка отклика на Upwork за оператора | Upwork API не позволяет |
| LLM cache в БД | Каждая вакансия уникальна, кэш не сработает |
| Локальная LLM на VPS | Pre-screen и так $6/мес, не оправдано |
| Tiered анализ (cheap для borderline, R1 для топ) | Риск незаметной потери хороших вакансий перевешивает экономию |
| Кастомные звуки на каждый рейтинг | Telegram Bot API не поддерживает, реализовано как silent/loud threshold |
| SQLAlchemy/Alembic, FastAPI/uvicorn, structlog, Redis, APScheduler, pydantic-settings, LangChain | Лишний RAM/зависимости для нашего объёма |
