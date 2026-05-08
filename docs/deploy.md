# DEPLOY.md — деплой на Hetzner-сервер (Coolify-managed PG)

Готовность проекта подтверждена аудитом: `Dockerfile`, `compose.yml`,
`requirements.lock`, `.dockerignore`, `.env.example`, `migrations.py` —
все production-ready.

---

## §1. Предварительные условия (на сервере)

| Что | Где | Статус |
|---|---|---|
| Postgres 18-alpine | контейнер `l6bjbfahvgll4rvvtqzxv24g`, сеть `coolify` | поднят (Coolify) |
| Docker network `coolify` | `bridge`, IPv4+IPv6 | существует |
| Coolify proxy (Traefik) | `coolify-proxy` | работает (для будущих HTTPS-роутов) |
| База данных | `postgres` (db), user `postgres` | пустая, **схема создаётся автоматически на первом старте** |

DNS-alias из `coolify`-сети: `l6bjbfahvgll4rvvtqzxv24g:5432` — это и есть DSN
в `DATABASE_URL`.

---

## §2. Артефакты для загрузки

С локали → на сервер копируется **вся папка** `tg_bot/`, включая:

```
tg_bot/
├─ Dockerfile              ← multi-stage, lock-based, healthcheck
├─ compose.yml             ← production: external coolify, port 8080
├─ requirements.lock       ← все 22 deps зафиксированы
├─ pyproject.toml
├─ src/                    ← исходники
├─ migrations/             ← пусто, для будущих миграций
├─ schema.sql              ← bootstrap-схема (применяется автоматом)
├─ .env.example            ← реальные значения, готов к `cp → .env`
└─ .dockerignore           ← исключает .venv, tests, кеши и .env
```

**НЕ копируется** (исключено `.dockerignore`):
- `.venv/`, `tests/`, `.pytest_cache/`, `.mypy_cache/`, `.ruff_cache/`, `.coverage*`
- `compose.local.yml` (для локалки)
- `.env` (если он есть локально — содержит локальный DATABASE_URL)

---

## §3. ⚠️ ВАЖНО — остановить локальный бот ДО запуска сервера

Telegram разрешает только **один** consumer `getUpdates` на бот-токен.
Если локальный бот продолжает polling, server-бот получит `409 Conflict`.

```bash
# Локально:
cd /Users/dev/projects/My_own_server/tg_bot
docker compose -f compose.local.yml stop bot
```

(БД локально можно оставить запущенной — она независима от serverver-БД.)

---

## §4. Деплой через rsync + docker compose

### Шаг 1 — закинуть код на сервер

```bash
# Локально, из tg_bot/
rsync -avz -e "ssh -p 55222" \
  --exclude='.venv' --exclude='__pycache__' --exclude='.git' \
  --exclude='.mypy_cache' --exclude='.pytest_cache' \
  --exclude='.ruff_cache' --exclude='.coverage*' \
  --exclude='compose.local.yml' --exclude='.env' \
  ./ root@157.90.175.121:/opt/tg_bot/
```

### Шаг 2 — создать `.env` на сервере

```bash
ssh -p 55222 root@157.90.175.121
cd /opt/tg_bot
cp .env.example .env
# При желании — отредактировать TG-токен / OpenRouter-ключ / WEBHOOK_BEARER_TOKEN
```

### Шаг 3 — поднять контейнер

```bash
docker compose up -d --build
```

При первом старте `src/migrations.py:init_schema` автоматически:
1. Создаст `schema_version` таблицу
2. Применит `schema.sql` → 9 таблиц + enums + индексы + триггеры
3. Забутстрапит 3 дефолтных промпта в `ai_prompts` (`pre_screen`, `analysis`, `cover`)

### Шаг 4 — проверить что всё работает

```bash
# Healthcheck (HTTP)
curl -sf http://127.0.0.1:8080/health

# Логи бота — должно быть `Start polling`, без 409 Conflict
docker compose logs -f bot --since=30s

# Схема в БД
docker exec l6bjbfahvgll4rvvtqzxv24g psql -U postgres -d postgres -c '\dt'
# Ожидается: ai_prompts, bot_events, bot_settings, normalize_failures,
# prompts_history, schema_version, secrets, upwork_jobs, webhook_inbox
```

