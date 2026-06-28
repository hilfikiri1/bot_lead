"""
calendar_service.py
Creates Google Calendar events after explicit manager approval.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from app.config import get_settings
from app.services.gmail_service import _get_credentials

logger = logging.getLogger(__name__)
settings = get_settings()


def _calendar_service():
    return build("calendar", "v3", credentials=_get_credentials())


def create_event(
    title: str,
    description: str,
    start_time_iso: str | None,
    duration_minutes: int = 15,
    calendar_id: str | None = None,
) -> str:
    """
    Create a Google Calendar event and return its event ID.

    Naive times are interpreted in MANAGER_TIMEZONE (Europe/Warsaw by default).
    """
    calendar_id = calendar_id or settings.google_calendar_id
    manager_tz = ZoneInfo(settings.manager_timezone)

    if start_time_iso:
        try:
            start_dt = datetime.fromisoformat(start_time_iso)
        except ValueError:
            logger.warning(
                "Could not parse start_time '%s', defaulting to tomorrow 10:00",
                start_time_iso,
            )
            start_dt = _default_start()
    else:
        start_dt = _default_start()

    if start_dt.tzinfo is None:
        start_dt = start_dt.replace(tzinfo=manager_tz)

    duration_minutes = max(5, min(int(duration_minutes or 15), 8 * 60))
    end_dt = start_dt + timedelta(minutes=duration_minutes)

    event_body = {
        "summary": title,
        "description": description,
        "start": {
            "dateTime": start_dt.isoformat(),
            "timeZone": settings.manager_timezone,
        },
        "end": {
            "dateTime": end_dt.isoformat(),
            "timeZone": settings.manager_timezone,
        },
        "reminders": {
            "useDefault": False,
            "overrides": [
                {"method": "popup", "minutes": 10},
                {"method": "email", "minutes": 30},
            ],
        },
    }

    try:
        service = _calendar_service()
        event = service.events().insert(calendarId=calendar_id, body=event_body).execute()
        event_id = event["id"]
        logger.info("Calendar event created: %s", event_id)
        return event_id
    except HttpError as exc:
        logger.error("Google Calendar API error: %s", exc)
        raise


def _default_start() -> datetime:
    manager_tz = ZoneInfo(settings.manager_timezone)
    tomorrow = datetime.now(tz=manager_tz) + timedelta(days=1)
    return tomorrow.replace(hour=10, minute=0, second=0, microsecond=0)
