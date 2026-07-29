"""Calendar policy: only precise timed events become Calendar entries."""

from __future__ import annotations

import re

CALENDAR_EVENT_TYPES = {"call", "meeting", "zoom", "visit", "exhibition", "trip", "demo"}

_TIME_RE = re.compile(
    r"(\d{1,2}[:.]\d{2}|\b\d{1,2}\s*(?:час|ч|am|pm)\b|\bв\s*\d{1,2}\b)",
    re.I,
)


def requires_calendar(*, event_type: str | None, due_at: str | None, title: str | None = None) -> bool:
    """Ordinary follow-ups without precise time stay as Kommo tasks / Telegram reminders."""
    kind = str(event_type or "").casefold().strip()
    blob = f"{due_at or ''} {title or ''}"
    has_time = bool(_TIME_RE.search(blob))
    if kind in CALENDAR_EVENT_TYPES and has_time:
        return True
    if kind in {"followup", "message", "task", "note"}:
        return False
    return bool(kind in {"call", "meeting"} and has_time)


def calendar_policy_reason(*, event_type: str | None, due_at: str | None) -> str:
    if requires_calendar(event_type=event_type, due_at=due_at):
        return "Точное время + тип события → Google Calendar + задача Kommo"
    return "Обычный follow-up → задача Kommo / Telegram-напоминание (без Calendar)"
