# LLM.md — LLM через OpenRouter

Описывает [llm.py](llm.py): построение payload, вызов OpenRouter, retry/fallback, семафор concurrency, prompt caching.

Связанные:
- Как вызывается из pipeline: [PIPELINE.md](PIPELINE.md) §4
- Промты в БД: [../DATABASE.md](../DATABASE.md) §3
- Карточка модели в UI: [bot/BOT.md](bot/BOT.md) §7
- Метрики кэш-хитов: [../ARCHITECTURE.md §6](../ARCHITECTURE.md#6-логирование)

---

## 1. Что вводит оператор vs что зашито

| Что | Откуда | Куда сохраняется |
|---|---|---|
| **API-ключ** (`sk-or-v1-...`) | https://openrouter.ai/keys | `secrets` (БД), bootstrap из `OPENROUTER_API_KEY` env |
| **Имя Pre-Screen модели** | https://openrouter.ai/models — slug | `bot_settings.prescreen_model` |
| **Имя Analysis модели** | то же | `bot_settings.analysis_model` |
| **Имя Pre-Screen fallback** | то же | `bot_settings.prescreen_fallback_model` |
| **Имя Analysis fallback** | то же | `bot_settings.analysis_fallback_model` |

| Зашито в код (НЕ редактируется) | Значение |
|---|---|
| Endpoint | `https://openrouter.ai/api/v1/chat/completions` |
| `HTTP-Referer` (опционально) | URL проекта (для leaderboard OpenRouter) |
| `X-Title` (опционально) | `Upwork AI Pipeline` |
| Таймаут pre-screen | 60 сек |
| Таймаут analysis | 120 сек |
| Семафор concurrency | `LLM_CONCURRENCY=5` (env) |

---

## 2. Полный код вызова

**Ключевое:** стабильный template из `ai_prompts` идёт в **system message**, уникальные данные вакансии — в **user message**. Это максимизирует кэш-хит DeepSeek/Claude/Gemini (см. §3).

**Pre-screen видит обогащённый набор (14 полей)** для лучших решений при минимальном росте токенов:

```python
def build_prescreen_payload(job) -> str:
    parts = [
        f"[название вакансии]\n{job.job_title or ''}",
        f"[описание вакансии]\n{job.job_description or ''}",
        f"[вопросы клиента]\n{job.questions or 'нет'}",
        f"[тип бюджета]\n{job.budget_type or ''}",
        f"[сумма бюджета]\n{job.budget or ''}",
        f"[тип занятости]\n{job.job_type or ''}",
        f"[категория клиента (rank)]\n{job.client_rank or ''}",
        f"[страна клиента]\n{job.client_country or ''}",
        f"[рейтинг клиента]\n{job.client_rating or 'нет'}",
        f"[всего потрачено клиентом, $]\n{job.client_total_spent or 0}",
        f"[количество наймов]\n{job.client_total_hires or 0}",
        f"[средняя ставка которую платит клиент, $/час]\n{job.client_avg_rate or 'нет'}",
        f"[год регистрации клиента на Upwork]\n"
            f"{job.client_registered_at.year if job.client_registered_at else 'нет'}",
    ]
    return "\n\n".join(parts)


def build_analysis_payload(job) -> str:
    """15 полей = pre-screen 14 + полные client_reviews."""
    parts = [
        # ... все поля из pre-screen ...
        f"[дата публикации вакансии]\n{job.published_date or 'нет'}",
        f"[отзывы о клиенте от других фрилансеров]\n{job.client_reviews or 'нет'}",
    ]
    return "\n\n".join(parts)
```

```python
# src/llm.py
import aiohttp, asyncio, logging
from src import db, log

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
HTTP_REFERER   = "https://github.com/myrepo/upwork-bot"
X_TITLE        = "Upwork AI Pipeline"

llm_sem = asyncio.Semaphore(5)

def _build_messages(template: str, job_payload: str, model: str) -> list[dict]:
    """Стабильный template — в system, уникальное — в user. Максимум кэш-хитов."""
    if model.startswith("anthropic/"):
        return [
            {"role": "system",
             "content": [{"type": "text", "text": template,
                          "cache_control": {"type": "ephemeral"}}]},
            {"role": "user", "content": job_payload},
        ]
    return [
        {"role": "system", "content": template},
        {"role": "user",   "content": job_payload},
    ]


async def _call(session, api_key, model, template, job_payload, timeout_s):
    headers = {
        "Authorization": f"Bearer {api_key}",
        "HTTP-Referer":  HTTP_REFERER,
        "X-Title":       X_TITLE,
        "Content-Type":  "application/json",
    }
    body = {
        "model": model,
        "messages": _build_messages(template, job_payload, model),
        "temperature": 0.3,                                     # стабильность оценок
    }
    try:
        async with session.post(
            OPENROUTER_URL, headers=headers, json=body,
            timeout=aiohttp.ClientTimeout(total=timeout_s),
        ) as resp:
            if resp.status >= 400:
                err_text = (await resp.text())[:200]
                await log.emit("openrouter_http_error", level=logging.WARNING,
                               status=resp.status, model=model, body=err_text)
                return None
            data = await resp.json()
            usage = data.get("usage", {})
            await log.emit("llm_call",
                           model=model,
                           tokens_in=usage.get("prompt_tokens"),
                           tokens_cached=usage.get("prompt_cache_hit_tokens", 0),
                           tokens_out=usage.get("completion_tokens"))
            return data["choices"][0]["message"]["content"]
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        await log.emit("openrouter_exception", level=logging.WARNING,
                       model=model, err=str(e))
        return None


async def _with_fallback(session, api_key, primary, fallback,
                         template, payload, timeout_s):
    async with llm_sem:
        result = await _call(session, api_key, primary, template, payload, timeout_s)
        if result:
            return result
        await log.emit("llm_fallback", level=logging.WARNING,
                       from_model=primary, to_model=fallback)
        return await _call(session, api_key, fallback, template, payload, timeout_s)


async def pre_screen(session, job) -> str | None:
    s        = await db.get_settings_cached()
    key      = await db.get_openrouter_key()
    template = await db.get_prompt_cached("pre_screen")
    payload  = build_prescreen_payload(job)
    return await _with_fallback(session, key,
                                s.prescreen_model, s.prescreen_fallback_model,
                                template, payload, timeout_s=60)


async def analyze(session, job) -> str | None:
    s        = await db.get_settings_cached()
    key      = await db.get_openrouter_key()
    template = await db.get_prompt_cached("analysis")
    payload  = build_analysis_payload(job)
    return await _with_fallback(session, key,
                                s.analysis_model, s.analysis_fallback_model,
                                template, payload, timeout_s=120)


async def validate_model(session, api_key, model) -> tuple[bool, str]:
    """Опциональная канарейка при сохранении новой модели через бота."""
    body = {"model": model, "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 5}
    headers = {"Authorization": f"Bearer {api_key}"}
    async with session.post(OPENROUTER_URL, headers=headers, json=body,
                            timeout=aiohttp.ClientTimeout(total=10)) as r:
        if r.status == 200: return True, "ok"
        if r.status == 401: return False, "API ключ недействителен"
        if r.status == 404: return False, "Модель не найдена на OpenRouter"
        return False, f"HTTP {r.status}"
```

**Намеренно НЕ ретраим** один и тот же вызов на одной модели. Если упало 1 раз — проблема устойчива, второй вызов с тем же — пустая трата денег. Идём в fallback.

---

## 3. Prompt caching

Кэширование работает **прозрачно для DeepSeek** (наш дефолт). Условие — стабильный префикс между запросами. Это обеспечивается разделением system (template из БД, идентичен) и user (уникальные данные вакансии).

| Провайдер | Как работает | Что нужно |
|---|---|---|
| DeepSeek (`deepseek/*`) | автоматически, TTL ~часы | ничего |
| Anthropic (`anthropic/*`) | явный `cache_control` | добавлено в `_build_messages` |
| Google Gemini (`google/*`) | implicit, от 1024 токенов | работает само |
| OpenAI o1/o3 | автоматически | работает само |

Метрики кэш-хитов (DeepSeek возвращает `usage.prompt_cache_hit_tokens`) логируются в событии `llm_call` — оператор видит в `/Логи`:
```
2026-05-02 10:15  INFO  llm_call  model=deepseek/r1 tokens_in=1100 tokens_cached=1000 tokens_out=400
```
1000 из 1100 input-токенов из кэша = ~90% хит-рейт.
