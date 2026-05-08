# CHAT.md — Chat-подсистема

Двусторонняя связь с клиентами Upwork через email-bridge: IMAP-чтение
входящих сообщений и SMTP-отправка ответов через `mg.upwork.com` Reply-To
токены. AI отвечает автоматически в режиме остановки бота, ручной режим
через Telegram-уведомления.

Этот файл — обзор подсистемы (Phase 0 — foundation). Конкретные модули
ниже реализуются итеративно по фазам.

---

## Содержание

1. [Цель](#1-цель)
2. [Триггеры](#2-триггеры)
3. [Модель данных](#3-модель-данных)
4. [Конфигурация](#4-конфигурация)
5. [Компоненты](#5-компоненты)
6. [Ночной AI](#6-ночной-ai)
7. [UI Telegram](#7-ui-telegram)
8. [Этапы разработки](#8-этапы-разработки)

---

## 1. Цель

Лифт конверсии `interview → hire` (текущая ~12.5%) до 18-25% через:

- **Day mode** (`is_paused = false`): мгновенные push-уведомления в Telegram
  при новых сообщениях клиентов. Оператор отвечает руками через Upwork web.
- **Night mode** (`is_paused = true`): AI отвечает автоматически с целью
  «потянуть время» до возвращения оператора. Не закрывает сделку, не
  называет цены/сроки. Только acknowledge + один probe-вопрос.

**Не-цели:** автоматизация submit'а proposals, браузерная автоматизация,
multi-user, замена ручных ответов оператора в day mode.

---

## 2. Триггеры

Один глобальный тумблер `bot_settings.is_paused` управляет всем:

| `is_paused` | `chat_ai_night_enabled` | Поведение при входящем |
|---|---|---|
| `false` | — | Громкий push в TG, ты отвечаешь руками |
| `true`  | `true`  | AI генерит ответ, post-validate, отправка через SMTP |
| `true`  | `false` | Сообщение копится в Отчёт без AI-ответа |

Это подсистема явно не имеет собственного toggle "мы спим / мы работаем" —
переиспользуем существующий `▶️ Запустить / ⏸ Остановить` в главном меню.

---

## 3. Модель данных

Одна новая таблица `chat_messages` (миграция 001_chat.sql). Без отдельной
таблицы threads — тред = группа сообщений с одинаковым `email_thread_key`.

### 3.1 chat_messages

См. `migrations/001_chat.sql` для полного DDL. Ключевые колонки:

| Колонка | Назначение |
|---|---|
| `email_thread_key` | sha256 от In-Reply-To или fallback hash |
| `direction` | `'in'` (от клиента) / `'out'` (мы / AI) |
| `body_text` | очищенный от HTML текст сообщения |
| `client_name`, `job_title`, `job_url` | кэш для UI (без JOIN) |
| `email_message_id`, `email_in_reply_to`, `raw_email_uid` | для thread-reconstruction и дедупа |
| `ai_generated`, `ai_model` | для аналитики (только для `out`) |
| `escalate_reason` | NULL = AI ответил, текст = AI промолчал |
| `is_shown_in_report` | флаг для drain'а в Отчёте |

### 3.2 Расширение bot_settings

| Колонка | Default | Назначение |
|---|---|---|
| `chat_ai_night_enabled` | `false` | Глобальный switch для AI-режима. По умолчанию выключен — Phase 1 даёт только notification без AI. Включается через TG (`Настройки → AI ответ при остановке`) после того как оператор убедился что IMAP/SMTP работают стабильно. |
| `chat_ai_delay_min_seconds` | 60 | Минимум задержки перед SMTP-отправкой |
| `chat_ai_delay_max_seconds` | 120 | Максимум задержки (рандом в диапазоне) |

### 3.3 Новый prompt slot

ENUM `prompt_slot` расширен значением `'dialog_night'`. Bootstrap дефолтного
текста — в [src/migrations.py](../migrations.py) `DEFAULT_PROMPTS`.

---

## 4. Конфигурация

IMAP/SMTP credentials хранятся в `secrets` таблице (приоритет: БД → env).
Match существующего паттерна `db.get_openrouter_key()`.

### 4.1 Bootstrap из env

При первом старте контейнера значения копируются из env-переменных в
`secrets` (если запись там пуста). После — берутся из БД через TTL-кэш.

```env
IMAP_HOST=imap.mail.me.com  # дефолт, обычно не меняется
IMAP_PORT=993
IMAP_USER=immunerebel@icloud.com
IMAP_PASSWORD=<app-specific password от appleid.apple.com>
IMAP_FOLDER=INBOX

SMTP_HOST=smtp.mail.me.com  # дефолт
SMTP_PORT=587
SMTP_USER=immunerebel@icloud.com  # обычно тот же что IMAP_USER
SMTP_PASSWORD=<тот же app-specific password>
```

### 4.2 Через Telegram UI

`Настройки → Email подключение` — inline-меню с 4 кнопками:

- IMAP login → FSM редактирования
- IMAP пароль → FSM редактирования (с auto-delete сообщений после Save)
- SMTP login → FSM
- SMTP пароль → FSM

Реализация — [src/bot/handlers/email_creds.py](../bot/handlers/email_creds.py).

### 4.3 App-specific password (iCloud)

Apple требует **app-specific password** для IMAP/SMTP, не основной iCloud
пароль. Получить:

1. https://appleid.apple.com → Sign-In and Security
2. App-Specific Passwords → Generate
3. Имя: `Upwork Bot`
4. Скопировать 16-символьный `xxxx-xxxx-xxxx-xxxx`

Пароль показывается **один раз** — сохранить сразу.

Включена должна быть 2FA (без неё Apple не даёт app-specific password).

---

## 5. Компоненты

Структура `src/chat/`:

```
src/chat/
├── __init__.py
├── CHAT.md           ← этот файл
├── inbox.py          ← IMAP IDLE watcher (Phase 1)
├── parser.py         ← парсинг email body, извлечение текста (Phase 1)
├── thread_resolver.py ← email → thread mapping (Phase 1)
├── escalate.py       ← pre-gate + post-validator (Phase 2)
├── dialog_ai.py      ← LLM call с dialog_night промтом (Phase 2)
├── outbox.py         ← SMTP send + delayed queue (Phase 2)
└── repository.py     ← chat-specific SQL (Phase 1)
```

Каждый модуль в Phase 0 — skeleton с типами/интерфейсами без реализации.

### Принципы изоляции

1. Никаких прямых вызовов `pipeline.py ↔ chat/*`
2. IMAP/SMTP/LLM завёрнуты в существующий `safe_external_call`-pattern
3. Падение IMAP-watcher не должно ронять процесс (cron `_loop()` глотает)
4. Reuse: `src.llm._call`, `src.notifier._safe_send_html`, `src.log.emit`,
   `src.db._conn()` pool

---

## 6. Ночной AI

### 6.1 Pipeline

```
[Клиент написал в is_paused=true] → IMAP IDLE
        ↓
[parser → resolver → INSERT chat_messages direction='in']
        ↓
[escalate.pre_gate(message)]
        ├── True → INSERT escalate_reason, без AI-ответа
        └── False → продолжаем
                ↓
[dialog_ai.generate(thread_history, prompt='dialog_night')]
        ├── result == "__ESCALATE__: ..." → mark escalate, не отправляем
        └── result == текст → продолжаем
                ↓
[escalate.post_validate(text)]
        ├── Failed → mark escalate, не отправляем
        └── Passed → продолжаем
                ↓
[asyncio.sleep(random(min_seconds, max_seconds))]
        ↓
[Перед SMTP: race-check on db.has_recent_human_outbound]
        ├── Оператор написал руками → cancel
        └── Тишина → SMTP send → INSERT direction='out' ai_generated=true
```

### 6.2 Что AI имеет право

✅ Можно (по `dialog_night` промту):
- Specific acknowledgment (упомянуть конкретику из сообщения клиента)
- Один probe-вопрос (Stage 2 style по dialog.md V5.0)
- Обещание подробного ответа когда оператор вернётся

🛑 Никогда:
- Цена / $ / hourly / fixed
- Сроки выполнения работы
- Close-формулы
- Multiple questions
- Em-dash, AI-словарь (robust/seamless/leverage/utilize/optimize/...)

### 6.3 Escalate triggers

**Pre-gate (до LLM):**
- Hot keywords: price, budget, quote, contract, when can you start, $
- Не-английский текст
- Длина > 300 слов
- Запрос на созвон

**Post-validate (после LLM):**
- Em-dash detected
- Money pattern (`$\d+`)
- Time commits (`will fix in X`, «X days/hours»)
- Multiple `?`
- AI вернул `__ESCALATE__: ...`

### 6.4 Race conditions

| Race | Решение |
|---|---|
| Оператор пишет руками пока AI готовится | `db.has_recent_human_outbound()` перед SMTP |
| Оператор жмёт Запустить во время задержки | Перед SMTP проверяем `is_paused` — если false, отменяем |
| Два IMAP fetch'а одного UID | UNIQUE на `raw_email_uid` + `ON CONFLICT DO NOTHING` |

---

## 7. UI Telegram

### 7.1 Главное меню

`Отчёт (N+M)` — N вакансий + M chat-сообщений. См. BOT.md §1 (расширение
существующих счётчиков).

### 7.2 Подменю Отчёт

После `Отчёт` — две sub-кнопки:
- `Показать вакансии (N)` — существующая функциональность
- `Показать сообщения (M)` — drain ночных Q+A пар (Phase 3)

### 7.3 Карточка ночного диалога

```
💼 [job_title]
👤 [client_name]
🕐 [received_at]

📥 Клиент: [body_text от клиента]

📤 AI ответил ([sent_at]): [body_text от AI]

[🔗 Открыть чат в Upwork]
```

Если AI escalate (нет out-сообщения в треде за ночь) — карточка не
показывается, в footer dump'а строка «⚠️ Ещё N сообщений без ответа AI».

### 7.4 Day mode notification

```
💼 [job_title]
👤 [client_name]
🕐 [received_at]

[body_text от клиента, обрезано до 400 chars]

[🔗 Открыть чат в Upwork]
```

Громкий push (без `disable_notification`).

### 7.5 Settings

`Настройки → Email подключение` — inline-меню для редактирования IMAP/SMTP
credentials (Phase 0 done).

`Настройки → Изменить промт → Промпт: AI ответ` — редактирование
`dialog_night` слота (Phase 0 done).

---

## 8. Этапы разработки

### Phase 0 — Foundation (✅ done)
- Миграция 001_chat.sql
- Расширения bot_settings, ai_prompts ENUM
- Skeleton `src/chat/`
- Settings UI: IMAP/SMTP credentials + dialog_night prompt slot

### Phase 1 — Inbox only (notification, без AI)
- `chat/inbox.py` — IMAP IDLE через `aioimaplib`
- `chat/parser.py` — извлечение body
- `chat/thread_resolver.py` — match thread
- `chat/repository.py` — изолированный SQL
- `notifier.send_inbound_alert()` — простая карточка
- Cron-loop регистрация
- Тесты с mock IMAP

**Ценность:** мгновенные уведомления = 80% лифта конверсии.

### Phase 2 — Outbox + Night AI
- `chat/escalate.py` — pre-gate + post-validator
- `chat/dialog_ai.py` — LLM call (через `src.llm._with_fallback`)
- `chat/outbox.py` — SMTP через `aiosmtplib` + delayed send
- Тесты с mock SMTP/LLM

### Phase 3 — UI (Отчёт integration)
- `bot/handlers/dialogs.py` — `Показать сообщения` sub-меню
- Расширение `bot/handlers/reports.py`
- `notifier.send_qna_card()`
- Расширение счётчика в `keyboards.main_menu_kb`

### Phase 4 — Прод-обкатка
- Sample реальных писем от Upwork для калибровки парсера
- 1 неделя на test-mailbox без crash
- Метрики: count of escalate / sent / validation_failed
