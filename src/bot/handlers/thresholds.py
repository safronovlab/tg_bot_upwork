"""Подменю «Пороги» (LLM + hard) + карточки + Пресеты + edit. См. ../BOT.md §3.4-3.7."""

from __future__ import annotations

from typing import Any

from src import db, log

THRESHOLD_SPECS: dict[str, dict[str, Any]] = {
    "pre_screen_threshold": {"type": "int", "type_human": "целое число", "min": 0, "max": 10},
    "analysis_threshold": {"type": "int", "type_human": "целое число", "min": 0, "max": 10},
    "loud_notification_threshold": {
        "type": "int",
        "type_human": "целое число",
        "min": 0,
        "max": 10,
    },
    "hard_min_client_spent": {"type": "float", "type_human": "число", "min": 0, "max": 1_000_000},
    "hard_min_client_rating": {"type": "float", "type_human": "число", "min": 0, "max": 5},
    "hard_min_hires_for_rating": {
        "type": "int",
        "type_human": "целое число",
        "min": 0,
        "max": 1000,
    },
    "hard_min_budget_hourly": {"type": "float", "type_human": "число", "min": 0, "max": 10_000},
    "hard_min_budget_fixed": {"type": "float", "type_human": "число", "min": 0, "max": 1_000_000},
    "hard_max_vacancy_age_h": {"type": "int", "type_human": "целое число", "min": 0, "max": 1000},
}


PRESETS: dict[str, dict[str, Any]] = {
    "zeros": {
        "pre_screen_threshold": 0,
        "analysis_threshold": 0,
        "loud_notification_threshold": 0,
        "hard_min_client_spent": 0,
        "hard_min_client_rating": 0,
        "hard_min_budget_hourly": 0,
        "hard_min_budget_fixed": 0,
        "hard_reject_no_hires": False,
        "hard_max_vacancy_age_h": 0,
    },
    "standard": {
        "pre_screen_threshold": 5,
        "analysis_threshold": 5,
        "loud_notification_threshold": 8,
        "hard_min_client_spent": 50,
        "hard_min_client_rating": 4.0,
        "hard_min_budget_hourly": 10,
        "hard_min_budget_fixed": 100,
        "hard_reject_no_hires": False,
        "hard_max_vacancy_age_h": 0,
    },
    "strict": {
        "pre_screen_threshold": 7,
        "analysis_threshold": 7,
        "loud_notification_threshold": 9,
        "hard_min_client_spent": 200,
        "hard_min_client_rating": 4.5,
        "hard_min_budget_hourly": 25,
        "hard_min_budget_fixed": 500,
        "hard_reject_no_hires": True,
        "hard_max_vacancy_age_h": 24,
    },
}


async def save_threshold(message: Any, state: Any) -> None:
    """Спец-хендлер `Сохранить` для ThresholdEdit (BOT.md §4)."""
    data = await state.get_data()
    buf = (data.get("buf") or "").strip()
    field = data.get("field")
    spec = THRESHOLD_SPECS.get(field)
    if spec is None:
        await message.answer("Неизвестный порог.")
        return

    try:
        if spec["type"] == "int":
            val: float = int(buf)
        else:
            val = float(buf.replace(",", "."))
    except ValueError:
        await message.answer(f"Ожидаю {spec['type_human']}. Попробуй ещё.")
        return

    if not (spec["min"] <= val <= spec["max"]):
        await message.answer(f"Диапазон {spec['min']}..{spec['max']}. Попробуй ещё.")
        return

    old = await db.get_setting(field)
    await db.set_setting(field, val)
    await db.invalidate_settings_cache()
    await log.emit(
        "threshold_updated",
        field=field,
        old_value=str(old),
        new_value=str(val),
        via="manual",
        updated_by=getattr(getattr(message, "from_user", None), "id", None),
    )
    await state.clear()
    from src.bot.handlers.settings_ui import show_thresholds_menu

    await message.answer("Сохранено.")
    await show_thresholds_menu(message)


async def apply_preset(pool: Any, name: str, user_id: int) -> None:
    new = PRESETS[name]
    old = await db.get_settings_full(pool)
    sets = ", ".join(f"{k} = ${i + 1}" for i, k in enumerate(new))
    await pool.execute(
        f"UPDATE bot_settings SET {sets}, updated_at = now() WHERE id = 1",
        *new.values(),
    )
    await db.invalidate_settings_cache()
    for field, new_val in new.items():
        old_val = getattr(old, field, None)
        if old_val != new_val:
            await log.emit(
                "threshold_updated",
                field=field,
                old_value=str(old_val),
                new_value=str(new_val),
                via=f"preset_{name}",
                updated_by=user_id,
            )
    await log.emit("preset_applied", preset=name, updated_by=user_id)