### Шаг 5 — отправить /start в Telegram

Открой `@upwork_lead_filter_bot` → `/start` → должен ответить меню.

---

## §5. Опционально — вебхук через HTTPS-домен (Coolify Traefik)

Сейчас webhook слушает `http://<server_ip>:8080/upwork-lead` с защитой по
`Authorization: Bearer <WEBHOOK_BEARER_TOKEN>`. Bearer-токен в principle хватит,
но если хочешь TLS — добавить traefik-метки в `compose.yml`:

```yaml
services:
  bot:
    ...
    labels:
      - "traefik.enable=true"
      - "traefik.http.routers.tg-bot.rule=Host(`bot.itoquant.tech`)"
      - "traefik.http.routers.tg-bot.entrypoints=https"
      - "traefik.http.routers.tg-bot.tls=true"
      - "traefik.http.routers.tg-bot.tls.certresolver=letsencrypt"
      - "traefik.http.services.tg-bot.loadbalancer.server.port=8080"
```

И тогда **убрать** `ports: 8080:8080` (Traefik сам пробрасывает наружу через :443).
DNS A-запись `bot.itoquant.tech → 157.90.175.121` должна существовать.

---

## §6. Откат и обновление

### Обновление кода
```bash
# Локально
rsync ...                                 # тот же rsync что в §4 шаг 1

# На сервере
ssh -p 55222 root@157.90.175.121
cd /opt/tg_bot
docker compose up -d --build              # пересоберётся с новыми исходниками
```

### Полная остановка
```bash
docker compose down                       # остановит и удалит контейнер
                                          # БД (Coolify-managed) НЕ затронута
```

### Откат к предыдущей сборке
Бэкап образа перед обновлением:
```bash
docker tag upwork_tg_bot:latest upwork_tg_bot:backup-$(date +%Y%m%d)
```

В случае проблем:
```bash
docker compose down
docker tag upwork_tg_bot:backup-YYYYMMDD upwork_tg_bot:latest
docker compose up -d
```

---

## §7. Проверочный чек-лист первого запуска

- [ ] Локальный бот остановлен (нет 409 Conflict)
- [ ] `.env` создан на сервере и содержит правильный `TELEGRAM_BOT_TOKEN`
- [ ] `docker compose up -d --build` отработал без ошибок
- [ ] `docker compose ps` показывает `Up (healthy)` в течение минуты
- [ ] `curl http://127.0.0.1:8080/health` отвечает `{"status": "ok", ...}`
- [ ] В логах бота строка `Start polling` без последующих error-строк
- [ ] В БД появились 9 таблиц (`\dt` в psql)
- [ ] В таблице `ai_prompts` 3 строки (pre_screen, analysis, cover)
- [ ] `/start` в Telegram возвращает приветствие + меню
- [ ] Тестовый webhook от scraper'а: `curl -X POST http://<server_ip>:8080/upwork-lead -H 'Authorization: Bearer ...' -d '{...}'` отвечает 200

---

## §8. Известные ограничения

1. **Polling vs webhook**: бот использует aiogram polling (`getUpdates`). Это означает:
   - Только один экземпляр может работать одновременно (TG limit)
   - Нет необходимости в публичном HTTPS для самого бота
   - Pипает Telegram API ~раз в секунду (нормально)

2. **Webhook scraper-input на :8080 без TLS**: защита только Bearer-токен.
   Если scraper и сервер в одной сети — приемлемо. Иначе — настроить Traefik (§5).

3. **Один replica**: бот не предназначен для горизонтального масштабирования
   (один TG-токен, in-memory FSM-storage, in-memory `_overflow_msgs`). Это OK для
   single-user assistant, но при необходимости HA — нужно redis-storage и
   webhook-режим вместо polling.

4. **БД-бэкапы**: Coolify имеет встроенные бэкапы — включить через UI
   ресурса PG → вкладка Backups. Раз в сутки в S3-compatible хранилище.
