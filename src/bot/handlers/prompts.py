"""Подменю «Изменить промт» (4 слота) + карточки + edit. См. ../BOT.md §3.1, §6."""

from __future__ import annotations

from typing import TYPE_CHECKING

from src import db, log
from src.bot import keyboards

if TYPE_CHECKING:
    from aiogram.fsm.context import FSMContext
    from aiogram.types import Message

PROMPT_MIN_LEN = 50
PROMPT_MAX_LEN = 50000


async def save_prompt(message: Message, state: FSMContext) -> None:
    """Спец-хендлер `Сохранить` для PromptEdit (BOT.md §4)."""
    data = await state.get_data()
    buf = data.get("buf") or ""
    slot = data.get("slot")

    if slot is None:
        await message.answer("Слот не указан.")
        return

    if not (PROMPT_MIN_LEN <= len(buf) <= PROMPT_MAX_LEN):
        await message.answer(
            f"Длина {len(buf)} вне диапазона {PROMPT_MIN_LEN}..{PROMPT_MAX_LEN}. Попробуй ещё."
        )
        return

    user_id = getattr(getattr(message, "from_user", None), "id", None)
    old = await db.get_prompt(slot)
    await db.insert_prompt_history(slot, old, user_id)
    await db.update_prompt(slot, buf)
    await db.invalidate_prompt_cache(slot)
    await log.emit(
        "prompt_updated",
        field=slot,
        old_length=len(old or ""),
        new_length=len(buf),
        updated_by=user_id,
    )
    await state.clear()
    await message.answer("Сохранено.", reply_markup=keyboards.prompts_submenu_kb())
