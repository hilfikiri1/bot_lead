"""Orchestrate calendar + Kommo task creation with idempotency and partial failures."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.services import calendar_event_builder, calendar_service, crm_service, kommo_service
from app.services.calendar_event_builder import ScheduledEventDraft
from app.services.google_calendar_service import GoogleCalendarError

logger = logging.getLogger(__name__)
settings = get_settings()


async def get_existing_by_idempotency(
    db: AsyncSession, idempotency_key: str
) -> dict[str, Any] | None:
    record = await crm_service.get_calendar_event_by_key(db, idempotency_key)
    if not record or record.status != "success":
        return None
    return {
        "calendar_success": True,
        "calendar_event_id": record.external_event_id,
        "calendar_event_url": record.external_event_url,
        "kommo_task_success": bool(record.kommo_task_id),
        "kommo_task_id": record.kommo_task_id,
        "idempotent": True,
        "title": record.title,
        "start_at": record.start_at,
        "end_at": record.end_at,
        "timezone": record.timezone,
    }


async def schedule_confirmed_event(
    db: AsyncSession,
    *,
    draft: ScheduledEventDraft,
    telegram_user_id: int,
    idempotency_key: str,
) -> dict[str, Any]:
    existing = await get_existing_by_idempotency(db, idempotency_key)
    if existing:
        return existing

    record = await crm_service.create_calendar_event_record(
        db,
        provider=(settings.calendar_provider or "google").strip().lower(),
        kommo_lead_id=draft.kommo_lead_id,
        title=draft.title,
        description=draft.description,
        start_at=draft.start_at,
        end_at=draft.end_at,
        timezone=calendar_event_builder.manager_timezone().key,
        reminder_minutes=draft.reminder_minutes,
        telegram_user_id=telegram_user_id,
        idempotency_key=idempotency_key,
    )

    result: dict[str, Any] = {
        "calendar_success": False,
        "calendar_event_id": None,
        "calendar_event_url": None,
        "calendar_error": None,
        "kommo_task_success": False,
        "kommo_task_id": None,
        "kommo_task_error": None,
        "ics_fallback": False,
        "ics_content": None,
        "idempotent": False,
        "title": draft.title,
        "start_at": draft.start_at,
        "end_at": draft.end_at,
        "timezone": calendar_event_builder.manager_timezone().key,
        "lead_url": draft.lead_url,
        "lead_name": draft.lead_name,
        "event_type": draft.event_type,
    }

    if draft.needs_calendar_event:
        calendar_result = await calendar_service.create_scheduled_event_async(draft)
        result["calendar_success"] = bool(calendar_result.get("success"))
        result["calendar_event_id"] = calendar_result.get("event_id")
        result["calendar_event_url"] = calendar_result.get("event_url")
        result["calendar_error"] = calendar_result.get("error")
        result["ics_fallback"] = bool(calendar_result.get("ics_content"))
        result["ics_content"] = calendar_result.get("ics_content")
        if result["calendar_success"]:
            await crm_service.mark_calendar_event_success(
                db,
                record,
                external_event_id=str(result["calendar_event_id"] or ""),
                external_event_url=str(result["calendar_event_url"] or "") or None,
            )
        else:
            await crm_service.mark_calendar_event_failed(
                db, record, error=str(result["calendar_error"] or "calendar failed")
            )
    else:
        result["calendar_success"] = True

    if draft.needs_kommo_task and draft.kommo_lead_id:
        try:
            task = await kommo_service.create_lead_task(
                lead_id=draft.kommo_lead_id,
                text=draft.title[:1000],
                complete_till=int(draft.start_at.timestamp()),
            )
            result["kommo_task_success"] = True
            result["kommo_task_id"] = task.get("task_id")
            result["lead_url"] = task.get("url") or draft.lead_url
            await crm_service.attach_kommo_task_to_calendar_event(
                db, record, int(task["task_id"]) if task.get("task_id") else None
            )
        except Exception as exc:
            logger.warning("Kommo task creation failed: %s", exc)
            result["kommo_task_error"] = str(exc)[:500]

    if draft.needs_calendar_event and not result["calendar_success"] and not result["ics_fallback"]:
        return result
    if not draft.needs_calendar_event and not result["kommo_task_success"]:
        return result
    return result
