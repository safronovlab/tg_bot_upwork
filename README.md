# AI Lead Filter Bot

**Multi-model AI engine for filtering incoming leads with Telegram delivery.**

Single-user Telegram bot that ingests leads from an external scraper, runs a two-pass LLM analysis (cheap filter then deep analysis), and pushes filtered results into Telegram with a full chat-based UI for queue management and runtime configuration. Built for a Canada-based client, runs on a minimal VPS.

---

## What it does

| Capability | Detail |
|---|---|
| Webhook ingestion | Authenticated `POST /upwork-lead` endpoint with Bearer-token verification, idempotent by `upwork_job_id` |
| Two-pass AI pipeline | Stage 1 — cheap / fast model rough-filters obviously irrelevant items. Stage 2 — premium model deep-analyses only the items that passed (~20-30% of traffic). Token spend cut substantially |
| Telegram delivery | aiogram 3, inline keyboard UI, runtime settings editing without restart |
| IMAP push notifications | Side subsystem for chat-based incident notifications |
| Operational UI | Pause / resume bot, edit prompts, change models, set thresholds, all from inside Telegram |
| Single HTTP stack | aiogram serves built-in `aiohttp.web` for both Telegram polling and `/upwork-lead` webhook. No FastAPI on top |

---

## Performance

```
~50 MB   resident memory in production       MALLOC_ARENA_MAX=2, slotted dataclasses
<50ms    p99 webhook ingestion                asyncpg binary protocol, prepared statements
<30s     p95 end-to-end through LLM stages    two-pass pipeline, OpenRouter
<2s      cold start                           Python 3.12 + uvloop
~80 MB   Docker image size                    multi-stage build with uv
```

VPS spec required: 1 vCPU, 256 MB RAM. Tested in production for months.

---

## Stack

| Layer | Technology | Why |
|---|---|---|
| Runtime | Python 3.12, uvloop | Fast async event loop |
| Bot + HTTP | aiogram 3.x with built-in `aiohttp.web` | One HTTP stack for polling and webhook |
| HTTP client | aiohttp ClientSession (shared) | OpenRouter calls |
| Database | asyncpg, prepared statements, no ORM, pool `min=2 max=10` | Binary protocol, direct SQL |
| Webhook parsing | msgspec.Struct (frozen, gc=False) | 3× faster than Pydantic v2 |
| Internal models | `dataclass(slots=True, frozen=True)` | Minimal per-instance overhead |
| Cron loops | 5 `asyncio.create_task` cycles in the same process | No APScheduler |
| Logging | stdlib `logging` + JSON formatter on `msgspec.json.encode` | No structlog |
| Config | `os.environ` + `dataclass(slots=True, frozen=True)` | No pydantic-settings |
| LLM | OpenRouter (multi-model routing) | Two-pass pipeline with different models per stage |
| Tests | pytest + pytest-asyncio + testcontainers (real Postgres) | No mocks for DB I/O |
| Deploy | Docker multi-stage, Python 3.12-slim, uv | Coolify on VPS |

### Tooling

```
mypy strict · ruff · bandit · radon · vulture · xenon · pytest-cov
```

---

## Pipeline overview

```
External scraper
       │
       ▼
POST /upwork-lead (Bearer-token auth)
       │
       ▼
msgspec parse → UPSERT by upwork_job_id (idempotent)
       │
       ▼
Already analyzed? ─── yes ──► stop
       │
       no
       ▼
Pre-screen prompt → cheap model → rating 0–10
       │
       ▼
Above min-threshold? ─── no ──► drop
       │
       yes
       ▼
Full-analysis prompt → premium model → structured verdict
       │
       ▼
Above paused-min-rating? ─── no ──► hold in queue
       │
       yes
       ▼
Format → Telegram message → user
```

Stage 1 and Stage 2 prompts, models, and rating thresholds are all editable at runtime from inside Telegram. No restart required.

---

## Architecture

```
src/
├── bot/              aiogram handlers, inline keyboards, chat-based settings UI
├── pipeline/         Two-pass orchestration: pre-screen → full analysis → delivery
├── llm/              OpenRouter client + prompt management
├── db/               asyncpg pool, prepared statements, migrations
├── webhook/          aiohttp.web routes mounted on aiogram dispatcher
├── chat/             IMAP push-notification subsystem (optional)
├── config/           dataclass-based settings, env-driven
└── main.py           Single-process composition root

docs/
├── architecture.md   System overview and contracts
├── core_pipeline.md  End-to-end pipeline reference
├── database.md       Schema and migration policy
├── deploy.md         Coolify deployment runbook
└── runbook.md        Operational procedures
```

Tests are contract-first. Every subsystem has a `*.md` specification; tests reference the spec, code is written against it.

---

## Quick start

Requires Python 3.12 and Docker.

### 1. Configure

```bash
cp .env.example .env
# Edit .env: set TELEGRAM_BOT_TOKEN, ALLOWED_USER_IDS, OPENROUTER_API_KEY,
# DATABASE_URL, WEBHOOK_BEARER_TOKEN
```

Generate a strong webhook token:

```bash
openssl rand -hex 32
```

### 2. Run locally

```bash
docker compose up -d
docker compose logs -f bot
curl http://localhost:8080/health   # {"status":"ok","in_flight":0}
```

### 3. Send a test lead

```bash
curl -X POST http://localhost:8080/upwork-lead \
  -H "Authorization: Bearer $WEBHOOK_BEARER_TOKEN" \
  -H "Content-Type: application/json" \
  -d @example/sample-lead.json
```

The bot will run pre-screen, then full analysis, then deliver to the configured Telegram chat.

---

## Production deploy

The repo targets [Coolify](https://coolify.io/) on a VPS, but anything that runs Docker works the same way. Full procedure: [docs/deploy.md](docs/deploy.md).

Highlights:

- Single Telegram consumer: only one process can call `getUpdates` at a time. Stop local instance before promoting to server.
- Secrets are stored in Coolify env-vars, never committed.
- Database is Coolify-managed Postgres 18 in the `coolify` docker network.
- Server hardening: SSH keys only, fail2ban, ufw, AppArmor.
- Operational runbook: [docs/runbook.md](docs/runbook.md).

---

## Testing

```bash
uv sync
uv run pytest -q                       # unit + integration
uv run pytest --cov=src                # coverage
uv run mypy --strict src               # type check
uv run ruff check src                  # lint
uv run bandit -r src                   # security scan
```

Integration tests use `testcontainers` to spin up real Postgres. No mocked database I/O.

---

## Author

Built by Oleg Safronov. Senior Backend, AI Integration.
Portfolio: [github.com/safronovlab](https://github.com/safronovlab)

---

## License

Showcase repository. The codebase is published for portfolio review. No external license is currently issued.
