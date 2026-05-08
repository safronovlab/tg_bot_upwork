# `[Core] Upwork AI Pipeline` — логика работы

**n8n workflow ID:** `oYscgTdUgQm3okPr`
**Статус:** `active = false` (выключен) — обратите внимание
**Узлов:** 30
**Триггер:** HTTP Webhook `POST /upwork-lead`

Документ описывает: что приходит на вход, как преобразуется на каждом шаге, какие ветвления и условия, что в итоге пишется в БД и отправляется в Telegram.

---

## 🔭 Карта потока (high-level)

```
Webhook ─► Split ─► Extract ─► Read Settings ─► [Bot Paused?]
                                                   │
                                                   ▼
                                        Save Job (UPSERT)
                                                   │
                                                   ▼
                                        Already Analyzed?
                                                   │
                                          (нет)    │  (да) ─► STOP
                                                   ▼
                                   Pre-Screen: Load Prompt ─► AI Quick Rating
                                                                │
                                                                ▼
                                                       Parse Rating (0-10)
                                                                │
                                                                ▼
                                                    Save pre_rating ─► [Rating >= 5?]
                                                                              │
                                                                  (нет) STOP  │  (да)
                                                                              ▼
                                                       Load "Анализ вакансии" prompt
                                                                              │
                                                                              ▼
                                                                AI Analyzes Job (DeepSeek R1)
                                                                              │
                                                                              ▼
                                                                  [Analysis Received? len>50]
                                                                              │
                                                                  (нет) STOP  │  (да)
                                                                              ▼
                                                              Save ai_analysis ─► [Rating >= 5?]
                                                                                      │
                                                                          (нет) STOP  │  (да)
                                                                                      ▼
                                                                          [Silent Mode?]
                                                                              ┌──────┴──────┐
                                                                       (да)  │             │  (нет)
                                                                              ▼             ▼
                                                                     Postpone Until    [Is It Night?]
                                                                       Morning            ┌──────┴──────┐
                                                                                   (да)  │             │  (нет)
                                                                                          ▼             ▼
                                                                                  Postpone (Night)  Send To Telegram
                                                                                                          │
                                                                                                          ▼
                                                                                                  Mark As Sent
```

---

## 🟢 Step 1: Webhook — точка входа

**Узел:** `Step 1: Webhook New Job From Upwork`
**Тип:** `n8n-nodes-base.webhook`

```
POST /upwork-lead
Content-Type: application/json
```

**Ожидаемый формат входа** (восстановлено из дальнейших узлов):
```json
{
  "body": {
    "projects": [
      {
        "url": "...",
        "title": "...",
        "description": "...",
        "questions": "...",
        "budget_type": "Hourly|Fixed",
        "budget": "...",
        "published": "2026-...",
        "job_type": "...",
        "client_details": {
          "rank": "...",
          "total_spent": 0,
          "total_hires": 0,
          "avg_hourly_rate_paid": 0,
          "rating": 0,
          "reviews": "...",
          "registered_at": "...",
          "country": { "name": "..." }
        },
        "client_work_history": [
          { "feedback": { "score": 5, "comment": "..." }, "total_charge": 100, "title": "..." }
        ]
      }
    ]
  }
}
```

Упомянутый источник наполнения — внешний скрейпер Upwork, который шлёт массив `projects` пакетом.

---

## 🔄 Step 2-3: Нормализация входа

### Step 2: Split Into Individual Jobs
Разбивает массив `body.projects` на отдельные элементы — далее каждая вакансия идёт по конвейеру независимо.

### Step 3: Extract Job Data
**Тип:** `set` — формирует плоский объект с типизированными полями для дальнейших шагов.

| Output поле | Источник | Преобразование |
|---|---|---|
| `id` | `url` | regex `/~[0-9a-zA-Z]+/` после двойного `decodeURIComponent` — это и есть `upwork_job_id` |
| `job_title` | `title` | as-is |
| `job_description` | `description` | as-is |
| `questions` | `questions` | as-is |
| `budget_type`, `budget`, `published_date`, `job_type` | as-is | as-is |
| `upwork_url` | `url` | если содержит `url=` — извлекает и двойной decode; иначе as-is |
| `client_details_*` | `client_details.{rank,total_spent,total_hires,avg_hourly_rate_paid,rating,reviews,registered_at,country.name}` | плоские поля |
| `client_reviews` | `client_work_history[]` | агрегация: первые 15 записей с feedback → строка вида `⭐ 5/5 (💵 $X) \| 🛠 title\n💬 comment` через `\n\n`, иначе `"Отзывов нет"` |
| `is_favorite` | константа `false` | |
| `is_sent` | константа `false` | |

