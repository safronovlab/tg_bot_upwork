"""Все ReplyKeyboard и InlineKeyboard для меню/карточек. См. BOT.md."""

from __future__ import annotations

from typing import Any

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

from src import db


# --------------------------------------------------------------------------- #
# Главное меню (BOT.md §1)
# --------------------------------------------------------------------------- #
async def main_menu_kb(pool: Any, is_paused: bool) -> ReplyKeyboardMarkup:
    pause_btn = "Запустить" if is_paused else "Остановить"

    n_jobs = await db.count_queued_by_reason_cached(pool, "manual")
    n_sync = await db.count_queued_by_reason_cached(pool, "menu")
    # Chat-сообщения копятся независимо от is_paused — счётчик всегда актуален
    n_chat = await db.count_unshown_inbound_messages_cached(pool)

    # Format: Отчёт (3+5) если есть и вакансии, и сообщения. Иначе одно из.
    if n_jobs and n_chat:
        report_label = f"Отчёт ({n_jobs}+{n_chat})"
    elif n_jobs or n_chat:
        report_label = f"Отчёт ({n_jobs + n_chat})"
    else:
        report_label = "Отчёт"

    sync_label = f"Синхронизация ({n_sync})" if n_sync else "Синхронизация"

    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=pause_btn), KeyboardButton(text=report_label)],
            [KeyboardButton(text="Избранное"), KeyboardButton(text="Настройки")],
            [KeyboardButton(text=sync_label)],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


# --------------------------------------------------------------------------- #
# Меню «Настройки» (BOT.md §3)
# --------------------------------------------------------------------------- #
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
        resize_keyboard=True,
        is_persistent=True,
    )


# --------------------------------------------------------------------------- #
# Универсальные клавиатуры FSM (BOT.md §4)
# --------------------------------------------------------------------------- #
EDIT_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="Сохранить")],
        [KeyboardButton(text="Назад")],
    ],
    resize_keyboard=True,
    is_persistent=True,
)


CANCEL_ONLY_KB = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="Назад")]],
    resize_keyboard=True,
    is_persistent=True,
)


# --------------------------------------------------------------------------- #
# Карточка вакансии — inline (BOT.md §9)
# --------------------------------------------------------------------------- #
def card_buttons(job: Any) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Открыть", url=job.upwork_url or "https://www.upwork.com/"
                ),
                InlineKeyboardButton(text="Избранное", callback_data=f"save_{job.upwork_job_id}"),
            ]
        ]
    )


def cleanup_confirm_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Да, очистить"), KeyboardButton(text="Нет")],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


# --------------------------------------------------------------------------- #
# Подменю настроек (BOT.md §3.1-§3.4)
# --------------------------------------------------------------------------- #
def prompts_submenu_kb() -> ReplyKeyboardMarkup:
    """BOT.md §3.1 — 4 слота промтов (с добавлением dialog_night из миграции 001)."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Промпт: Pre-Screen"), KeyboardButton(text="Промпт: Анализ")],
            [KeyboardButton(text="Промпт: Cover"), KeyboardButton(text="Промпт: AI ответ")],
            [KeyboardButton(text="В настройки")],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def main_models_submenu_kb() -> ReplyKeyboardMarkup:
    """BOT.md §3.2 — Pre-Screen и Анализ модели. `В настройки` идёт на уровень выше."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Pre-Screen модель")],
            [KeyboardButton(text="Анализ модель")],
            [KeyboardButton(text="В настройки")],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def fallback_models_submenu_kb() -> ReplyKeyboardMarkup:
    """BOT.md §3.3 — фолбэк модели."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Pre-Screen фолбэк")],
            [KeyboardButton(text="Анализ фолбэк")],
            [KeyboardButton(text="В настройки")],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def thresholds_submenu_kb() -> ReplyKeyboardMarkup:
    """Reply-keyboard когда юзер в Порогах — только `[В настройки]`. Сами пороги
    переехали в inline-сообщение `thresholds_inline_kb()` (BOT.md §3.4 переработан)."""
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="В настройки")]],
        resize_keyboard=True,
        is_persistent=True,
    )


def thresholds_inline_kb() -> InlineKeyboardMarkup:
    """Меню Порогов inline (был длинный reply-keyboard, неудобный)."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Пресеты", callback_data="thr:presets")],
            [
                InlineKeyboardButton(text="Pre-Screen порог", callback_data="thr:pre_screen_threshold"),
                InlineKeyboardButton(text="Анализ порог", callback_data="thr:analysis_threshold"),
            ],
            [InlineKeyboardButton(text="Громкость уведомления", callback_data="thr:loud_notification_threshold")],
            [InlineKeyboardButton(text="Минимум потрачено клиентом", callback_data="thr:hard_min_client_spent")],
            [InlineKeyboardButton(text="Минимум рейтинг клиента", callback_data="thr:hard_min_client_rating")],
            [InlineKeyboardButton(text="Минимум наймов для рейтинга", callback_data="thr:hard_min_hires_for_rating")],
            [InlineKeyboardButton(text="Минимум Hourly бюджет", callback_data="thr:hard_min_budget_hourly")],
            [InlineKeyboardButton(text="Минимум Fixed бюджет", callback_data="thr:hard_min_budget_fixed")],
            [InlineKeyboardButton(text="Отсекать клиентов без наймов", callback_data="thr:hard_reject_no_hires")],
            [InlineKeyboardButton(text="Максимум возраст вакансии", callback_data="thr:hard_max_vacancy_age_h")],
        ]
    )


