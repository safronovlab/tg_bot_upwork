-- =============================================================================
-- Upwork Telegram Bot — actual baseline schema
-- Применяется один раз на пустую БД (см. src/migrations.py).
-- Последующие изменения — только через нумерованные файлы migrations/NNN_*.sql
-- =============================================================================

-- ENUMS -----------------------------------------------------------------------
CREATE TYPE proc_state AS ENUM
  ('pending', 'pre_screened', 'analyzed', 'delivered', 'filtered', 'failed');

CREATE TYPE prompt_slot AS ENUM
  ('pre_screen', 'analysis', 'cover', 'night_report');

-- 3.1 upwork_jobs -------------------------------------------------------------
CREATE TABLE upwork_jobs (
  id                   bigserial PRIMARY KEY,
  upwork_job_id        text        NOT NULL UNIQUE,
  created_at           timestamptz NOT NULL DEFAULT now(),
  updated_at           timestamptz NOT NULL DEFAULT now(),

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

  pre_rating           smallint,
  ai_analysis          text,
  rating               smallint,

  processing_state     proc_state  NOT NULL DEFAULT 'pending',
  attempts             smallint    NOT NULL DEFAULT 0,
  last_error           varchar(200),
  is_favorite          boolean     NOT NULL DEFAULT false,
  is_sent              boolean     NOT NULL DEFAULT false,

  queued_reason        text        CHECK (queued_reason IN ('manual', 'menu'))
);

CREATE OR REPLACE FUNCTION touch_updated_at() RETURNS trigger AS $$
BEGIN NEW.updated_at = now(); RETURN NEW; END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_upwork_jobs_updated_at BEFORE UPDATE ON upwork_jobs
  FOR EACH ROW EXECUTE FUNCTION touch_updated_at();

CREATE INDEX upwork_jobs_queue_idx
  ON upwork_jobs (created_at)
  WHERE is_sent = false AND ai_analysis IS NOT NULL;

CREATE INDEX upwork_jobs_favorites_idx
  ON upwork_jobs (published_date)
  WHERE is_favorite = true;

-- 3.2 bot_settings ------------------------------------------------------------
CREATE TABLE bot_settings (
  id                          smallint PRIMARY KEY DEFAULT 1
    CHECK (id = 1),

  is_paused                   boolean  NOT NULL DEFAULT false,
  is_paused_menu              boolean  NOT NULL DEFAULT false,

  pre_screen_threshold        smallint NOT NULL DEFAULT 0
    CHECK (pre_screen_threshold BETWEEN 0 AND 10),
  analysis_threshold          smallint NOT NULL DEFAULT 0
    CHECK (analysis_threshold  BETWEEN 0 AND 10),

  hard_min_client_spent       numeric  NOT NULL DEFAULT 0,
  hard_min_client_rating      numeric  NOT NULL DEFAULT 0
    CHECK (hard_min_client_rating BETWEEN 0 AND 5),
  hard_min_hires_for_rating   smallint NOT NULL DEFAULT 3,
  hard_min_budget_hourly      numeric  NOT NULL DEFAULT 0,
  hard_min_budget_fixed       numeric  NOT NULL DEFAULT 0,
  hard_reject_no_hires        boolean  NOT NULL DEFAULT false,
  hard_max_vacancy_age_h      smallint NOT NULL DEFAULT 0,

  prescreen_model             text     NOT NULL DEFAULT 'xiaomi/mimo-v2-flash',
  analysis_model              text     NOT NULL DEFAULT 'deepseek/deepseek-r1-0528',
  prescreen_fallback_model    text     NOT NULL DEFAULT 'deepseek/deepseek-v4-flash',
  analysis_fallback_model     text     NOT NULL DEFAULT 'minimax/minimax-m2.5',

  loud_notification_threshold smallint NOT NULL DEFAULT 8
    CHECK (loud_notification_threshold BETWEEN 0 AND 10),

  updated_at                  timestamptz NOT NULL DEFAULT now()
);

INSERT INTO bot_settings (id) VALUES (1);

-- 3.3 ai_prompts --------------------------------------------------------------
CREATE TABLE ai_prompts (
  slot       prompt_slot PRIMARY KEY,
  content    text        NOT NULL,
  updated_at timestamptz NOT NULL DEFAULT now()
);

-- 3.4 prompts_history ---------------------------------------------------------
CREATE TABLE prompts_history (
  id             bigserial   PRIMARY KEY,
  slot           prompt_slot NOT NULL,
  content_before text        NOT NULL,
  edited_by      bigint,
  edited_at      timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX prompts_history_slot_idx ON prompts_history (slot, edited_at DESC);

-- 3.5 webhook_inbox -----------------------------------------------------------
CREATE TABLE webhook_inbox (
  request_id    bytea       PRIMARY KEY,
  received_at   timestamptz NOT NULL DEFAULT now(),
  processed_at  timestamptz
);

-- 3.6 secrets -----------------------------------------------------------------
CREATE TABLE secrets (
  name        text         PRIMARY KEY,
  value       text         NOT NULL,
  updated_at  timestamptz  NOT NULL DEFAULT now(),
  updated_by  bigint
);

-- 3.7 normalize_failures ------------------------------------------------------
CREATE TABLE normalize_failures (
  id          bigserial   PRIMARY KEY,
  request_id  bytea       NOT NULL REFERENCES webhook_inbox(request_id) ON DELETE CASCADE,
  raw_payload jsonb       NOT NULL,
  error       varchar(500) NOT NULL,
  ts          timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX normalize_failures_ts_idx ON normalize_failures (ts DESC);

-- 3.8 bot_events --------------------------------------------------------------
CREATE TABLE bot_events (
  id        serial      PRIMARY KEY,
  ts        timestamptz NOT NULL DEFAULT now(),
  level     smallint    NOT NULL,
  event     text        NOT NULL,
  data      jsonb
);

CREATE INDEX bot_events_ts_idx ON bot_events (ts DESC);