---

## ⚙️ Step 4: Чтение настроек бота

**Узел:** `Step 4: Read Bot Settings From DB`
**SQL:**
```sql
SELECT is_silent_mode, night_mode_override,
  CASE
    WHEN night_mode_override = 'force_on'  THEN true
    WHEN night_mode_override = 'force_off' THEN false
    WHEN night_start::time > night_end::time THEN
      (now() AT TIME ZONE 'Europe/Moscow')::time >= night_start::time OR
      (now() AT TIME ZONE 'Europe/Moscow')::time <  night_end::time
    ELSE
      (now() AT TIME ZONE 'Europe/Moscow')::time >= night_start::time AND
      (now() AT TIME ZONE 'Europe/Moscow')::time <  night_end::time
  END AS is_night_now
FROM "Settings" WHERE id = 3
```

Возвращает 3 поля:
- `is_silent_mode` (bool) — глобальный «не отправлять, копить»
- `night_mode_override` (`'auto' | 'force_on' | 'force_off' | NULL`)
- `is_night_now` (bool, вычислено в SQL по МСК с поддержкой переходящих через полночь интервалов и override)

**Замечание:** хардкод `WHERE id = 3`. Если запись `Settings` пересоздадут с другим `id` — пайплайн молча сломается.

---

## 🚦 Step 5: Bot Paused? — мёртвая ветка

**Узел:** `Step 5: Bot Paused?` (IF)
**Условие:** `"always_active" == "never_match"` (литералы)

Условие **никогда не выполняется** → всегда уходит в FALSE-ветку → `Step 6: Save Job To DB`. TRUE-ветка ведёт в `STOP: Bot Disabled` и недостижима.

**Вывод:** проверка «бот на паузе» физически отключена. Похоже на временно закомментированную логику. Либо удалить узел, либо подставить реальное условие из `Settings.bot_mode`.

---

## 💾 Step 6: Сохранение в `upwork_jobs` (UPSERT)

**Узел:** `Step 6: Save Job To DB`
**Операция:** `insert` с `matchingColumns = upwork_job_id` (по сути UPSERT по UNIQUE).

| Колонка БД | Значение из Step 3 |
|---|---|
| `upwork_job_id` | `id` (matching) |
| `job_title` | `job_title` |
| `job_description` | `job_description` |
| `budget` | `budget` |
| `questions` | `questions` |
| `upwork_url` | `upwork_url` |
| `published_date` | `published_date` |
| `job_type` | `job_type` |
| `budget_type` | `budget_type` |
| `client_country` | `client_details_country_name` |
| `client_rank` | `client_details_rank` |
| `client_total_spent` | `client_details_total_spent` |
| `client_total_hires` | `client_details_total_hires` |
| `client_avg_rate` | `client_details_avg_hourly_rate_paid` |
| `client_rating` | `client_details_rating` |
| `client_registered_at` | `client_details_registered_at` |
| `client_reviews` | `client_reviews` (агрегированная строка) |

`status`, `is_favorite`, `is_sent`, `is_night`, `pre_rating`, `ai_analysis`, `created_at`, `updated_at` — заполняются дефолтами / далее по ходу.

---

## 🧪 Step 7-8: Дедупликация

### Step 7: Already Analyzed?
```sql
SELECT COUNT(*) AS cnt FROM upwork_jobs
WHERE upwork_job_id = $1
  AND (ai_analysis IS NOT NULL OR is_sent = true);
```

### Step 8: New Job? (IF)
`cnt == 0` → дальше в Pre-Screen. Иначе — пайплайн обрывается (нет outgoing connection из FALSE-ветки).

**Семантика:** «новый» = такого `upwork_job_id` ещё не анализировали и не отправляли. Просто запись в БД (без `ai_analysis`) не считается обработанной — её «доехать» до анализа можно повторно.

---

## 🎯 Pre-Screen ветка — быстрая отсевочная оценка

Цель — дешёвой моделью убрать совсем неподходящие лиды до запуска тяжёлого анализа.

### Pre-Screen: Load Prompt
```sql
SELECT * FROM ai_prompts WHERE name = 'Pre-Screen Rating' LIMIT 1
```
Промт `id = 7` (содержит инструкцию: «вернуть ТОЛЬКО ОДНУ ЦИФРУ от 0 до 10»).

### Pre-Screen: AI Quick Rating
- **Primary:** `xiaomi/mimo-v2-flash` через OpenRouter
- **Fallback:** `deepseek/deepseek-v4-flash` (узел `FallBack` с заглавной B)

Промт + подстановка полей: `job_title`, `job_description`, `budget_type`, `budget`, `job_type`, `country`, `rating`, `total_spent`, `total_hires`.

### Pre-Screen: Parse Rating (Code-узел, JS)
```js
const aiText = $input.first().json.text || '';
const match = aiText.match(/(\d+)/);
let rating = match ? parseInt(match[1], 10) : 5;
if (isNaN(rating) || rating < 1 || rating > 10) rating = 5;
return [{ json: { ...jobData, pre_rating: rating, pre_screen_raw: aiText } }];
```

⚠ **Скрытая особенность:** при нечитаемом ответе — fallback `5`, что **проходит** фильтр `>= 5`. То есть «непонятный» ответ модели по умолчанию = «вакансия идёт дальше». Это либеральное поведение.

### Pre-Screen: Save Rating to DB
```sql
UPDATE upwork_jobs SET pre_rating = $1 WHERE upwork_job_id = $2
```

### Pre-Screen: Rating >= 5?
- TRUE → переход к полному анализу (Step 9)
- FALSE → `STOP: Low Pre-Rating`

---

## 🧠 Step 9-10: Полный AI-анализ

### Step 9: Load Prompt From DB
```sql
SELECT * FROM ai_prompts WHERE name = 'Анализ вакансии' LIMIT 1
```
Промт `id = 3`: «Master Prompt V 7.0: Conveyor Sniper — 5-color tactical edition». Должен вернуть текст, содержащий маркер `РЕЙТИНГ: <число>`.

### Step 10: AI Analyzes Job
- **Primary:** `deepseek/deepseek-r1-0528`
- **Fallback:** `minimax/minimax-m2.5` (узел `Fallback` со строчной b)

Подставляет в промт **15 полей** о вакансии и клиенте (включая `questions`, `client_reviews` и `client_details_avg_hourly_rate_paid`).

### Analysis Received? (IF)
```js
$json.text?.length > 50 ? 'yes' : 'no'
```
Защита от пустых/обрезанных ответов модели.

---

## 💾 Step 12: Сохранение анализа

```
UPDATE-вариант UPSERT по upwork_job_id:
  ai_analysis = <ответ модели>
```

(Operation: insert с matching по `upwork_job_id`.)

---

## 🎚 Rating Filter >= 5

Парсит из `ai_analysis`:
```js
Number(($('Step 10: AI Analyzes Job').first().json.text || '')
       .match(/РЕЙТИНГ:\s*(\d+)/)?.[1] || '0')
```
- TRUE → дальше
- FALSE → пайплайн обрывается (FALSE-ветка не подключена)

⚠ **Дублирующая логика:** оба узла (`Pre-Screen: Rating >= 5?` и `Rating Filter >= 5`) ставят порог `5`, но шкалы разные:
- pre-screen: «конвейеропригодность» 0-10 (шкала Conveyor Gatekeeper)
- основной анализ: «РЕЙТИНГ» 1-10 (шкала Conveyor Sniper, 5 цветов)

То, что обе шкалы используют одинаковый порог 5 — совпадение, а не общая константа. При тюнинге одной шкалы можно случайно сломать симметрию.

⚠ Если регекс не нашёл `РЕЙТИНГ:` — будет `0`, фильтр отбросит вакансию. Но `ai_analysis` всё равно уже сохранён в БД.

---

## 🌙 Step 13-16: Тихий режим / ночной режим

### Step 13: Silent Mode Enabled?
Читает `is_silent_mode` из результата Step 4.

| Silent | Действие |
|---|---|
| `true` | **Step 14:** UPDATE `is_sent = false`, `is_night = <вычислено>`. Уведомление **не уходит**, вакансия лежит в БД и ждёт ручной отправки. |
| `false` | переход к Step 15 |

