"""Configurable business-hours helpers for Kommo task due dates.

Timezone and business hours are configurable via ``LEAD_PROCESSING_TIMEZONE``
(falls back to the existing ``MANAGER_TIMEZONE``), ``LEAD_PROCESSING_BUSINESS_START``
and ``LEAD_PROCESSING_BUSINESS_END``.
"""

from __future__ import annotations

from datetime import datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

from app.config import get_settings

settings = get_settings()


def timezone() -> ZoneInfo:
    name = settings.lead_processing_timezone.strip() or settings.manager_timezone or "Europe/Warsaw"
    try:
        return ZoneInfo(name)
    except Exception:
        return ZoneInfo("Europe/Warsaw")


def _parse_hm(value: str, default: dtime) -> dtime:
    try:
        hour_str, minute_str = value.strip().split(":")
        return dtime(int(hour_str), int(minute_str))
    except Exception:
        return default


def business_window() -> tuple[dtime, dtime]:
    start = _parse_hm(settings.lead_processing_business_start or "09:00", dtime(9, 0))
    end = _parse_hm(settings.lead_processing_business_end or "18:00", dtime(18, 0))
    return start, end


def now() -> datetime:
    return datetime.now(timezone())


def is_business_hours(moment: datetime | None = None) -> bool:
    current = moment or now()
    start, end = business_window()
    return current.weekday() < 5 and start <= current.time() <= end


def next_business_day(moment: datetime) -> datetime:
    candidate = moment + timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate += timedelta(days=1)
    return candidate


def add_business_days(moment: datetime, days: int) -> datetime:
    candidate = moment
    for _ in range(max(0, days)):
        candidate = next_business_day(candidate)
    return candidate


def due_timestamp(due_rule: str, *, explicit_due_at: str | None = None) -> int:
    """Resolve a Kommo task ``complete_till`` unix timestamp from a due rule."""
    tz = timezone()
    current = now()
    start, _end = business_window()

    if due_rule == "manual" and explicit_due_at:
        try:
            parsed = datetime.fromisoformat(explicit_due_at)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=tz)
            return int(parsed.timestamp())
        except ValueError:
            pass

    if due_rule == "today" and is_business_hours(current):
        due = current + timedelta(hours=2)
        return int(due.timestamp())

    if due_rule == "in_2_business_days":
        due = add_business_days(current, 2)
    else:
        # "next_business_day", unrecognised rules, or "today" requested
        # after hours all resolve to the next working day morning.
        due = next_business_day(current)

    due = due.replace(hour=start.hour, minute=start.minute, second=0, microsecond=0)
    return int(due.timestamp())