def presets_submenu_kb() -> ReplyKeyboardMarkup:
    """BOT.md §3.7 — три пресета."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Все нули (нет фильтрации)")],
            [KeyboardButton(text="Стандарт")],
            [KeyboardButton(text="Строгий")],
            [KeyboardButton(text="Назад")],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def card_action_kb(edit_label: str = "Изменить") -> ReplyKeyboardMarkup:
    """Универсальная клавиатура для карточки настройки: [Изменить] / [Назад]."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=edit_label)],
            [KeyboardButton(text="Назад")],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def toggle_action_kb(current: bool) -> ReplyKeyboardMarkup:
    """Карточка булева toggle: [Включить]/[Выключить] + [Назад] (BOT.md §3.6)."""
    label = "Выключить" if current else "Включить"
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=label)],
            [KeyboardButton(text="Назад")],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def preset_confirm_kb() -> ReplyKeyboardMarkup:
    """BOT.md §3.7 — подтверждение применения пресета."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Да, применить"), KeyboardButton(text="Нет")],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


# --------------------------------------------------------------------------- #
# Подменю Отчёт (BOT.md §10) — три кнопки
# --------------------------------------------------------------------------- #
def report_submenu_kb(n_jobs: int, n_chat: int = 0) -> ReplyKeyboardMarkup:
    """Подменю Отчёт — два типа очередей: вакансии (manual queue) и chat-сообщения.

    Чтобы пользователь не путался когда чего нет — кнопки скрываются (счётчик 0).
    Кнопка `Очистить очередь` действует на ВАКАНСИИ; chat-сообщения чистятся
    при показе через `Показать сообщения` (флаг is_shown_in_report).
    """
    jobs_label = f"Показать вакансии ({n_jobs})" if n_jobs > 0 else "Показать вакансии"
    chat_label = f"Показать сообщения ({n_chat})" if n_chat > 0 else "Показать сообщения"
    rows: list[list[KeyboardButton]] = []
    rows.append([KeyboardButton(text=jobs_label)])
    rows.append([KeyboardButton(text=chat_label)])
    rows.append([KeyboardButton(text="Очистить очередь")])
    rows.append([KeyboardButton(text="Назад")])
    return ReplyKeyboardMarkup(
        keyboard=rows,
        resize_keyboard=True,
        is_persistent=True,
    )


# --------------------------------------------------------------------------- #
# Настройки (BOT.md §3) — переход на inline (длинный reply-keyboard был неудобен)
# --------------------------------------------------------------------------- #
def settings_back_only_kb() -> ReplyKeyboardMarkup:
    """Reply-клавиатура внизу когда юзер в Настройках — только [Назад]."""
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="Назад")]],
        resize_keyboard=True,
        is_persistent=True,
    )


def settings_inline_kb() -> InlineKeyboardMarkup:
    """Меню Настроек — inline-кнопки в сообщении. См. settings_ui.show_settings_menu."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Изменить промт", callback_data="settings:prompts")],
            [InlineKeyboardButton(text="Основные модели", callback_data="settings:main_models")],
            [InlineKeyboardButton(text="Фолбэк модели", callback_data="settings:fallback_models")],
            [InlineKeyboardButton(text="Пороги", callback_data="settings:thresholds")],
            [InlineKeyboardButton(text="API ключ OpenRouter", callback_data="settings:apikey")],
            [InlineKeyboardButton(text="Email подключение", callback_data="settings:email")],
            [InlineKeyboardButton(text="AI ответ при остановке", callback_data="settings:chat_ai")],
            [InlineKeyboardButton(text="Логи", callback_data="settings:logs")],
            [InlineKeyboardButton(text="Очистить БД", callback_data="settings:cleanup")],
        ]
    )


def email_inline_kb() -> InlineKeyboardMarkup:
    """Меню Email подключения — inline-кнопки. См. CHAT.md §4 Configuration."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="IMAP login", callback_data="email:imap_user")],
            [InlineKeyboardButton(text="IMAP пароль", callback_data="email:imap_password")],
            [InlineKeyboardButton(text="SMTP login", callback_data="email:smtp_user")],
            [InlineKeyboardButton(text="SMTP пароль", callback_data="email:smtp_password")],
        ]
    )


# --------------------------------------------------------------------------- #
# Подменю Избранное (BOT.md §9)
# --------------------------------------------------------------------------- #
def favorites_submenu_kb() -> ReplyKeyboardMarkup:
    """Подменю Избранное — `[Очистить всё]` и `[Назад]`."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Очистить всё")],
            [KeyboardButton(text="Назад")],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )
