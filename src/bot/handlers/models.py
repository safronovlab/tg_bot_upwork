"""Подменю Основные/Фолбэк модели + карточка одной модели + edit. См. ../BOT.md §3.2-3.3, §7."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from src import db, log
from src.bot import keyboards

if TYPE_CHECKING:
    from aiogram.fsm.context import FSMContext
    from aiogram.types import Message

ROLE_TO_COLUMN: dict[str, str] = {
    "prescreen": "prescreen_model",
    "analysis": "analysis_model",
    "prescreen_fallback": "prescreen_fallback_model",
    "analysis_fallback": "analysis_fallback_model",
}


MODEL_NAME_RE = re.compile(r"^[a-z0-9._\-]+/[a-z0-9._\-]+(:[a-z0-9._\-]+)?$")


async def save_model(message: Message, state: FSMContext) -> None:
    """Спец-хендлер `Сохранить` для ModelEdit (BOT.md §7)."""
    data = await state.get_data()
    buf = (data.get("buf") or "").strip()
    role = data.get("role")

    if role not in ROLE_TO_COLUMN:
        await message.answer("Неизвестный тип модели.")
        return

    if not (3 <= len(buf) <= 100) or not MODEL_NAME_RE.match(buf):
        await message.answer("Формат vendor/model-name (3..100 символов). Попробуй ещё.")
        return

    column = ROLE_TO_COLUMN[role]
    user_id = getattr(getattr(message, "from_user", None), "id", None)
    old = await db.get_model(column)
    await db.set_model(column, buf)
    await db.invalidate_settings_cache()
    await log.emit(
        "model_updated",
        field=column,
        old_value=old,
        new_value=buf,
        via="manual",
        updated_by=user_id,
    )
    await state.clear()
    # Возврат в главное меню моделей или фолбэков (выбор по role)
    if role.endswith("_fallback"):
        kb = keyboards.fallback_models_submenu_kb()
    else:
        kb = keyboards.main_models_submenu_kb()
    await message.answer("Сохранено.", reply_markup=kb)
