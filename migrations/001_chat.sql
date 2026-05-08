-- =============================================================================
-- 001_chat.sql — chat-подсистема (Phase 0 foundation)
--
-- Добавляет minimum-viable схему для chat-функционала:
--   1. ENUM-value 'dialog_night' для слота промта AI-ответа в режиме остановки
--   2. Колонки в bot_settings: chat_ai_night_enabled + delay min/max seconds
--   3. Новая таблица chat_messages (один-в-одном для in/out, без отдельного
--      thread-таблицы — тред = группа сообщений с одинаковым email_thread_key)
--
-- Additive only. Идемпотентно: безопасно перезапускать.
-- См. src/chat/CHAT.md §3 (Data model).
-- =============================================================================


-- 1. Расширение enum prompt_slot ----------------------------------------------
-- ALTER TYPE ... ADD VALUE IF NOT EXISTS работает внутри транзакции в PG12+,
-- но новое значение нельзя использовать в той же транзакции — bootstrap
-- дефолтной строки ai_prompts происходит в Python (src/migrations.py
-- _bootstrap_prompts) на следующем старте.
ALTER TYPE prompt_slot ADD VALUE IF NOT EXISTS 'dialog_night';


-- 2. Расширение bot_settings --------------------------------------------------
-- Один тумблер `is_paused` управляет всем (см. CHAT.md §2 Triggers):
--   is_paused = true  → AI отвечает (если chat_ai_night_enabled = true)
--   is_paused = false → бот шлёт push-уведомления, ты отвечаешь руками
-- ВАЖНО: default=false (Phase 1 — только notification без AI). Включается
-- оператором через TG (Настройки → AI ответ при остановке) после того как
-- IMAP/SMTP проверены в проде и оператор готов доверять AI ночью.
ALTER TABLE bot_settings
    ADD COLUMN IF NOT EXISTS chat_ai_night_enabled boolean
        NOT NULL DEFAULT false;

ALTER TABLE bot_settings
    ADD COLUMN IF NOT EXISTS chat_ai_delay_min_seconds smallint
        NOT NULL DEFAULT 60
        CHECK (chat_ai_delay_min_seconds >= 0);

ALTER TABLE bot_settings
    ADD COLUMN IF NOT EXISTS chat_ai_delay_max_seconds smallint
        NOT NULL DEFAULT 120
        CHECK (chat_ai_delay_max_seconds >= 0);


-- 3. chat_messages ------------------------------------------------------------
-- Одна таблица на всё:
--   - direction='in'  : входящее от клиента (через IMAP)
--   - direction='out' : исходящее (AI или мы) через SMTP
-- Тред = группа сообщений с одинаковым `email_thread_key` (sha256 от
-- нормализованного In-Reply-To или fallback hash(client_name|job_title)).
-- Без отдельной таблицы threads — все thread-уровневые данные считаются на лету
-- через GROUP BY email_thread_key (для single-user volume этого с запасом).
CREATE TABLE IF NOT EXISTS chat_messages (
    id                 bigserial    PRIMARY KEY,
    received_at        timestamptz  NOT NULL DEFAULT now(),

    -- Идентификация треда (sha256 = bytea(32))
    email_thread_key   bytea        NOT NULL,

    -- Soft link на радар (если знаем вакансию)
    upwork_job_id      bigint       REFERENCES upwork_jobs(id) ON DELETE SET NULL,

    -- Кэш-поля для UI без JOIN (single-user, объёмы маленькие)
    client_name        text         NOT NULL,
    job_title          text,
    job_url            text,
    subject            text,

    -- Содержимое
    direction          text         NOT NULL
                       CHECK (direction IN ('in', 'out')),
    body_text          text         NOT NULL,
    has_attachment     boolean      NOT NULL DEFAULT false,

    -- Email headers (для дедупа и thread reconstruction)
    email_message_id   text,
    email_in_reply_to  text,
    raw_email_uid      text,

    -- AI-данные (только для direction='out')
    ai_generated       boolean      NOT NULL DEFAULT false,
    ai_model           text,

    -- Escalate (только для direction='in'): NULL = AI ответил,
    -- текст = AI промолчал (hot keyword / язык / etc)
    escalate_reason    text,

    -- Флаги для UI Отчёта
    is_shown_in_report boolean      NOT NULL DEFAULT false,

    -- Время фактической SMTP-отправки (для direction='out')
    sent_at            timestamptz
);


-- Индексы под реальные запросы --------------------------------------------- --

-- Просмотр треда: последние сообщения первыми
CREATE INDEX IF NOT EXISTS chat_messages_thread_idx
    ON chat_messages (email_thread_key, received_at DESC);

-- Дедупликация по IMAP UID (повторный fetch idempotent)
CREATE UNIQUE INDEX IF NOT EXISTS chat_messages_imap_uid_idx
    ON chat_messages (raw_email_uid)
    WHERE raw_email_uid IS NOT NULL;

-- Счётчик «не показано в Отчёте» — для main_menu_kb
CREATE INDEX IF NOT EXISTS chat_messages_unshown_idx
    ON chat_messages (received_at)
    WHERE direction = 'in' AND is_shown_in_report = false;

-- Поиск по Message-ID для In-Reply-To матчинга при ингесте
CREATE INDEX IF NOT EXISTS chat_messages_message_id_idx
    ON chat_messages (email_message_id)
    WHERE email_message_id IS NOT NULL;
