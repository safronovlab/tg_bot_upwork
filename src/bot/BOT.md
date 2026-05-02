# BOT.md — Telegram бот (UI)

Описывает [app.py](app.py), [auth.py](auth.py), [states.py](states.py), [keyboards.py](keyboards.py), [formatters.py](formatters.py) и все [handlers/](handlers/).

Связанные:
- Pipeline (как pause влияет): [../PIPELINE.md](../PIPELINE.md) §4
- LLM (карточка модели читает): [../LLM.md](../LLM.md)
- Логи (источник «Последние изменения»): [../../ARCHITECTURE.md §6](../../ARCHITECTURE.md#6-логирование)
- Конфиг (auth middleware): [../../ARCHITECTURE.md §4](../../ARCHITECTURE.md#4-конфигурация-и-секреты)
- Notifier (отправка карточки вакансии): [../notifier.py](../notifier.py)

---

## 1. Главное меню

Подсказка при `/start`:
```
Радар Upwork-вакансий. Жми кнопку.
```

```
[ Запустить / Остановить ]   [ Отчёт (3) ]
[ Избранное (12) ]           [ Настройки ]
[          Синхронизация              ]   ← на всю ширину
```

Кнопка `Запустить` показывается если `is_paused = true`, иначе `Остановить`.

**Счётчики только у `Отчёт` и `Избранное`** — две кнопки где знание «есть что смотреть или нет» реально экономит клик. Если значение `0` — скобки не показываются (просто `Отчёт`). У `Синхронизация` счётчик не показываем — её жмут реже.

```python
async def main_menu_kb(pool, is_paused: bool) -> ReplyKeyboardMarkup:
    pause_btn = "Запустить" if is_paused else "Остановить"

    # Кэш на 10 сек чтобы не хитать БД на каждый рендер
    n_report = await db.count_queued_by_reason_cached(pool, "manual")
    n_favs   = await db.count_favorites_cached(pool)

    report_label = f"Отчёт ({n_report})" if n_report else "Отчёт"
    favs_label   = f"Избранное ({n_favs})" if n_favs else "Избранное"

    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=pause_btn),  KeyboardButton(text=report_label)],
            [KeyboardButton(text=favs_label), KeyboardButton(text="Настройки")],
            [KeyboardButton(text="Синхронизация")],
        ],
        resize_keyboard=True, is_persistent=True,
    )
```

**Хендлеры ловят кнопки по prefix-match** — счётчик в скобках не должен ломать маршрутизацию:
```python
@router.message(F.text.startswith("Отчёт"))
async def handle_report_btn(message): ...

@router.message(F.text.startswith("Избранное"))
async def handle_favorites_btn(message): ...
```

---

## 2. Авто-пауза в блокирующих меню

Меню `Избранное`, `Отчёт`, `Настройки` (и любые подменю) — **блокирующие**: пока юзер в них, в чат не должны валиться real-time вакансии (это спамило бы поверх списков).

| Действие | `is_paused_menu` |
|---|---|
| Войти в `Избранное` / `Отчёт` / `Настройки` (и подменю) | → `true` |
| `/start`, `Назад` (в любом меню), `Синхронизация` | → `false` |

Pipeline проверяет **обе паузы** (см. [../PIPELINE.md](../PIPELINE.md) §4):
- При активной любой паузе вакансия копится с `queued_reason='manual'` (приоритет, если ручная) или `'menu'`
- `is_paused` (ручная) сохраняется между сессиями
- `is_paused_menu` сбрасывается на каждый выход

---

## 3. Меню «Настройки» (3 уровня вложенности)

**Уровень 1 — основное меню настроек.** Подсказка: `Настройки бота. Что меняем?`

```
[ Изменить промт         ]   → §3.1
[ Основные модели        ]   → §3.2
[ Фолбэк модели          ]   → §3.3
[ Пороги                 ]   → §3.4
[ API ключ OpenRouter    ]   → §5
[ Логи                   ]   → §11
[ Очистить БД            ]   → §12
[ Назад                  ]   → главное меню
```

Все кнопки full-width. `Назад` всегда последняя — единая точка возврата.

```python
def settings_menu_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Изменить промт")],
            [KeyboardButton(text="Основные модели")],
            [KeyboardButton(text="Фолбэк модели")],
            [KeyboardButton(text="Пороги")],
            [KeyboardButton(text="API ключ OpenRouter")],
            [KeyboardButton(text="Логи")],
            [KeyboardButton(text="Очистить БД")],
            [KeyboardButton(text="Назад")],
        ],
        resize_keyboard=True, is_persistent=True,
    )
```

### 3.1 Подменю «Изменить промт»

Подсказка: `Тексты для LLM. Каждая правка сохраняется в историю.`

```
[ Промпт: Pre-Screen ]   [ Промпт: Анализ ]
[ Промпт: Cover ]        [ Промпт: Ночной отчёт ]
[ В настройки ]
```

Каждая кнопка → карточка промта (см. §6) → `Изменить` → универсальный flow (см. §4).

### 3.2 Подменю «Основные модели»

Подсказка: `Pre-Screen — быстрая модель отсева. Анализ — главная.`

```
[ Pre-Screen модель      ]   → карточка модели §7
[ Анализ модель          ]   → карточка модели §7
[ Назад                  ]   → меню настроек
```

### 3.3 Подменю «Фолбэк модели»

Подсказка: `Резервные модели — включаются если основная упала.`

```
[ Pre-Screen фолбэк      ]   → карточка модели §7
[ Анализ фолбэк          ]   → карточка модели §7
[ Назад                  ]   → меню настроек
```

### 3.4 Подменю «Пороги»

Подсказка: `Пороги фильтрации. 0 = фильтр выключен.`

```
[ Пресеты                     ]   → §3.7
[ Pre-Screen порог            ]   → карточка §3.5
[ Анализ порог                ]   → карточка §3.5
[ Громкость уведомления       ]   → карточка §3.5
[ Минимум потрачено клиентом  ]   → карточка §3.5
[ Минимум рейтинг клиента     ]   → карточка §3.5
[ Минимум наймов для рейтинга ]   → карточка §3.5
[ Минимум Hourly бюджет       ]   → карточка §3.5
[ Минимум Fixed бюджет        ]   → карточка §3.5
[ Отсекать клиентов без наймов]   → toggle §3.6
[ Максимум возраст вакансии   ]   → карточка §3.5
[ Назад                       ]   → меню настроек
```

`Пресеты` сверху чтобы быстро сбросить всё на дефолт. После применения пресета можно дальше править отдельные пороги вручную через карточки — пресет не блокирует ручное редактирование.

### 3.5 Карточка одного порога

Все числовые пороги имеют одну форму карточки. Подробное объяснение **что значит этот порог** + текущее значение + рекомендация + блок «Последние изменения» (см. §8).

Пример карточки `Минимум потрачено клиентом`:
```
Минимум потрачено клиентом

Текущее значение: 0 (фильтр выключен)
Поле в БД: hard_min_client_spent

Что это: вакансии от клиентов потративших меньше указанной
суммы (в долларах) автоматически отбрасываются ДО любого LLM-вызова.
Цель — экономить токены на заведомо мусорных лидах от новичков.

Рекомендация: 50 (отсекает совсем новых клиентов).
Чтобы выключить — введите 0.

Последние изменения:
  2026-05-02 14:30   было 0 → стало 50  (вручную)
  2026-05-01 10:15   было 0 → стало 0   (preset_zeros)
```

```
[ Изменить значение     ]
[ Назад                 ]   → подменю Пороги
```

`Изменить значение` → переход в `ThresholdEdit.waiting_value` → универсальный flow §4 (отправь число → preview → `Сохранить` → `Сохранено.` → возврат на эту карточку с новым значением).

**Текст для каждого порога** (хранится в коде, не в БД — статичные):

| Порог | Что это | Рекомендация |
|---|---|---|
| `Pre-Screen порог` | Минимальный pre-screen рейтинг (0-10) чтобы вакансия прошла к полному анализу. Ниже — DELETE из БД. 0 = все проходят. | 5 для нормальной фильтрации, 0 если хочется видеть всё |
| `Анализ порог` | Минимальный финальный рейтинг (0-10) чтобы вакансия пришла в Telegram. Ниже — DELETE из БД полностью. 0 = все доходят. | 5 для нормальной фильтрации, 7+ только топ |
| `Громкость уведомления` | Порог рейтинга для громкого уведомления (0-10). Вакансии с рейтингом >= порога приходят со звуком, ниже — беззвучно. | 8 (только топовые звонят), 0 (всё со звуком), 11 (всё беззвучно) |
| `Минимум потрачено клиентом` | Hard-фильтр: клиенты с тратой меньше $X отбрасываются ДО LLM. 0 = выключено. | 50 (отсекает новичков) |
| `Минимум рейтинг клиента` | Hard-фильтр: клиенты с рейтингом ниже X.Y (0-5) отбрасываются. Применяется только если у клиента >= N наймов. 0 = выключено. | 4.0 |
| `Минимум наймов для рейтинга` | Сколько наймов должно быть у клиента, чтобы рейтингу можно было верить. С маленькой выборкой 5★ ничего не значит. | 3 (фундаментально) |
| `Минимум Hourly бюджет` | Hard-фильтр: hourly-вакансии где верхний бюджет < $X/час отбрасываются. 0 = выключено. | 10 |
| `Минимум Fixed бюджет` | Hard-фильтр: fixed-вакансии с бюджетом < $X отбрасываются. 0 = выключено. | 100 |
| `Максимум возраст вакансии` | Hard-фильтр: вакансии старше N часов отбрасываются. 0 = любой возраст ок. | 24 |

### 3.6 Карточка булева переключателя `Отсекать клиентов без наймов`

Один из порогов — булев. Карточка с двумя кнопками вместо ввода:

```
Отсекать клиентов без наймов

Текущее значение: ВЫКЛЮЧЕНО
Поле в БД: hard_reject_no_hires

Что это: если включено — вакансии от клиентов с 0 наймов
автоматически отбрасываются ДО LLM. Полезно когда устали
от ноунейм-клиентов, готовы пропускать иногда хорошие
первые проекты ради экономии времени.

Последние изменения:
  2026-04-28 08:10   было ВКЛ → стало ВЫКЛ  (вручную)
```

```
[ Включить ] / [ Выключить ]   ← одна кнопка, зависит от текущего значения
[ Назад                    ]
```

Нажатие сразу переключает значение, отвечает `Сохранено.`, обновляет карточку.

### 3.7 Подменю «Пресеты»

Подсказка: `Готовые наборы порогов одной кнопкой. Текущие значения перезапишутся.`

```
[ Все нули (нет фильтрации)   ]
[ Стандарт                    ]
[ Строгий                     ]
[ Назад                       ]
```

| Пресет | Значения | Когда |
|---|---|---|
| **Все нули** | Все пороги (включая Pre-Screen, Analysis, Громкость и hard-фильтры) → `0`. Toggle «без наймов» → выкл. | Тестовый режим: видеть всё. Default состояние при свежем деплое |
| **Стандарт** | Pre=5, Analysis=5, Громкость=8, min_spent=$50, min_rating=4.0, min_hourly=$10, min_fixed=$100, max_age=0, no_hires=off | Балансированный режим |
| **Строгий** | Pre=7, Analysis=7, Громкость=9, min_spent=$200, min_rating=4.5, min_hourly=$25, min_fixed=$500, max_age=24, no_hires=on | Только высокое качество |

После нажатия — подтверждение со списком всех значений которые будут установлены, кнопки `Да, применить` / `Нет`. После применения — один UPDATE bot_settings + диффовое логирование `threshold_updated` для каждого изменённого поля + событие `preset_applied`.

```python
PRESETS = {
    "zeros": {
        "pre_screen_threshold": 0, "analysis_threshold": 0,
        "loud_notification_threshold": 0,
        "hard_min_client_spent": 0, "hard_min_client_rating": 0,
        "hard_min_budget_hourly": 0, "hard_min_budget_fixed": 0,
        "hard_reject_no_hires": False, "hard_max_vacancy_age_h": 0,
    },
    "standard": {
        "pre_screen_threshold": 5, "analysis_threshold": 5,
        "loud_notification_threshold": 8,
        "hard_min_client_spent": 50, "hard_min_client_rating": 4.0,
        "hard_min_budget_hourly": 10, "hard_min_budget_fixed": 100,
        "hard_reject_no_hires": False, "hard_max_vacancy_age_h": 0,
    },
    "strict": {
        "pre_screen_threshold": 7, "analysis_threshold": 7,
        "loud_notification_threshold": 9,
        "hard_min_client_spent": 200, "hard_min_client_rating": 4.5,
        "hard_min_budget_hourly": 25, "hard_min_budget_fixed": 500,
        "hard_reject_no_hires": True, "hard_max_vacancy_age_h": 24,
    },
}

async def apply_preset(pool, name: str, user_id: int):
    new = PRESETS[name]
    old = await db.get_settings_full(pool)
    sets = ", ".join(f"{k} = ${i+2}" for i, k in enumerate(new))
    await pool.execute(
        f"UPDATE bot_settings SET {sets}, updated_at = now() WHERE id = 1",
        *new.values()
    )
    await db.invalidate_settings_cache()
    for field, new_val in new.items():
        old_val = getattr(old, field)
        if old_val != new_val:
            await log.emit("threshold_updated",
                           field=field, old_value=str(old_val), new_value=str(new_val),
                           via=f"preset_{name}", updated_by=user_id)
    await log.emit("preset_applied", preset=name, updated_by=user_id)
```

---

## 4. Универсальный FSM-паттерн ввода с подтверждением

Все настройки требующие ввода значения (промт, ключ, модель, числовой порог) используют **один паттерн**: ввод в буфер → явное `Сохранить` → подтверждение `Сохранено.`. Однообразие важнее микро-удобства «без лишних кликов».

```python
class PromptEdit(StatesGroup):
    waiting_text = State()        # промт, буфер копится в context['buf']

class ApiKeyEdit(StatesGroup):
    waiting_key = State()         # API-ключ, последнее сообщение → context['buf']

class ModelEdit(StatesGroup):
    waiting_name = State()        # модель, role в context

class ThresholdEdit(StatesGroup):
    waiting_value = State()       # порог, field/type в context

class CleanupConfirm(StatesGroup):
    waiting = State()             # Да/Нет на очистку
```

**Универсальный flow:**

| Шаг | Что делает |
|---|---|
| 1 | Карточка → `Изменить` → переход в FSM |
| 2 | Бот: «Изменение: `<название>`. Текущее: `<value>`. Отправь новое значение.» Reply-клавиатура: только `Назад` |
| 3 | Оператор присылает значение одним или несколькими сообщениями |
| 4 | После каждого сообщения бот отвечает: «Получено: `<preview>`. Жми `Сохранить` или отправь ещё.» Reply-клавиатура: `Сохранить` + `Назад` |
| 5а | Жмёт `Сохранить` → валидация. Если плохо → «<причина>. Попробуй ещё раз.», остаёмся в state |
| 5б | Если ок — UPDATE в БД, инвалидация кеша, событие в `bot_events`, ответ **«Сохранено.»**, возврат на карточку с новым значением |
| 6 | Жмёт `Назад` или `/cancel` → буфер выкидывается, ничего не меняется |

```python
EDIT_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="Сохранить")],
        [KeyboardButton(text="Назад")],
    ],
    resize_keyboard=True, is_persistent=True,
)

CANCEL_ONLY_KB = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="Назад")]],
    resize_keyboard=True, is_persistent=True,
)
```

**Поведение для разных типов:**

| Тип значения | Логика буфера | Особенности |
|---|---|---|
| Промт (длинный текст) | накопительный — каждое сообщение `append` | поддержка `.txt` файла |
| API ключ | замещающий — последнее сообщение перезаписывает | preview редактируется (`sk-or-…a8f2`); сообщения пользователя удаляются при `Сохранить` |
| Имя модели | замещающий | preview полный |
| Числовой порог | замещающий | preview как есть |

```python
@router.message(StateFilter(PromptEdit, ApiKeyEdit, ModelEdit, ThresholdEdit),
                F.text, ~F.text.in_({"Сохранить", "Назад"}))
async def buffer_input(message, state: FSMContext):
    current_state = await state.get_state()
    data = await state.get_data()

    if current_state == PromptEdit.waiting_text.state:
        buf = data.get("buf", "") + message.text                # накопительный
        preview = f"{len(buf)} символов"
    else:
        buf = message.text.strip()                              # замещающий
        preview = redact_for_preview(buf, current_state)

    await state.update_data(buf=buf)
    await message.answer(f"Получено: {preview}. Жми Сохранить или отправь ещё.",
                         reply_markup=EDIT_KB)


@router.message(StateFilter(PromptEdit, ApiKeyEdit, ModelEdit, ThresholdEdit),
                F.text == "Назад")
async def universal_cancel(message, state: FSMContext):
    parent = (await state.get_data()).get("parent_card")
    await state.clear()
    await message.answer("Отменено.")
    await reopen_card(message, parent)


def redact_for_preview(value: str, state: str) -> str:
    """Превью для подтверждения. Для ключей — только последние 4 символа."""
    if state == ApiKeyEdit.waiting_key.state:
        return f"{value[:6]}…{value[-4:]} ({len(value)} символов)" if len(value) > 10 else "<скрыто>"
    if len(value) > 60:
        return value[:60] + "…"
    return value
```

**Специфические `Сохранить` хендлеры** (по одному на каждый state, валидация + запись):

```python
@router.message(PromptEdit.waiting_text, F.text == "Сохранить")
async def save_prompt(message, state):
    data = await state.get_data()
    buf  = data.get("buf", "")
    slot = data["slot"]
    if not (50 <= len(buf) <= 50000):
        await message.answer(f"Длина {len(buf)} вне диапазона 50..50000. Попробуй ещё.")
        return
    old = await db.get_prompt(slot)
    await db.insert_prompt_history(slot, old, message.from_user.id)
    await db.update_prompt(slot, buf)
    await db.invalidate_prompt_cache(slot)
    await log.emit("prompt_updated", field=slot,
                   old_length=len(old), new_length=len(buf),
                   updated_by=message.from_user.id)
    await state.clear()
    await message.answer("Сохранено.")
    await show_prompt_card(message, slot)


@router.message(ApiKeyEdit.waiting_key, F.text == "Сохранить")
async def save_api_key(message, state, bot):
    data = await state.get_data()
    buf = data.get("buf", "")
    if not (10 <= len(buf) <= 200) or not buf.isascii() or not buf.isprintable():
        await message.answer("Ключ выглядит некорректно. Попробуй ещё.")
        return
    await db.set_secret("openrouter_api_key", buf, message.from_user.id)
    await db.invalidate_secrets_cache()
    await log.emit("key_updated", field="openrouter_api_key",
                   updated_by=message.from_user.id)        # значение НЕ логируется!
    await delete_user_messages_in_state(message, bot)      # удаляем фрагменты ключа из чата
    await state.clear()
    await message.answer("Сохранено.")
    await show_api_key_card(message)


@router.message(ModelEdit.waiting_name, F.text == "Сохранить")
async def save_model(message, state):
    data = await state.get_data()
    buf  = data.get("buf", "").strip()
    role = data["role"]                                    # 'prescreen' | 'analysis' | '*_fallback'
    if not re.match(r"^[a-z0-9._\-]+/[a-z0-9._\-]+(:[a-z0-9._\-]+)?$", buf) \
       or not (3 <= len(buf) <= 100):
        await message.answer("Формат vendor/model-name (3..100 символов). Попробуй ещё.")
        return
    column = ROLE_TO_COLUMN[role]
    old = await db.get_model(column)
    await db.set_model(column, buf)
    await db.invalidate_settings_cache()
    await log.emit("model_updated", field=column, old_value=old, new_value=buf,
                   via="manual", updated_by=message.from_user.id)
    await state.clear()
    await message.answer("Сохранено.")
    await show_model_card(message, role)


@router.message(ThresholdEdit.waiting_value, F.text == "Сохранить")
async def save_threshold(message, state):
    data  = await state.get_data()
    buf   = data.get("buf", "").strip()
    field = data["field"]
    spec  = THRESHOLD_SPECS[field]                         # {'type': 'int'|'float', 'min':..., 'max':...}

    try:
        val = int(buf) if spec["type"] == "int" else float(buf.replace(",", "."))
    except ValueError:
        await message.answer(f"Ожидаю {spec['type_human']}. Попробуй ещё.")
        return
    if not (spec["min"] <= val <= spec["max"]):
        await message.answer(f"Диапазон {spec['min']}..{spec['max']}. Попробуй ещё.")
        return

    old = await db.get_setting(field)
    await db.set_setting(field, val)
    await db.invalidate_settings_cache()
    await log.emit("threshold_updated", field=field,
                   old_value=str(old), new_value=str(val), via="manual",
                   updated_by=message.from_user.id)
    await state.clear()
    await message.answer("Сохранено.")
    await show_threshold_card(message, field)
```

**Toggle-переключатели** (булевы пороги, см. §3.6) **не используют** этот flow — нажатие сразу применяется и шлёт `Сохранено.`

---

## 5. Карточка API-ключа

```
API ключ OpenRouter

Текущий ключ: sk-or-v1-…a8f2 (последние 4 символа)
Источник: secrets (БД)
Обновлён: 2026-04-12 14:30 by @user

Получить ключ: https://openrouter.ai/keys
Формат: sk-or-v1-...

Последние изменения:
  2026-04-12 14:30   обновлён by @user
  2026-03-20 09:00   обновлён by @user
```

```
[ Изменить ]
[ Назад    ]
```

`Изменить` → `ApiKeyEdit.waiting_key` → универсальный flow §4.

**Безопасность:**
- В preview только `sk-or-v1-…a8f2 (50 символов)`, не весь ключ
- При успешном `Сохранить` все сообщения пользователя со state-сессии удаляются из чата
- Значение никогда не попадает в `bot_events.data` или structlog

---

## 6. Карточка промта

При нажатии любого слота из подменю «Изменить промт» (см. §3.1) — карточка:
```
Промпт: Анализ

Текущая длина: 3450 символов
Слот: analysis
Обновлён: 2026-04-12 14:30 by @user

[первые 500 символов содержимого для контекста]
…

Последние изменения:
  2026-04-12 14:30   обновлён (длина 3200 → 3450)
  2026-04-08 10:00   обновлён (длина 2900 → 3200)
```

```
[ Изменить ]
[ Назад    ]
```

`Изменить` → `PromptEdit.waiting_text` → универсальный flow §4 с накопительным буфером.

### 6.1 Загрузка промта файлом

Длинные промты можно отправить `.txt` файлом вместо набора в чате — удобно когда промт > 8000 символов или хочется править в IDE с подсветкой:

```python
@router.message(PromptEdit.waiting_text, F.document)
async def upload_prompt_file(message, state, bot):
    if not message.document.file_name.endswith('.txt'):
        await message.answer("Принимаю только .txt файлы.")
        return
    file = await bot.download(message.document)
    text = file.read().decode('utf-8', errors='replace')
    await state.update_data(buf=text)
    await message.answer(f"Файл загружен: {len(text)} символов. Жми Сохранить.",
                         reply_markup=EDIT_KB)
```

---

## 7. Карточка одной модели (общая для всех 4)

При нажатии любой из 4 кнопок (`Pre-Screen модель`, `Анализ модель`, `Pre-Screen фолбэк`, `Анализ фолбэк`) — карточка:

```
Pre-Screen модель

Текущее значение: xiaomi/mimo-v2-flash
Источник: bot_settings (БД)
Обновлено: 2026-04-12 14:30

Список доступных моделей: https://openrouter.ai/models

Последние изменения:
  2026-04-12 14:30   было deepseek/v3-flash → стало xiaomi/mimo-v2-flash
  2026-04-08 10:00   было gemini/flash → стало deepseek/v3-flash
```

```
[ Изменить модель ]
[ Назад           ]
```

`Изменить` → `ModelEdit.waiting_name` → универсальный flow §4.

```python
ROLE_TO_COLUMN = {
    "prescreen":          "prescreen_model",
    "analysis":           "analysis_model",
    "prescreen_fallback": "prescreen_fallback_model",
    "analysis_fallback":  "analysis_fallback_model",
}
```

Опциональная **канарейка** перед сохранением — `validate_model()` (см. [../LLM.md](../LLM.md)) шлёт `prompt='ping', max_tokens=5`. Если 401 → «API ключ недействителен», 404 → «Модель не найдена», 200 → ОК. Защита от опечаток `deekseep/deepseek-r1`.

---

## 8. Блок «Последние изменения» в карточках (общий)

Все карточки настроек (порог, модель, ключ, промт) показывают **последние 3 изменения** этого поля внизу — без отдельной кнопки.

Источник — `bot_events` отфильтрованные по событию и полю:
```sql
SELECT ts, data
FROM bot_events
WHERE event = $1                                  -- 'threshold_updated', 'model_updated', etc
  AND data->>'field' = $2                         -- имя поля (например 'prescreen_model')
ORDER BY ts DESC
LIMIT 3;
```

**Формат отображения:**

| Тип поля | Что показываем |
|---|---|
| Порог числовой | `было 0 → стало 50  (вручную)` или `(preset_strict)` |
| Порог булев | `было ВЫКЛ → стало ВКЛ` |
| Модель | `было xiaomi/mimo-v2 → стало deepseek/v4-flash` |
| Промт | `обновлён (длина 3200 → 3450)` — содержимое не показываем |
| Ключ | `обновлён by @user` — значение НЕ показываем (security) |

Каждое событие смены настройки кладёт в `data` поля `field`, `old_value`, `new_value`, и опционально `via` (`manual` / `preset_<name>`):

```python
await emit("threshold_updated",
           field="hard_min_client_spent",
           old_value=str(old), new_value=str(new),
           via="manual",
           updated_by=user_id)
```

Для секретов (`key_updated`) — `old_value` и `new_value` НЕ записываем, только `updated_by`.

Если истории нет (свежий деплой) — блок просто не показывается.

---

## 9. Карточка вакансии и inline-кнопки

Сообщение в Telegram при доставке вакансии:
```
{ai_analysis от R1, отформатированный}

{JOB_TITLE.toUpperCase()}

[ Открыть на Upwork ]    ← URL button
[ Избранное ]            ← callback save_<upwork_job_id>
```

| Лейбл | Тип | Callback / URL | Действие |
|---|---|---|---|
| `Открыть на Upwork` | URL | `upwork_url` | открывает страницу вакансии в браузере |
| `Избранное` | callback | `save_<upwork_job_id>` | `is_favorite = true` + ответ «Добавлено в избранное.» |

В списке избранного на каждой записи:
| Лейбл | Callback | Действие |
|---|---|---|
| `Анализ` | `analysis_<id>` | показать сохранённый `ai_analysis` |
| `Карточка` | `card_<id>` | показать краткую карточку (title + url) |
| `Удалить из избранного` | `del_<id>` | `is_favorite = false`, удалить сообщение |

**Звуковые уведомления** — настраиваются через порог `Громкость уведомления`:
- `rating >= порога` → отправка с `disable_notification=False` (со звуком)
- `rating < порога` → отправка с `disable_notification=True` (беззвучно)

Ограничение Telegram Bot API: **только два уровня (звук / беззвучно)**. Кастомные звуки на каждый рейтинг невозможны.

```python
async def send_job(bot, chat_id, job, analysis, *, silent: bool):
    await bot.send_message(
        chat_id=chat_id,
        text=format_job(job, analysis),
        reply_markup=card_buttons(job),
        disable_notification=silent,
    )
```

---

## 10. «Отчёт» vs «Синхронизация»

Две независимых очереди по полю `queued_reason` с **разным UX**:

| Кнопка | Что выгружает | UX |
|---|---|---|
| **Отчёт** | `queued_reason='manual'` — копились пока бот был на ручной паузе | Сначала **сводка**, потом выбор: всё / топ-5 / очистить |
| **Синхронизация** | `queued_reason='menu'` — копились пока оператор был в меню | **Сразу выгружает всё**, без сводки |

### 10.1 Синхронизация — мгновенная выгрузка

Простой drain: SELECT накопившегося, отправка по одному.

```python
async def handle_sync(message):
    rows = await db.drain_queued_by_reason('menu')   # UPDATE+RETURNING сразу
    if not rows:
        await message.answer("Новых вакансий нет.")
        return
    for row in rows:
        await notifier.send_job_from_row(row)
        await asyncio.sleep(0.05)
    await db.set_paused_menu(False)
```

### 10.2 Отчёт — сводка перед выгрузкой

Защита от спама когда за паузу накопилось много.

```python
async def handle_report(message):
    rows = await db.peek_queued_by_reason('manual')   # SELECT, без UPDATE
    if not rows:
        await message.answer("Новых вакансий нет.")
        return
    await show_report_digest(message, rows)
```

Дайджест-сообщение:
```
В очереди 12 вакансий (накопились за паузу):

10  Senior FastAPI dev for SaaS         (US, $5K)
9   Python data pipeline migration       (UK, $2K)
9   Async web scraper consultation       (DE, hourly)
8   Refactor Django to FastAPI           (CA, $3K)
8   Selenium automation for QA team      (US, hourly)
7   Webhook integration with Stripe      (AU, $800)
... (ещё 6 с рейтингом 5-7)

[ Выгрузить все 12   ]
[ Выгрузить топ-5    ]
[ Очистить очередь   ]
[ Назад              ]
```

```python
async def show_report_digest(message, rows):
    text = f"В очереди {len(rows)} вакансий (накопились за паузу):\n\n"
    for r in sorted(rows, key=lambda x: -x['rating'])[:10]:
        text += (f"{r['rating']:>2}  {r['job_title'][:40]:<40}  "
                 f"({r['client_country'] or '?'}, {r['budget'] or '?'})\n")
    if len(rows) > 10:
        text += f"... (ещё {len(rows)-10} с рейтингом ниже)"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"Выгрузить все {len(rows)}", callback_data="rep:all")],
        [InlineKeyboardButton(text="Выгрузить топ-5",            callback_data="rep:top5")],
        [InlineKeyboardButton(text="Очистить очередь",           callback_data="rep:clear")],
        [InlineKeyboardButton(text="Назад",                       callback_data="rep:close")],
    ])
    await message.answer(text, reply_markup=kb)


@router.callback_query(F.data.startswith("rep:"))
async def handle_report_action(callback):
    action = callback.data.split(":")[1]
    if action == "close":
        await callback.message.delete()
        return
    if action == "all":
        rows = await db.drain_queued_by_reason("manual")
    elif action == "top5":
        rows = await db.drain_queued_by_reason("manual", limit=5, order_by_rating=True)
    elif action == "clear":
        n = await db.mark_queued_as_sent("manual")
        await callback.message.edit_text(f"Очищено {n} вакансий.")
        await db.set_paused_menu(False)
        return

    await callback.message.delete()
    for row in rows:
        await notifier.send_job_from_row(row)
        await asyncio.sleep(0.05)
    await db.set_paused_menu(False)
```

**Приоритет при двойной паузе:** если активны обе (`is_paused=true AND is_paused_menu=true`) — `queued_reason='manual'`. Ручная пауза «сильнее».

**SQL-методы** (общие для Отчёта и Синхронизации):

```sql
-- peek: SELECT для дайджеста, БЕЗ UPDATE (только Отчёт)
SELECT id, upwork_job_id, job_title, rating, client_country, budget,
       ai_analysis, upwork_url
FROM upwork_jobs
WHERE is_sent = false AND queued_reason = $1 AND ai_analysis IS NOT NULL
ORDER BY rating DESC NULLS LAST, created_at DESC;

-- drain: UPDATE с RETURNING, concurrency-safe
UPDATE upwork_jobs SET is_sent = true, queued_reason = NULL
WHERE id IN (
  SELECT id FROM upwork_jobs
  WHERE is_sent = false AND queued_reason = $1 AND ai_analysis IS NOT NULL
  ORDER BY rating DESC NULLS LAST, created_at DESC
  LIMIT $2                                       -- $2 = N (NULL для всех)
  FOR UPDATE SKIP LOCKED
) RETURNING id, ai_analysis, upwork_url, upwork_job_id;

-- mark_sent: тихо помечаем sent (для «Очистить очередь» в Отчёте)
UPDATE upwork_jobs SET is_sent = true, queued_reason = NULL
WHERE is_sent = false AND queued_reason = $1 AND ai_analysis IS NOT NULL;
```

---

## 11. Логи (с пагинацией)

Подсказка: `События за 7 дней. Стрелки — листать.`

Кнопка `Логи` → последние 10 событий + inline-навигация:
```
Стр. 1 из 12 (всего 117 событий)

2026-05-02 14:23  INFO  pipeline_finished   job=~01a2b3 result=delivered rating=8 dur=12s
2026-05-02 14:22  WARN  llm_fallback        slot=analysis from=deepseek-r1 to=minimax
2026-05-02 14:18  ERROR llm_failed          slot=pre_screen attempts=3 reason=timeout
...

[ ← Назад ]   [ Вперёд → ]
[    Только ошибки    ]
[        Закрыть       ]
```

```python
PAGE_SIZE = 10

async def show_logs_page(message, page: int = 0, only_errors: bool = False):
    where = "level >= 1" if only_errors else "TRUE"     # 1=warn, 2=error
    total = await db.count_events(where)
    rows = await db.fetch_events(where, offset=page * PAGE_SIZE, limit=PAGE_SIZE)
    text = format_log_rows(rows, page, total // PAGE_SIZE + 1)

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("← Назад",
                                        callback_data=f"logs:{page-1}:{int(only_errors)}"))
    if (page + 1) * PAGE_SIZE < total:
        nav.append(InlineKeyboardButton("Вперёд →",
                                        callback_data=f"logs:{page+1}:{int(only_errors)}"))
    filter_btn = InlineKeyboardButton(
        "Все события" if only_errors else "Только ошибки",
        callback_data=f"logs:0:{int(not only_errors)}",
    )
    close_btn = InlineKeyboardButton("Закрыть", callback_data="logs:close")
    kb = InlineKeyboardMarkup(inline_keyboard=[nav, [filter_btn], [close_btn]])
    await message.answer(text, reply_markup=kb, parse_mode=None)
```

**Edit-in-place**: при нажатии стрелок — `message.edit_text(...)` вместо нового сообщения. История чата чистая.

---

## 12. Очистка БД (простое Да/Нет)

```python
async def handle_clear_db_button(message, state):
    await message.answer(
        "Очистить базу данных вакансий? Все записи будут удалены безвозвратно.",
        reply_markup=ReplyKeyboardMarkup([[
            KeyboardButton("Да, очистить"),
            KeyboardButton("Нет"),
        ]])
    )
    await state.set_state(CleanupConfirm.waiting)

async def handle_confirm_yes(message, state):
    await db.truncate_jobs()
    await log.emit("db_truncated", updated_by=message.from_user.id)
    await message.answer("Сохранено.", reply_markup=settings_menu_kb())
    await state.clear()

async def handle_confirm_no(message, state):
    await message.answer("Отменено.", reply_markup=settings_menu_kb())
    await state.clear()
```
