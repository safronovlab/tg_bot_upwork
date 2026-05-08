"""Регистрация aiogram-router'ов и связь кнопок/коллбэков с handlers.

См. BOT.md — каждая кнопка / inline-callback здесь связан со своим handler'ом.
Архитектурно отделено от bot/app.py чтобы build() оставался простым.
"""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from src.bot.handlers import (
    cleanup as cleanup_h,
)
from src.bot.handlers import (
    email_creds as email_h,
)
from src.bot.handlers import (
    favorites as favorites_h,
)
from src.bot.handlers import (
    logs as logs_h,
)
from src.bot.handlers import (
    menu as menu_h,
)
from src.bot.handlers import (
    models as models_h,
)
from src.bot.handlers import (
    prompts as prompts_h,
)
from src.bot.handlers import (
    reports as reports_h,
)
from src.bot.handlers import (
    secrets as secrets_h,
)
from src.bot.handlers import (
    settings_ui as ui,
)
from src.bot.handlers import (
    thresholds as thresholds_h,
)
from src.bot.states import (
    ApiKeyEdit,
    CleanupConfirm,
    EmailCredentialEdit,
    ModelEdit,
    PromptEdit,
    ThresholdEdit,
)


# --------------------------------------------------------------------------- #
# Главное меню (BOT.md §1, §2)
# --------------------------------------------------------------------------- #
def _register_main_menu(router: Router) -> None:
    router.message.register(menu_h.handle_start, Command("start"))
    router.message.register(menu_h.handle_back, F.text == "Назад")
    router.message.register(menu_h.handle_settings_btn, F.text == "Настройки")
    router.message.register(menu_h.handle_pause_toggle, F.text.in_({"Запустить", "Остановить"}))

    # prefix-match — счётчик `Отчёт (3)` не должен ломать маршрутизацию (BOT.md §1)
    router.message.register(reports_h.handle_report, F.text.startswith("Отчёт"))
    router.message.register(favorites_h.handle_favorites_btn, F.text.startswith("Избранное"))
    # prefix-match — счётчик `Синхронизация (3)` не должен ломать маршрутизацию
    router.message.register(reports_h.handle_sync, F.text.startswith("Синхронизация"))

    # Подменю Отчёт (BOT.md §10 + CHAT.md §7) — drain manual-очереди + chat-сообщений
    router.message.register(
        reports_h.handle_report_show_jobs, F.text.startswith("Показать вакансии")
    )
    router.message.register(
        reports_h.handle_report_show_messages, F.text.startswith("Показать сообщения")
    )
    # Обратная совместимость: старая кнопка "Выгрузить все" — для тестов и legacy
    router.message.register(reports_h.handle_report_unload_all, F.text.startswith("Выгрузить все"))
    router.message.register(reports_h.handle_report_clear, F.text == "Очистить очередь")

    # Подменю Избранное (BOT.md §9)
    router.message.register(favorites_h.handle_clear_all_request, F.text == "Очистить всё")


# --------------------------------------------------------------------------- #
# Меню «Настройки» (BOT.md §3)
# --------------------------------------------------------------------------- #
def _register_settings_menu(router: Router) -> None:
    # 1-й уровень — открытие подменю
    router.message.register(ui.open_prompts_submenu, F.text == "Изменить промт")
    router.message.register(ui.open_main_models_submenu, F.text == "Основные модели")
    router.message.register(ui.open_fallback_models_submenu, F.text == "Фолбэк модели")
    router.message.register(ui.open_thresholds_submenu, F.text == "Пороги")
    router.message.register(ui.show_apikey_card, F.text == "API ключ OpenRouter")

    router.message.register(logs_h.show_logs_page, F.text == "Логи")
    router.message.register(cleanup_h.handle_clear_db_button, F.text == "Очистить БД")

    # «В настройки» — выход из подменю «Изменить промт» (BOT.md §3.1)
    router.message.register(ui.back_to_settings_from_prompts, F.text == "В настройки")


# --------------------------------------------------------------------------- #
# Подменю промтов / моделей / порогов (BOT.md §3.1-§3.4)
# --------------------------------------------------------------------------- #
def _register_settings_submenus(router: Router) -> None:
    router.message.register(ui.route_prompt_button, F.text.in_(set(ui.PROMPT_LABEL_TO_SLOT.keys())))
    router.message.register(ui.route_model_button, F.text.in_(set(ui.MODEL_LABEL_TO_ROLE.keys())))
    router.message.register(
        ui.route_threshold_button, F.text.in_(set(ui.THRESHOLD_LABEL_TO_FIELD.keys()))
    )

    # Toggle карточка (BOT.md §3.6)
    router.message.register(ui.show_no_hires_toggle_card, F.text == "Отсекать клиентов без наймов")
    router.message.register(ui.handle_no_hires_toggle, F.text.in_({"Включить", "Выключить"}))

    # Пресеты (BOT.md §3.7)
    router.message.register(ui.open_presets_submenu, F.text == "Пресеты")
    router.message.register(ui.select_preset, F.text.in_(set(ui.PRESET_LABEL_TO_NAME.keys())))
    router.message.register(ui.confirm_preset_yes, F.text == "Да, применить")
    router.message.register(ui.confirm_preset_no, F.text == "Нет")


# --------------------------------------------------------------------------- #
# FSM entry-handlers — нажатие кнопки `Изменить` на карточке (BOT.md §4)
# --------------------------------------------------------------------------- #
async def _enter_prompt_edit(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    slot = data.get("slot")
    if slot is None:
        await message.answer("Сначала выбери слот промта.")
        return
    await ui.start_prompt_edit(message, state, slot)


async def _enter_model_edit(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    role = data.get("role")
    if role is None:
        await message.answer("Сначала выбери модель.")
        return
    await ui.start_model_edit(message, state, role)


async def _enter_threshold_edit(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    field = data.get("field")
    if field is None:
        await message.answer("Сначала выбери порог.")
        return
    await ui.start_threshold_edit(message, state, field)


async def _enter_email_edit(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    field = data.get("email_field")
    if field is None:
        await message.answer("Сначала выбери email-поле.")
        return
    await email_h.start_email_edit(message, state, field)


async def _route_edit_btn(message: Message, state: FSMContext) -> None:
    """Кнопка `Изменить` / `Изменить значение` / `Изменить модель` — диспатчер по state.

    Какой FSM запустить определяется по `slot` / `role` / `field` / `email_field`
    в state.data, которые предыдущий `show_*_card` положил туда. Без любого
    context'а — это API-key edit.
    """
    data = await state.get_data()
    if data.get("slot") is not None:
        await _enter_prompt_edit(message, state)
    elif data.get("role") is not None:
        await _enter_model_edit(message, state)
    elif data.get("field") is not None:
        await _enter_threshold_edit(message, state)
    elif data.get("email_field") is not None:
        await _enter_email_edit(message, state)
    else:
        await ui.start_apikey_edit(message, state)


def _register_fsm_entries(router: Router) -> None:
    """Кнопки `Изменить` / `Изменить значение` / `Изменить модель` со страницы карточки."""
    router.message.register(
        _route_edit_btn,
        F.text.in_({ui.EDIT_BUTTON, ui.EDIT_VALUE_BUTTON, ui.EDIT_MODEL_BUTTON}),
    )


# --------------------------------------------------------------------------- #
# FSM Save / Cancel / Buffer / Upload (BOT.md §4)
# --------------------------------------------------------------------------- #
async def _universal_cancel(message: Message, state: FSMContext) -> None:
    """`Назад` в любом FSM — отменяет редактирование, **сохраняет breadcrumbs** для
    иерархического возврата (handle_back уведёт в parent submenu, а не в главное)."""
    data = await state.get_data()
    breadcrumbs = {
        k: data[k]
        for k in ("slot", "role", "field", "email_field", "section")
        if data.get(k) is not None
    }
    await state.clear()
    if breadcrumbs:
        await state.update_data(**breadcrumbs)
    await menu_h.handle_back(message, state)


async def _save_api_key_wrapper(message: Message, state: FSMContext) -> None:
    """Save handler для ApiKeyEdit — пробрасывает Bot из notifier (aiogram не передаёт)."""
    from src import notifier as notifier_mod

    if notifier_mod.bot is None:
        await message.answer("Bot недоступен.")
        return
    await secrets_h.save_api_key(message, state, notifier_mod.bot)


async def _save_email_credential_wrapper(message: Message, state: FSMContext) -> None:
    """Save handler для EmailCredentialEdit — пробрасывает Bot для удаления password-сообщений."""
    from src import notifier as notifier_mod

    if notifier_mod.bot is None:
        await message.answer("Bot недоступен.")
        return
    await email_h.save_email_credential(message, state, notifier_mod.bot)


async def _upload_prompt_wrapper(message: Message, state: FSMContext) -> None:
    """`.txt`-upload в PromptEdit (BOT.md §6.1) — пробрасывает Bot из notifier."""
    from src import notifier as notifier_mod

    if notifier_mod.bot is None:
        return
    await ui.upload_prompt_file(message, state, notifier_mod.bot)


def _register_fsm_handlers(router: Router) -> None:
    """`Сохранить` для каждого FSM + universal buffer + cancel + upload."""
    fsm_states = (PromptEdit, ApiKeyEdit, ModelEdit, ThresholdEdit, EmailCredentialEdit)

    # Cancel первым — иначе buffer его перехватит
    router.message.register(_universal_cancel, StateFilter(*fsm_states), F.text == "Назад")

    # Save — каждый со своим спец-handler'ом (BOT.md §4)
    router.message.register(prompts_h.save_prompt, PromptEdit.waiting_text, F.text == "Сохранить")
    router.message.register(_save_api_key_wrapper, ApiKeyEdit.waiting_key, F.text == "Сохранить")
    router.message.register(models_h.save_model, ModelEdit.waiting_name, F.text == "Сохранить")
    router.message.register(
        thresholds_h.save_threshold, ThresholdEdit.waiting_value, F.text == "Сохранить"
    )
    router.message.register(
        _save_email_credential_wrapper,
        EmailCredentialEdit.waiting_value,
        F.text == "Сохранить",
    )

    # .txt файлы для PromptEdit (BOT.md §6.1)
    router.message.register(_upload_prompt_wrapper, PromptEdit.waiting_text, F.document)

    # Universal buffer — должен быть последним среди FSM-фильтров
    router.message.register(
        ui.universal_buffer,
        StateFilter(*fsm_states),
        F.text,
        ~F.text.in_({"Сохранить", "Назад"}),
    )


# --------------------------------------------------------------------------- #
# Cleanup confirm (BOT.md §12)
# --------------------------------------------------------------------------- #
def _register_cleanup_confirm(router: Router) -> None:
    router.message.register(
        cleanup_h.handle_confirm_yes, CleanupConfirm.waiting, F.text == "Да, очистить"
    )
    router.message.register(cleanup_h.handle_confirm_no, CleanupConfirm.waiting, F.text == "Нет")


# --------------------------------------------------------------------------- #
# Inline-callbacks: карточка вакансии + Логи + Отчёт (BOT.md §9, §10, §11)
# --------------------------------------------------------------------------- #
def _register_callbacks(router: Router) -> None:
    # Live карточка (BOT.md §9): A → B → C / B
    router.callback_query.register(favorites_h.handle_save_favorite, F.data.startswith("save_"))
    router.callback_query.register(
        favorites_h.handle_show_description, F.data.startswith("desc_")
    )
    router.callback_query.register(
        favorites_h.handle_show_analysis_live, F.data.startswith("analysis_")
    )

    # Submenu карточка: title ↔ analysis + delete
    router.callback_query.register(
        favorites_h.handle_show_analysis_sub, F.data.startswith("subana_")
    )
    router.callback_query.register(
        favorites_h.handle_show_title_sub, F.data.startswith("subtit_")
    )
    router.callback_query.register(favorites_h.handle_delete_favorite, F.data.startswith("del_"))

    # Подтверждение «Очистить всё избранное»
    router.callback_query.register(
        favorites_h.handle_clear_all_callback, F.data.startswith("clrfav:")
    )

    # Меню Настроек теперь inline (BOT.md §3) — диспатч 7 разделов одним хендлером
    router.callback_query.register(
        ui.handle_settings_inline_callback, F.data.startswith("settings:")
    )

    # Меню Порогов тоже inline (BOT.md §3.4) — диспатч 11 callback'ов
    router.callback_query.register(
        ui.handle_thresholds_inline_callback, F.data.startswith("thr:")
    )

    # Меню Email подключения inline (CHAT.md §4) — 4 callback'а (imap/smtp × user/password)
    router.callback_query.register(
        email_h.handle_email_inline_callback, F.data.startswith("email:")
    )

    router.callback_query.register(_handle_logs_callback, F.data.startswith("logs:"))


async def _handle_logs_callback(callback: CallbackQuery) -> None:
    """Inline-callback Логов: `logs:<page>:<only_errors>`, `logs:close`,
    `logs:clear`, `logs:clearyes`, `logs:clearno`."""
    data = callback.data or ""
    msg = callback.message
    if data == "logs:close":
        if msg is not None and hasattr(msg, "delete"):
            await msg.delete()
        await callback.answer()
        return
    if data == "logs:clear":
        await logs_h.handle_clear_logs(callback)
        return
    if data == "logs:clearyes":
        await logs_h.handle_clear_logs_confirm(callback, yes=True)
        return
    if data == "logs:clearno":
        await logs_h.handle_clear_logs_confirm(callback, yes=False)
        return
    parts = data.split(":")
    if len(parts) != 3:
        await callback.answer()
        return
    try:
        page = int(parts[1])
        only_errors = bool(int(parts[2]))
    except ValueError:
        await callback.answer()
        return
    if msg is not None and hasattr(msg, "answer"):
        # edit=True — пагинация/фильтр обновляют существующее сообщение,
        # вместо отправки нового (захламляло чат).
        await logs_h.show_logs_page(msg, page=page, only_errors=only_errors, edit=True)  # type: ignore[arg-type]
    await callback.answer()


# --------------------------------------------------------------------------- #
# Сборка
# --------------------------------------------------------------------------- #
def build_router() -> Router:
    """Собирает один корневой Router со всеми зарегистрированными handlers.

    ВАЖЕН ПОРЯДОК регистрации (aiogram матчит первый подходящий):
      1. FSM-handlers (cancel/save/buffer/upload) — должны иметь приоритет над
         текстом без state, иначе кнопка `Сохранить` вне FSM попадёт в buffer.
      2. CleanupConfirm — узкий state-фильтр.
      3. Главное меню + подменю настроек + карточки.
      4. Inline callbacks.
    """
    root = Router(name="bot_root")

    _register_fsm_handlers(root)
    _register_cleanup_confirm(root)
    _register_main_menu(root)
    _register_settings_menu(root)
    _register_settings_submenus(root)
    _register_fsm_entries(root)
    _register_callbacks(root)

    return root
