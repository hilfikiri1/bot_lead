"""Weekend-aware scheduling for client follow-ups.

Saturday and Sunday are non-working client days. Presets count working days,
explicit weekend dates roll forward to Monday at the same local clock time, and
the reminder loop never notifies on a weekend. Public holidays are out of scope
until a holiday calendar is configured.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from app.services import followup_service

_INSTALLED = False
_WEEKEND = {5, 6}


def is_weekend(value: datetime) -> bool:
    aware = followup_service._aware(value)  # noqa: SLF001
    if aware is None:
        return False
    return aware.astimezone(followup_service._manager_tz()).weekday() in _WEEKEND  # noqa: SLF001


def roll_forward_to_business_day(value: datetime) -> datetime:
    aware = followup_service._aware(value)  # noqa: SLF001
    if aware is None:
        raise ValueError("Срок follow-up не указан.")
    local = aware.astimezone(followup_service._manager_tz())  # noqa: SLF001
    while local.weekday() in _WEEKEND:
        local += timedelta(days=1)
    return local.astimezone(timezone.utc)


def add_business_days(value: datetime, days: int) -> datetime:
    if days < 0:
        raise ValueError("Количество рабочих дней не может быть отрицательным.")
    local = (followup_service._aware(value) or followup_service.utcnow()).astimezone(
        followup_service._manager_tz()
    )
    remaining = int(days)
    while remaining:
        local += timedelta(days=1)
        if local.weekday() not in _WEEKEND:
            remaining -= 1
    return local


def business_preset_due_at(preset: str, *, now: datetime | None = None) -> datetime:
    if preset not in followup_service.FOLLOWUP_PRESETS:
        raise ValueError("Неизвестный срок follow-up.")
    local_now = (now or followup_service.utcnow()).astimezone(
        followup_service._manager_tz()
    )
    target = add_business_days(local_now, followup_service.FOLLOWUP_PRESETS[preset])
    target = target.replace(hour=10, minute=0, second=0, microsecond=0)
    if target <= local_now:
        target = add_business_days(local_now, 1).replace(
            hour=10, minute=0, second=0, microsecond=0
        )
    return target.astimezone(timezone.utc)


def _business_markup(draft_id: int) -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {"text": "Завтра", "callback_data": f"followup:set:{draft_id}:tomorrow"},
                {"text": "Через 3 дня", "callback_data": f"followup:set:{draft_id}:3d"},
            ],
            [
                {"text": "Через 7 дней", "callback_data": f"followup:set:{draft_id}:7d"},
                {"text": "Выбрать дату", "callback_data": f"followup:custom:{draft_id}"},
            ],
            [{"text": "Не напоминать", "callback_data": f"followup:none:{draft_id}"}],
        ]
    }


def install_business_day_followup_runtime() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True

    original_parse_custom = followup_service.parse_custom_due_at
    original_snooze = followup_service.snooze_followup
    original_send_due = followup_service.send_due_reminders

    followup_service.preset_due_at = business_preset_due_at
    followup_service.followup_prompt_markup = _business_markup

    def parse_custom_due_at_business(value: str, *, now: datetime | None = None) -> datetime:
        parsed = original_parse_custom(value, now=now)
        return roll_forward_to_business_day(parsed)

    async def snooze_followup_business(*args: Any, **kwargs: Any):
        due_at = kwargs.get("due_at")
        if due_at is not None:
            kwargs["due_at"] = roll_forward_to_business_day(due_at)
        return await original_snooze(*args, **kwargs)

    async def send_due_reminders_business(*, now: datetime | None = None, limit: int = 50) -> int:
        current = followup_service._aware(now) or followup_service.utcnow()
        if is_weekend(current):
            return 0
        return await original_send_due(now=current, limit=limit)

    followup_service.parse_custom_due_at = parse_custom_due_at_business
    followup_service.snooze_followup = snooze_followup_business
    followup_service.send_due_reminders = send_due_reminders_business