### Step 14: Postpone Until Morning
```sql
UPDATE upwork_jobs
SET is_sent = false,
    is_night = (SELECT CASE ... END FROM "Settings" WHERE id = 3)
WHERE upwork_job_id = $1
```
Семантически — «отложить, разбудить утром».

### Step 15: Is It Night?
Читает `is_night_now` из Step 4.

| Night | Действие |
|---|---|
| `true` | **Step 16:** UPDATE `is_sent = false, is_night = true` — отложить до утра, не пушить. |
| `false` | **Step 17:** отправить в Telegram. |

⚠ **Дублирование:** Step 14 и Step 16 делают почти одно и то же — UPDATE `is_sent=false, is_night=...`. Step 14 пересчитывает `is_night` через SELECT, Step 16 хардкодит `true`. Можно унифицировать.

---

## 📨 Step 17: Отправка в Telegram

**chat_id:** `701492865` — хардкод одного получателя (single-user бот).

**Сообщение:**
```
{ai_analysis text}

{JOB_TITLE.toUpperCase()}
```

**Inline-кнопки:**
1. `🔗 Открыть на Upwork` → `url = upwork_url` (из Step 6)
2. `Исходник и Избранное` → `callback_data = save_<upwork_job_id>` — обрабатывается админ-ботом `[Admin] Telegram Controller`

---

## ✅ DB: Mark As Sent

```sql
UPDATE upwork_jobs SET is_sent = true
WHERE upwork_job_id = $1 AND is_sent = false
RETURNING upwork_job_id
```

Идемпотентный апдейт: повторный запуск не «отметит дважды».

---

## 🤖 Используемые модели (через OpenRouter)

| Назначение | Primary | Fallback |
|---|---|---|
| Pre-Screen quick rating | `xiaomi/mimo-v2-flash` | `deepseek/deepseek-v4-flash` |
| Полный анализ | `deepseek/deepseek-r1-0528` | `minimax/minimax-m2.5` |

Двухуровневая стратегия: **дёшево отсеиваем мусор → дорого анализируем выживших**.

---

## ⚠️ Найденные проблемы и риски

| # | Проблема | Где |
|---|---|---|
| 1 | **Workflow выключен** (`active = false`). На сервере он не запущен — webhook не принимает данные. | Свойство workflow |
| 2 | **Step 5 (Bot Paused?) — мёртвый узел.** Условие литерально false, всегда идёт «бот не на паузе». | Step 5 |
| 3 | **Хардкод `WHERE id = 3`** для `Settings`. Сломается, если строка пересоздана с другим id. | Step 4, Step 14 |
| 4 | **Хардкод `chat_id = 701492865`** в Telegram-узле. Single-user. | Step 17 |
| 5 | **Парсинг pre-rating с дефолтом 5** при нечитаемом ответе → нечитаемые ответы проходят фильтр. | Pre-Screen Parse |
| 6 | **`РЕЙТИНГ:` regex** — если формат ответа AI поменяется, фильтр обнулится молча. | Rating Filter |
| 7 | **Дубль** Step 14 и Step 16: оба «отложить», логика почти одинакова. | Step 14/16 |
| 8 | **Имена узлов `Fallback` и `FallBack`** различаются регистром — путаница, легко промахнуться при поиске/правке. | Сводка моделей |
| 9 | **`ai_analysis` сохраняется до проверки `Rating Filter`.** Низкорейтинговые анализы остаются в БД и потом ломают `Already Analyzed?` (Step 7) — повторная отправка той же вакансии будет «уже обработана». | Порядок Step 12 → Rating Filter |
| 10 | Поле `is_night` в `upwork_jobs` пишется и в Step 14, и в Step 16, хотя [улучшения БД](database_improvements.md) предлагают его выкинуть как производное. Если выкидывать — переписывать оба этих UPDATE. | Step 14/16 |

---

## 🎯 Краткая семантика пайплайна одной фразой

«Принять пакет вакансий → распарсить → сохранить → быстрый отсев дешёвой моделью → полный анализ дорогой моделью → если рейтинг ≥ 5 и сейчас не silent/не ночь → отправить одному пользователю в Telegram с кнопкой добавить в избранное».
