# RUNBOOK — операционные процедуры

Single-user Telegram bot для AI-фильтрации Upwork-вакансий.
Всё что нужно оператору в production: deploy, наблюдение, downtime, восстановление.

Связано: [ARCHITECTURE.md](ARCHITECTURE.md), [DATABASE.md](DATABASE.md), [src/PIPELINE.md](src/PIPELINE.md), [src/bot/BOT.md](src/bot/BOT.md).

---

## 1. Первый запуск

### 1.1 Подготовка
1. Создать Telegram-бот через [@BotFather](https://t.me/botfather), скопировать `TOKEN`.
2. Узнать свой Telegram user ID (например через [@userinfobot](https://t.me/userinfobot)).
3. Получить OpenRouter API-ключ на [openrouter.ai/keys](https://openrouter.ai/keys) (формат `sk-or-v1-...`).
4. Подготовить `.env` файл:

```bash
cp .env.example .env
# затем заполнить TELEGRAM_BOT_TOKEN, ALLOWED_USER_IDS, OPENROUTER_API_KEY
```

### 1.2 Локально через docker compose

```bash
docker compose up -d
docker compose logs -f bot           # смотреть как стартует
curl http://localhost:8080/health    # должен вернуть {"status": "ok", "in_flight": 0}
```

В Telegram — отправить `/start` боту → должно прийти главное меню.

### 1.3 Coolify

1. New Resource → Docker Compose
2. Указать репозиторий, ветку
3. Скопировать `.env.example` в Environment Variables, заполнить
4. Deploy
5. Проверить `/health` через preview URL Coolify

`HEALTHCHECK` встроен в Dockerfile — Coolify автоматически рестартит контейнер если health не отвечает 3 раза подряд.

---

## 2. Что мониторить

### 2.1 Активный мониторинг
- **`/health`** — Coolify дёргает каждые 30 сек. 503 → авторестарт.
- **Webhook от скрейпера** — `tail -f docker logs` или Dozzle. Каждый POST = одна строка `batch_finished`.
- **Cron `alert_error_burst`** — каждые 15 мин шлёт сообщение оператору если за период было >= 5 событий level=error.

### 2.2 Пассивный мониторинг (через бота)
- Главное меню → `Настройки` → `Логи` → `Только ошибки` — вся история ошибок за 7 дней.
- Если за сутки пусто — система здорова.

### 2.3 Ключевые события (см. ARCHITECTURE.md §6)
| Событие | Что значит | Action |
|---|---|---|
| `pipeline_finished result=delivered` | вакансия отправлена | норма |
| `pipeline_finished result=filtered_*` | вакансия отбракована (hard / pre / analysis) | норма |
| `llm_fallback` | основная модель упала, ушли в фолбэк | предупреждение |
| `llm_failed` | обе модели упали | разобраться: ключ? rate limit? |
| `recovery_triggered count=N` | поднимаются зависшие | norm если N мал, проблема если регулярно |
| `normalize_failed` | скрейпер прислал битый JSON | смотреть `normalize_failures.raw_payload` |
| `telegram_send_failed` | Telegram не принял сообщение | rate limit / бот забанен в чате |
| `shutdown_drain_started n=N` | SIGTERM, drain N задач | штатный deploy |
| `shutdown_drain_timeout n_left=N` | drain не уложился в 30 сек | бот завершился жёстко |

---

## 3. Регулярные операции

### 3.1 Изменение модели / промта / порога / API-ключа
**Только через бот** — не лезть в БД руками. См. [src/bot/BOT.md](src/bot/BOT.md):
- `Настройки` → `Основные модели` / `Фолбэк модели` → выбрать → `Изменить модель`
- `Настройки` → `Изменить промт` → 4 слота → `Изменить`
- `Настройки` → `Пороги` → 9 порогов / `Пресеты`
- `Настройки` → `API ключ OpenRouter` → `Изменить`

Все изменения логируются в `bot_events` с `via=manual` или `via=preset_<name>`. Видны в Логах + в карточке (блок `Последние изменения`).

### 3.2 Пауза / запуск пайплайна
- Главное меню → `Запустить` / `Остановить` — мгновенно.
- Во время паузы вакансии **накапливаются**, не теряются.
- После `Запустить` накопленные доставляются через `Отчёт` (с дайджестом) или `Синхронизация` (сразу).

### 3.3 Очистка БД
- `Настройки` → `Очистить БД` → `Да, очистить` — TRUNCATE упомянутой таблицы.
- ⚠️ Только данные вакансий. `bot_settings`, `ai_prompts`, `secrets` не трогает.

### 3.4 Просмотр истории очереди
- `Отчёт (N)` — manual-очередь (накопилось при ручной паузе): дайджест → `Выгрузить все` / `Выгрузить топ-5` / `Очистить очередь`.
- `Синхронизация` — menu-очередь (накопилось пока был в меню): сразу выгружает.

---

## 4. Downtime / restart

### 4.1 Штатный restart
```bash
docker compose restart bot                    # local / VPS
# или Coolify → Restart
```
Запустит:
1. SIGTERM → `_graceful_shutdown` (см. main.py)
2. webhook начинает отвечать `503` (скрейпер ретраит)
3. polling Telegram останавливается
4. drain in-flight pipeline-задач (timeout 30 сек)
5. close БД-пула, http-сессии, http-runner

После рестарта `recover_stuck_jobs` cron подберёт всё что не успело завершиться.

### 4.2 Срочный hard kill
```bash
docker compose kill -s SIGKILL bot
```
**Что произойдёт:**
- in-flight pipeline-задачи прервутся → состояние `pending` / `pre_screened` в БД
- Через ≤10 минут `recover_stuck_jobs` поднимет их обратно (3 попытки, потом `failed` dead-letter)
- Webhook потеряет до 1 сек запросов — скрейпер ретраит на 5xx

**Допустимо**, если drain застрял.

### 4.3 БД упала
Webhook начинает отвечать **503** (см. http_app.py — `try_register_request` ловит исключение). Скрейпер ретраит. Данные не теряются.

После восстановления БД — `docker compose restart bot` чтобы пул переподключился, `recover_stuck_jobs` подберёт зависшие.

---

## 5. Backup и восстановление

### 5.1 Бэкап (ручной перед миграциями)
```bash
docker compose exec db pg_dump -U upwork upwork > backup-$(date +%F).sql
```

### 5.2 Бэкап (рекомендуемая cron на хосте, не в боте)
```cron
# /etc/cron.daily/upwork-backup
0 3 * * * cd /path/to/tg_bot && \
    docker compose exec -T db pg_dump -U upwork upwork | \
    gzip > /backups/upwork-$(date +\%F).sql.gz && \
    find /backups -name "upwork-*.sql.gz" -mtime +14 -delete
```

### 5.3 Восстановление из бэкапа
```bash
docker compose stop bot
docker compose exec -T db psql -U upwork -d upwork -c "DROP SCHEMA public CASCADE; CREATE SCHEMA public;"
gunzip -c /backups/upwork-2026-05-01.sql.gz | docker compose exec -T db psql -U upwork upwork
docker compose start bot
```

### 5.4 Восстановление при потере БД-volume
1. Docker compose поднимет пустую БД
2. `migrations.init_schema` сам применит `schema.sql` + bootstrap дефолтных промтов
3. Оператор задаёт API-ключ через бот, при необходимости — модели/пороги
4. Скрейпер шлёт новые вакансии — поток восстанавливается

**Что теряется**: история вакансий и логи. Текущие настройки восстанавливаются вручную через бот.

---

## 6. Миграции схемы БД

См. [DATABASE.md §9](DATABASE.md#9-миграции-бд).

### 6.1 Добавить миграцию
1. Создать `migrations/NNN_short_name.sql`. NNN — целое число с ведущими нулями (001, 002...).
2. Только additive операции (`ADD COLUMN IF NOT EXISTS`, `CREATE INDEX`, `INSERT`).
3. **Без `BEGIN/COMMIT`** — runner сам оборачивает в транзакцию.
4. **Перед applying на проде сделать backup** (см. §5.1).
5. Restart контейнера → runner применит миграцию автоматически.

### 6.2 Откат миграции
- Автоматического rollback **нет** (см. DATABASE.md §9.6).
- Действия:
  1. Restore из `pg_dump`-бэкапа (см. §5.3)
  2. Или написать обратную миграцию `XXX_revert_<N>.sql`

---

## 7. Тестирование (локально)

### 7.1 Установить dev-зависимости
```bash
uv pip install -e ".[dev]"
```

### 7.2 Прогон
```bash
# Быстрые unit (≈1.5 сек)
pytest --ignore=tests/test_integration_db.py

# Интеграционные с реальной Postgres (≈6 сек, требует Docker)
pytest tests/test_integration_db.py -m integration
```

### 7.3 Static analysis (gates для CI)
```bash
ruff check src tests
ruff format --check src tests
mypy src
bandit -c pyproject.toml -r src
xenon --max-absolute B --max-modules B --max-average A src
vulture src --min-confidence 80
```
Все 6 проверок должны быть зелёными до merge.

---

## 8. Troubleshooting

| Симптом | Причина | Решение |
|---|---|---|
| `/health` 503 with `db_down` | Postgres упал / сетевая проблема | Проверить `docker compose ps db`, логи db |
| `/health` 503 with `shutting_down` | идёт graceful shutdown | подождать ≤30 сек и/или проверить что бот рестартует |
| Бот не отвечает на `/start` | неверный `TELEGRAM_BOT_TOKEN` | проверить `docker compose logs bot \| grep "Polling"` |
| `AllowlistMiddleware` блокирует | user_id не в `ALLOWED_USER_IDS` | поправить env, restart |
| Все вакансии `filtered_pre` | `pre_screen_threshold` слишком высокий или промт битый | через бот: Настройки → Пороги → понизить, или Промпт: Pre-Screen → проверить |
| `llm_failed` подряд | API-ключ невалиден | через бот: Настройки → API ключ OpenRouter → Изменить |
| `recovery_triggered` каждые 10 мин | падает между upsert и mark_sent — баг или сеть | искать в логах level=error, чинить |
| Память растёт | редкий случай — открытые asyncpg-коннекты | `docker compose restart bot` (workaround); для root cause — проверить что in-flight tasks не утекают |

---

## 9. Безопасность

| Что | Где |
|---|---|
| API-ключ OpenRouter | `secrets` (БД) — никогда не логируется. Bootstrap из env только при пустой БД. |
| Telegram bot token | env. Не редактируется через бота. |
| `ALLOWED_USER_IDS` | env. Любой user не из списка получает silent drop в `AllowlistMiddleware`. |
| SQL-инъекция | защита через whitelist `_check_field` для всех динамических колонок (см. db.py). |
| `host=0.0.0.0` | OK для контейнера за reverse-proxy / Coolify; не выставлять напрямую в интернет без auth. |
| Telegram сообщения с фрагментами ключа | удаляются из чата при `Сохранить` (см. BOT.md §5). |

---

## 10. Шпаргалка команд

```bash
# Логи в реальном времени
docker compose logs -f bot

# Только ошибки за последний час
docker compose logs bot --since 1h | grep '"level":"error"'

# Подключиться к БД
docker compose exec db psql -U upwork -d upwork

# Быстрая ревизия в БД
docker compose exec db psql -U upwork -d upwork -c "
  SELECT processing_state, COUNT(*) FROM upwork_jobs GROUP BY processing_state;"

# В очереди manual / menu
docker compose exec db psql -U upwork -d upwork -c "
  SELECT queued_reason, COUNT(*) FROM upwork_jobs
  WHERE is_sent = false AND ai_analysis IS NOT NULL
  GROUP BY queued_reason;"

# Запустить миграции вручную (на самом деле это происходит автоматически при старте)
# - но можно проверить состояние:
docker compose exec db psql -U upwork -d upwork -c "SELECT * FROM schema_version ORDER BY version;"

# Сменить пароль БД (если скомпрометирован)
docker compose exec db psql -U postgres -c "ALTER USER upwork WITH PASSWORD 'new_pass';"
# обновить DATABASE_URL в .env, restart
```
