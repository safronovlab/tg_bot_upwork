"""Settings UI: подменю, карточки, FSM entry, universal buffer, toggle, presets.

Реализует BOT.md §3.1-§3.7, §4 (универсальный flow), §5-§7 (карточки), §8 (история).

Архитектурно собрано в одном модуле — UI-навигация плотно связана и легче читается
вместе. Save-handlers остаются в своих модулях (prompts.py, models.py, secrets.py,
thresholds.py) — туда попадает только валидация-и-запись.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from aiogram.fsm.context import FSMContext

from src import db, log
from src.bot import keyboards
from src.bot.formatters import escape_html
from src.bot.handlers.thresholds import PRESETS, THRESHOLD_SPECS, apply_preset
from src.bot.states import ApiKeyEdit, ModelEdit, PromptEdit, ThresholdEdit

if TYPE_CHECKING:
    from aiogram.types import Message


# --------------------------------------------------------------------------- #
# Маппинги label ↔ внутреннее имя — спека BOT.md §3.1, §3.2, §3.3, §3.4
# --------------------------------------------------------------------------- #
PROMPT_LABEL_TO_SLOT: dict[str, str] = {
    "Промпт: Pre-Screen": "pre_screen",
    "Промпт: Анализ": "analysis",
    "Промпт: Cover": "cover",
    "Промпт: AI ответ": "dialog_night",
}

MODEL_LABEL_TO_ROLE: dict[str, str] = {
    "Pre-Screen модель": "prescreen",
    "Анализ модель": "analysis",
    "Pre-Screen фолбэк": "prescreen_fallback",
    "Анализ фолбэк": "analysis_fallback",
}

ROLE_TO_COLUMN: dict[str, str] = {
    "prescreen": "prescreen_model",
    "analysis": "analysis_model",
    "prescreen_fallback": "prescreen_fallback_model",
    "analysis_fallback": "analysis_fallback_model",
}

THRESHOLD_LABEL_TO_FIELD: dict[str, str] = {
    "Pre-Screen порог": "pre_screen_threshold",
    "Анализ порог": "analysis_threshold",
    "Громкость уведомления": "loud_notification_threshold",
    "Минимум потрачено клиентом": "hard_min_client_spent",
    "Минимум рейтинг клиента": "hard_min_client_rating",
    "Минимум наймов для рейтинга": "hard_min_hires_for_rating",
    "Минимум Hourly бюджет": "hard_min_budget_hourly",
    "Минимум Fixed бюджет": "hard_min_budget_fixed",
    "Максимум возраст вакансии": "hard_max_vacancy_age_h",
}

# Текст и рекомендация для каждого порога (BOT.md §3.5).
THRESHOLD_DESCRIPTIONS: dict[str, tuple[str, str]] = {
    "pre_screen_threshold": (
        "Минимальный pre-screen рейтинг (0-10) чтобы вакансия прошла к полному анализу. "
        "Ниже — DELETE из БД. 0 = все проходят.",
        "5 для нормальной фильтрации, 0 если хочется видеть всё",
    ),
    "analysis_threshold": (
        "Минимальный финальный рейтинг (0-10) чтобы вакансия пришла в Telegram. "
        "Ниже — DELETE из БД полностью. 0 = все доходят.",
        "5 для нормальной фильтрации, 7+ только топ",
    ),
    "loud_notification_threshold": (
        "Порог рейтинга для громкого уведомления (0-10). "
        "Вакансии с рейтингом >= порога приходят со звуком, ниже — беззвучно.",
        "8 (только топовые звонят), 0 (всё со звуком), 11 (всё беззвучно)",
    ),
    "hard_min_client_spent": (
        "Hard-фильтр: клиенты с тратой меньше $X отбрасываются ДО LLM. 0 = выключено.",
        "50 (отсекает новичков)",
    ),
    "hard_min_client_rating": (
        "Hard-фильтр: клиенты с рейтингом ниже X.Y (0-5) отбрасываются. "
        "Применяется только если у клиента >= N наймов. 0 = выключено.",
        "4.0",
    ),
    "hard_min_hires_for_rating": (
        "Сколько наймов должно быть у клиента, чтобы рейтингу можно было верить. "
        "С маленькой выборкой 5★ ничего не значит.",
        "3 (фундаментально)",
    ),
    "hard_min_budget_hourly": (
        "Hard-фильтр: hourly-вакансии где верхний бюджет < $X/час отбрасываются. 0 = выключено.",
        "10",
    ),
    "hard_min_budget_fixed": (
        "Hard-фильтр: fixed-вакансии с бюджетом < $X отбрасываются. 0 = выключено.",
        "100",
    ),
    "hard_max_vacancy_age_h": (
        "Hard-фильтр: вакансии старше N часов отбрасываются. 0 = любой возраст ок.",
        "24",
    ),
}

PRESET_LABEL_TO_NAME: dict[str, str] = {
    "Все нули (нет фильтрации)": "zeros",
    "Стандарт": "standard",
    "Строгий": "strict",
}

# Лейблы кнопок-входов в FSM с карточек.
EDIT_BUTTON = "Изменить"
EDIT_VALUE_BUTTON = "Изменить значение"
EDIT_MODEL_BUTTON = "Изменить модель"


# --------------------------------------------------------------------------- #
# Подменю (BOT.md §3.1-§3.4)
# --------------------------------------------------------------------------- #
async def open_prompts_submenu(message: Message) -> None:
    await message.answer(
        "Тексты для LLM. Каждая правка сохраняется в историю.",
        reply_markup=keyboards.prompts_submenu_kb(),
    )


async def open_main_models_submenu(message: Message) -> None:
    await message.answer(
        "Pre-Screen — быстрая модель отсева. Анализ — главная.",
        reply_markup=keyboards.main_models_submenu_kb(),
    )


async def open_fallback_models_submenu(message: Message) -> None:
    await message.answer(
        "Резервные модели — включаются если основная упала.",
        reply_markup=keyboards.fallback_models_submenu_kb(),
    )


async def open_thresholds_submenu(message: Message) -> None:
    """Меню Порогов — переключить reply-keyboard на `[В настройки]` + inline-список."""
    await show_thresholds_menu(message)


async def show_thresholds_menu(message: Any) -> None:
    """Меню Порогов в виде inline-кнопок. Reply-keyboard: только `[В настройки]`.

    Переработано из длинного reply-keyboard'а — он не помещался в чате (BOT.md §3.4).
    """
    await message.answer(
        "Пороги фильтрации. 0 = фильтр выключен.",
        reply_markup=keyboards.thresholds_submenu_kb(),
    )
    await message.answer(
        "Что меняем?",
        reply_markup=keyboards.thresholds_inline_kb(),
    )


async def handle_thresholds_inline_callback(callback: Any, state: FSMContext) -> None:
    """Inline-callback `thr:<field>` — открыть карточку соответствующего порога.

    Спецслучаи: `thr:presets` → подменю Пресетов, `thr:hard_reject_no_hires` →
    toggle-карточка (булев). Все остальные — числовые threshold-карточки.
    """
    field = (callback.data or "").removeprefix("thr:")
    msg = callback.message
    if msg is None or not hasattr(msg, "answer"):
        await callback.answer()
        return

    # Удаляем inline-сообщение перед открытием карточки (UX)
    if hasattr(msg, "delete"):
        try:
            await msg.delete()
        except Exception:
            pass

    if field == "presets":
        await open_presets_submenu(msg, state)
    elif field == "hard_reject_no_hires":
        await show_no_hires_toggle_card(msg, state)
    elif field in THRESHOLD_LABEL_TO_FIELD.values():
        await show_threshold_card(msg, field, state)
    await callback.answer()


async def open_presets_submenu(message: Message, state: FSMContext) -> None:
    """Подменю Пресетов внутри Порогов. `section=presets` — маркер для handle_back."""
    await state.update_data(section="presets", slot=None, role=None, field=None)
    await message.answer(
        "Готовые наборы порогов одной кнопкой. Текущие значения перезапишутся.",
        reply_markup=keyboards.presets_submenu_kb(),
    )


# --------------------------------------------------------------------------- #
# История изменений (BOT.md §8) — общий рендерер
# --------------------------------------------------------------------------- #
def _format_change_line(event: str, ts: Any, data: dict) -> str:
    ts_str = str(ts)[:16] if ts is not None else "?"
    via = data.get("via", "вручную")
    if event == "key_updated":
        return f"  {ts_str}   обновлён by {data.get('updated_by', '?')}"
    if event == "prompt_updated":
        old_len = data.get("old_length", "?")
        new_len = data.get("new_length", "?")
        return f"  {ts_str}   обновлён (длина {old_len} → {new_len})"
    old = data.get("old_value", "?")
    new = data.get("new_value", "?")
    return f"  {ts_str}   было {old} → стало {new}  ({via})"


async def _render_history(event: str, field: str) -> str:
    rows = await db.get_recent_changes(event, field, limit=3)
    if not rows:
        return ""
    lines = ["", "Последние изменения:"]
    for r in rows:
        data = r["data"] or {}
        if isinstance(data, str):
            import msgspec

            try:
                data = msgspec.json.decode(data.encode())
            except Exception:
                data = {}
        lines.append(_format_change_line(event, r["ts"], data))
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Карточки (BOT.md §3.5, §3.6, §5, §6, §7) + entry в FSM
# --------------------------------------------------------------------------- #
async def show_prompt_card(message: Message, slot: str, state: FSMContext | None = None) -> None:
    """BOT.md §6 — карточка одного промта.

    Если передан `state` — слот сохраняется в state.data, чтобы кнопка `Изменить`
    знала какой промт редактировать (см. `_route_edit_btn` в routers.py).
    """
    content = await db.get_prompt(slot)
    preview = (content[:500] + "…") if len(content) > 500 else content
    history = await _render_history("prompt_updated", slot)
    text = (
        f"Промпт: {slot}\n\n"
        f"Текущая длина: {len(content)} символов\n"
        f"Слот: {slot}\n\n"
        f"{escape_html(preview) or '(пусто)'}\n"
        f"{history}"
    )
    if state is not None:
        await state.update_data(slot=slot, role=None, field=None)
    await message.answer(text, reply_markup=keyboards.card_action_kb(EDIT_BUTTON))


async def start_prompt_edit(message: Message, state: FSMContext, slot: str) -> None:
    await state.set_state(PromptEdit.waiting_text)
    await state.update_data(slot=slot, buf="")
    await message.answer(
        f"Изменение промта `{slot}`. Отправь новый текст одним или несколькими сообщениями.\n"
        f"Можно прикрепить .txt файл.",
        reply_markup=keyboards.CANCEL_ONLY_KB,
    )


async def show_model_card(message: Message, role: str, state: FSMContext | None = None) -> None:
    """BOT.md §7 — карточка одной модели (общая для 4 ролей)."""
    column = ROLE_TO_COLUMN[role]
    current = await db.get_model(column)
    history = await _render_history("model_updated", column)
    label = next(k for k, v in MODEL_LABEL_TO_ROLE.items() if v == role)
    text = (
        f"{label}\n\n"
        f"Текущее значение: {current}\n"
        f"Источник: bot_settings (БД)\n\n"
        f"Список доступных моделей: https://openrouter.ai/models\n"
        f"{history}"
    )
    if state is not None:
        await state.update_data(role=role, slot=None, field=None)
    await message.answer(text, reply_markup=keyboards.card_action_kb(EDIT_MODEL_BUTTON))


async def start_model_edit(message: Message, state: FSMContext, role: str) -> None:
    await state.set_state(ModelEdit.waiting_name)
    await state.update_data(role=role, buf="")
    await message.answer(
        "Отправь имя модели в формате `vendor/model-name`.",
        reply_markup=keyboards.CANCEL_ONLY_KB,
    )


def _format_threshold_value(field: str, value: Any) -> str:
    if value is None:
        return "0"
    spec = THRESHOLD_SPECS.get(field, {})
    if spec.get("type") == "float":
        return f"{float(value):.1f}"
    return str(value)


async def show_threshold_card(
    message: Message, field: str, state: FSMContext | None = None
) -> None:
    """BOT.md §3.5 — карточка одного числового порога."""
    current = await db.get_setting(field)
    description, recommendation = THRESHOLD_DESCRIPTIONS[field]
    history = await _render_history("threshold_updated", field)
    label = next(k for k, v in THRESHOLD_LABEL_TO_FIELD.items() if v == field)
    text = (
        f"{label}\n\n"
        f"Текущее значение: {_format_threshold_value(field, current)}\n"
        f"Поле в БД: {field}\n\n"
        f"Что это: {description}\n\n"
        f"Рекомендация: {recommendation}.\n"
        f"Чтобы выключить — введите 0.\n"
        f"{history}"
    )
    if state is not None:
        await state.update_data(field=field, slot=None, role=None)
    await message.answer(text, reply_markup=keyboards.card_action_kb(EDIT_VALUE_BUTTON))


async def start_threshold_edit(message: Message, state: FSMContext, field: str) -> None:
    await state.set_state(ThresholdEdit.waiting_value)
    await state.update_data(field=field, buf="")
    spec = THRESHOLD_SPECS[field]
    await message.answer(
        f"Введите значение в диапазоне {spec['min']}..{spec['max']} ({spec['type_human']}).",
        reply_markup=keyboards.CANCEL_ONLY_KB,
    )


async def show_chat_ai_toggle_card(
    message: Message, state: FSMContext | None = None
) -> None:
    """BOT.md §3.6 + CHAT.md §6 — toggle AI ответа при остановке (is_paused=true).

    `field=chat_ai_night_enabled` — маркер для handle_back и для
    `handle_no_hires_toggle` который шарит ту же реализацию toggle.
    """
    current = bool(await db.get_setting("chat_ai_night_enabled"))
    state_label = "ВКЛЮЧЕНО" if current else "ВЫКЛЮЧЕНО"
    history = await _render_history("threshold_updated", "chat_ai_night_enabled")
    text = (
        "AI ответ при остановке (Чат)\n\n"
        f"Текущее значение: {state_label}\n"
        "Поле в БД: chat_ai_night_enabled\n\n"
        "Что это: когда бот «Остановлен» (is_paused=true) и эта опция ВКЛ —\n"
        "AI автоматически отвечает клиентам по промту `dialog_night` с задержкой\n"
        "1-2 минуты. Если ВЫКЛ — сообщения копятся и показываются в Отчёте,\n"
        "ответы не отправляются.\n"
        f"{history}"
    )
    if state is not None:
        await state.update_data(
            field="chat_ai_night_enabled",
            slot=None,
            role=None,
            email_field=None,
            section="settings",
        )
    await message.answer(text, reply_markup=keyboards.toggle_action_kb(bool(current)))


async def show_no_hires_toggle_card(message: Message, state: FSMContext | None = None) -> None:
    """BOT.md §3.6 — булев toggle карточка. `field=hard_reject_no_hires` — маркер для Назад."""
    current = await db.get_setting("hard_reject_no_hires")
    state_label = "ВКЛЮЧЕНО" if current else "ВЫКЛЮЧЕНО"
    history = await _render_history("threshold_updated", "hard_reject_no_hires")
    text = (
        "Отсекать клиентов без наймов\n\n"
        f"Текущее значение: {state_label}\n"
        "Поле в БД: hard_reject_no_hires\n\n"
        "Что это: если включено — вакансии от клиентов с 0 наймов отбрасываются ДО LLM.\n"
        f"{history}"
    )
    if state is not None:
        await state.update_data(
            field="hard_reject_no_hires", slot=None, role=None, section=None
        )
    await message.answer(text, reply_markup=keyboards.toggle_action_kb(bool(current)))


_BOOL_TOGGLE_REFRESH: dict[str, Any] = {}  # field → callable(message, state) — заполняется ниже


async def handle_bool_toggle(message: Message, state: FSMContext) -> None:
    """Generic [Включить]/[Выключить] для всех boolean-карточек.

    Маркер какое поле тогглить — `state.data.field`. Поддерживаются:
        - hard_reject_no_hires
        - chat_ai_night_enabled

    Match диспатчер `_BOOL_TOGGLE_REFRESH` решает какую карточку перерисовать
    после flip'а (чтобы видно было обновлённое состояние).
    """
    data = await state.get_data()
    field = data.get("field")
    if field not in {"hard_reject_no_hires", "chat_ai_night_enabled"}:
        # Не toggle-card → игнорируем (сюда могут попасть случайно если FSM
        # сломался; молча возвращаемся, не показываем ошибку)
        return

    current = bool(await db.get_setting(field))
    new_value = not current
    await db.set_setting(field, new_value)
    await db.invalidate_settings_cache()
    user_id = getattr(getattr(message, "from_user", None), "id", None)
    await log.emit(
        "threshold_updated",
        field=field,
        old_value="ВКЛ" if current else "ВЫКЛ",
        new_value="ВКЛ" if new_value else "ВЫКЛ",
        via="manual",
        updated_by=user_id,
    )
    await message.answer("Сохранено.")
    refresh = _BOOL_TOGGLE_REFRESH.get(field)
    if refresh is not None:
        await refresh(message, state)


# Обратная совместимость: старое имя для router'а
handle_no_hires_toggle = handle_bool_toggle


def _register_bool_toggle_refresh() -> None:
    """Привязка field → функция-перерисовщик карточки. Вызывается на module-load."""
    _BOOL_TOGGLE_REFRESH["hard_reject_no_hires"] = (
        lambda msg, _state: show_no_hires_toggle_card(msg)
    )
    _BOOL_TOGGLE_REFRESH["chat_ai_night_enabled"] = (
        lambda msg, _state: show_chat_ai_toggle_card(msg)
    )


async def show_apikey_card(message: Message, state: FSMContext | None = None) -> None:
    """BOT.md §5 — карточка API-ключа OpenRouter.

    Сбрасывает slot/role/field в state — `_route_edit_btn` без context'а
    интерпретирует нажатие `Изменить` как `start_apikey_edit`.
    """
    key = await db.get_openrouter_key()
    masked = f"{key[:6]}…{key[-4:]} ({len(key)} символов)" if len(key) >= 10 else "не задан"
    history = await _render_history("key_updated", "openrouter_api_key")
    text = (
        "API ключ OpenRouter\n\n"
        f"Текущий ключ: {masked}\n"
        "Источник: secrets (БД)\n\n"
        "Получить ключ: https://openrouter.ai/keys\n"
        "Формат: sk-or-v1-...\n"
        f"{history}"
    )
    if state is not None:
        await state.update_data(slot=None, role=None, field=None)
    await message.answer(text, reply_markup=keyboards.card_action_kb(EDIT_BUTTON))


async def start_apikey_edit(message: Message, state: FSMContext) -> None:
    await state.set_state(ApiKeyEdit.waiting_key)
    await state.update_data(buf="", user_message_ids=[])
    await message.answer(
        "Отправь новый API-ключ. После сохранения сообщения с ключом будут удалены.",
        reply_markup=keyboards.CANCEL_ONLY_KB,
    )


# --------------------------------------------------------------------------- #
# Универсальный buffer-handler (BOT.md §4) — ловит текст в любом FSM-состоянии
# --------------------------------------------------------------------------- #
# Email-поля которые считаются паролями — маскируются в preview + сообщения
# удаляются после Save (CHAT.md §4 Configuration).
_EMAIL_PASSWORD_FIELDS: frozenset[str] = frozenset({"imap_password", "smtp_password"})


def _is_password_input(current_state: str | None, email_field: str | None) -> bool:
    """True если текущий FSM-state означает ввод пароля (нужно маскировать)."""
    if current_state == ApiKeyEdit.waiting_key.state:
        return True
    from src.bot.states import EmailCredentialEdit

    return (
        current_state == EmailCredentialEdit.waiting_value.state
        and email_field in _EMAIL_PASSWORD_FIELDS
    )


def _redact_for_preview(value: str, is_password: bool) -> str:
    if is_password:
        if len(value) > 10:
            return f"{value[:6]}…{value[-4:]} ({len(value)} символов)"
        return "<скрыто>"
    if len(value) > 60:
        return value[:60] + "…"
    return value


async def universal_buffer(message: Message, state: FSMContext) -> None:
    """BOT.md §4 — копит ввод в state.data['buf'].

    PromptEdit — накопительный (append).
    Остальные — замещающий (последний message перезаписывает).
    Password-поля (API key + email passwords) маскируются в preview и id'шники
    сообщений сохраняются для удаления при Save.
    """
    current_state = await state.get_state()
    data = await state.get_data()
    text = message.text or ""
    is_password = _is_password_input(current_state, data.get("email_field"))

    if current_state == PromptEdit.waiting_text.state:
        buf = data.get("buf", "") + text
        preview = f"{len(buf)} символов"
    else:
        buf = text.strip()
        preview = _redact_for_preview(buf, is_password)

    update: dict[str, Any] = {"buf": buf}

    # Копим id сообщений для удаления при Сохранить (BOT.md §5, CHAT.md §4)
    if is_password:
        ids = list(data.get("user_message_ids", []))
        if message.message_id is not None:
            ids.append(message.message_id)
        update["user_message_ids"] = ids

    await state.update_data(**update)
    await message.answer(
        f"Получено: {preview}. Жми Сохранить или отправь ещё.",
        reply_markup=keyboards.EDIT_KB,
    )


# --------------------------------------------------------------------------- #
# .txt upload для PromptEdit (BOT.md §6.1)
# --------------------------------------------------------------------------- #
async def upload_prompt_file(message: Message, state: FSMContext, bot: Any) -> None:
    document = message.document
    if document is None:
        return
    name = (document.file_name or "").lower()
    if not name.endswith(".txt"):
        await message.answer("Принимаю только .txt файлы.")
        return
    file_bytes = await bot.download(document)
    text = file_bytes.read().decode("utf-8", errors="replace")
    await state.update_data(buf=text)
    await message.answer(
        f"Файл загружен: {len(text)} символов. Жми Сохранить.",
        reply_markup=keyboards.EDIT_KB,
    )


# --------------------------------------------------------------------------- #
# Presets — выбор и подтверждение (BOT.md §3.7)
# --------------------------------------------------------------------------- #
def _format_preset_summary(name: str) -> str:
    values = PRESETS[name]
    lines = [f"Применить пресет «{name}»? Будут установлены значения:", ""]
    for k, v in values.items():
        lines.append(f"  {k} = {v}")
    return "\n".join(lines)


async def select_preset(message: Message, state: FSMContext) -> None:
    """Кнопка нажата → показать сводку и попросить подтверждение Да/Нет."""
    label = message.text or ""
    name = PRESET_LABEL_TO_NAME.get(label)
    if name is None:
        return
    await state.update_data(pending_preset=name)
    summary = _format_preset_summary(name)
    await message.answer(summary, reply_markup=keyboards.preset_confirm_kb())


async def confirm_preset_yes(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    name = data.get("pending_preset")
    if name not in PRESETS:
        await message.answer("Пресет не выбран.", reply_markup=keyboards.presets_submenu_kb())
        await state.clear()
        return
    user_id = getattr(getattr(message, "from_user", None), "id", None) or 0
    await apply_preset(db._conn(), name, user_id)
    await state.clear()
    await message.answer("Сохранено.")
    await show_thresholds_menu(message)


async def confirm_preset_no(message: Message, state: FSMContext) -> None:
    """Возврат в presets-подменю — section=presets чтобы [Назад] вёл в Пороги."""
    await state.clear()
    await state.update_data(section="presets")
    await message.answer("Отменено.", reply_markup=keyboards.presets_submenu_kb())


# --------------------------------------------------------------------------- #
# Вход в карточку — диспатчер: «Промпт: X» / «X модель» / «X порог» / API ключ
# --------------------------------------------------------------------------- #
async def route_prompt_button(message: Message, state: FSMContext) -> None:
    slot = PROMPT_LABEL_TO_SLOT.get(message.text or "")
    if slot:
        await show_prompt_card(message, slot, state)


async def route_model_button(message: Message, state: FSMContext) -> None:
    role = MODEL_LABEL_TO_ROLE.get(message.text or "")
    if role:
        await show_model_card(message, role, state)


async def route_threshold_button(message: Message, state: FSMContext) -> None:
    field = THRESHOLD_LABEL_TO_FIELD.get(message.text or "")
    if field:
        await show_threshold_card(message, field, state)


# --------------------------------------------------------------------------- #
# Выходы из подменю
# --------------------------------------------------------------------------- #
async def back_to_settings_from_prompts(message: Message, state: FSMContext) -> None:
    """`В настройки` — общий выход из любого Settings sub-submenu. Очищает breadcrumbs."""
    await state.update_data(slot=None, role=None, field=None, section=None)
    await show_settings_menu(message)


# --------------------------------------------------------------------------- #
# Меню Настроек — inline-кнопки в сообщении + reply-keyboard [Назад] (BOT.md §3)
# --------------------------------------------------------------------------- #
async def show_settings_menu(message: Any) -> None:
    """Открыть меню Настроек: переключить reply-keyboard на [Назад] +
    отправить сообщение с inline-кнопками 7 разделов настроек.
    """
    await message.answer("Настройки бота.", reply_markup=keyboards.settings_back_only_kb())
    await message.answer("Что меняем?", reply_markup=keyboards.settings_inline_kb())


async def _dispatch_settings_action(action: str, msg: Any, state: FSMContext) -> None:
    """Диспатч `settings:<action>` → нужный open_*_submenu / show_*_card.

    Вынесено из handle_settings_inline_callback для снижения cyclomatic complexity.
    Lazy-imports для logs / cleanup / email / chat_ai — они сами импортируют
    settings_ui (циркулярка иначе).
    """
    if action == "prompts":
        await open_prompts_submenu(msg)
    elif action == "main_models":
        await open_main_models_submenu(msg)
    elif action == "fallback_models":
        await open_fallback_models_submenu(msg)
    elif action == "thresholds":
        await open_thresholds_submenu(msg)
    elif action == "apikey":
        await show_apikey_card(msg, state)
    elif action == "email":
        from src.bot.handlers.email_creds import show_email_menu

        await show_email_menu(msg)
    elif action == "chat_ai":
        await show_chat_ai_toggle_card(msg, state)
    elif action == "logs":
        from src.bot.handlers import logs as logs_h

        await logs_h.show_logs_page(msg)
    elif action == "cleanup":
        from src.bot.handlers import cleanup as cleanup_h

        await cleanup_h.handle_clear_db_button(msg, state)


async def handle_settings_inline_callback(
    callback: Any, state: FSMContext
) -> None:
    """Inline-callback `settings:<action>` — диспатчит в подменю.

    Для разделов БЕЗ собственного reply-keyboard `[В настройки]` (logs, apikey,
    cleanup, email) ставим breadcrumb `section="settings"` чтобы [Назад] вёл
    обратно в Settings inline, а не в главное меню.
    """
    action = (callback.data or "").removeprefix("settings:")
    msg = callback.message
    if msg is None or not hasattr(msg, "answer"):
        await callback.answer()
        return

    # Удалить inline-сообщение перед переходом в подменю (UX)
    if hasattr(msg, "delete"):
        import contextlib

        with contextlib.suppress(Exception):
            await msg.delete()

    # Breadcrumb для разделов без собственного "В настройки"
    if action in {"apikey", "logs", "cleanup", "email"}:
        await state.update_data(section="settings", slot=None, role=None, field=None)

    await _dispatch_settings_action(action, msg, state)
    await callback.answer()


# --------------------------------------------------------------------------- #
# Валидация имени модели — формат vendor/model-name
# --------------------------------------------------------------------------- #
MODEL_NAME_RE = re.compile(r"^[a-z0-9._\-]+/[a-z0-9._\-]+(:[a-z0-9._\-]+)?$")


def is_valid_model_name(name: str) -> bool:
    return bool(MODEL_NAME_RE.match(name)) and 3 <= len(name) <= 100


# Регистрируем перерисовщики boolean toggle-карточек на module-load.
_register_bool_toggle_refresh()
