"""Local PostgreSQL persistence for clients, leads, audio jobs and actions."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models import Action, AIReport, CalendarEvent, Client, Lead, SpreadsheetLeadMapping, VoiceNote

logger = logging.getLogger(__name__)


async def upsert_client(db: AsyncSession, client_data: dict[str, Any]) -> Client:
    """Find an existing client by local/Kommo identity or create a new one."""
    phone = (client_data.get("phone") or "").strip() or None
    email = (client_data.get("email") or "").strip() or None
    kommo_contact_id = client_data.get("kommo_contact_id")

    existing: Client | None = None
    if kommo_contact_id:
        result = await db.execute(
            select(Client).where(Client.kommo_contact_id == int(kommo_contact_id))
        )
        existing = result.scalar_one_or_none()
    if not existing and phone:
        result = await db.execute(select(Client).where(Client.phone == phone))
        existing = result.scalars().first()
    if not existing and email:
        result = await db.execute(select(Client).where(Client.email == email))
        existing = result.scalars().first()

    if existing:
        for field in ("name", "phone", "email", "company", "language"):
            value = client_data.get(field)
            if value:
                setattr(existing, field, value)
        if kommo_contact_id:
            existing.kommo_contact_id = int(kommo_contact_id)
        await db.commit()
        await db.refresh(existing)
        logger.info("Updated existing client id=%d", existing.id)
        return existing

    client = Client(
        kommo_contact_id=int(kommo_contact_id) if kommo_contact_id else None,
        name=client_data.get("name"),
        phone=phone,
        email=email,
        company=client_data.get("company"),
        language=client_data.get("language"),
        source=client_data.get("source") or "telegram_voice_note",
    )
    db.add(client)
    await db.commit()
    await db.refresh(client)
    logger.info("Created new client id=%d", client.id)
    return client


async def create_lead(
    db: AsyncSession, client: Client, lead_data: dict[str, Any]
) -> Lead:
    urgency_map = {
        "high": "high",
        "medium": "medium",
        "low": "low",
        "unknown": "medium",
    }
    lead = Lead(
        client_id=client.id,
        product_requested=lead_data.get("product_requested"),
        budget=lead_data.get("budget"),
        country=lead_data.get("country"),
        city=lead_data.get("city"),
        status=lead_data.get("status", "new"),
        priority=urgency_map.get(lead_data.get("urgency", "unknown"), "medium"),
        next_action=lead_data.get("next_action"),
        next_followup_at=lead_data.get("next_followup_at"),
    )
    db.add(lead)
    await db.commit()
    await db.refresh(lead)
    logger.info("Created local lead id=%d for client id=%d", lead.id, client.id)
    return lead


async def get_or_create_voice_note_job(
    db: AsyncSession,
    *,
    telegram_user_id: int,
    telegram_message_id: int,
    processing_status: str = "received",
) -> tuple[VoiceNote, bool]:
    result = await db.execute(
        select(VoiceNote).where(
            VoiceNote.telegram_user_id == telegram_user_id,
            VoiceNote.telegram_message_id == telegram_message_id,
        )
    )
    existing = result.scalar_one_or_none()
    if existing:
        return existing, False

    note = VoiceNote(
        telegram_user_id=telegram_user_id,
        telegram_message_id=telegram_message_id,
        processing_status=processing_status,
        processing_started_at=datetime.now(tz=timezone.utc),
    )
    db.add(note)
    try:
        await db.commit()
        await db.refresh(note)
        return note, True
    except IntegrityError:
        await db.rollback()
        result = await db.execute(
            select(VoiceNote).where(
                VoiceNote.telegram_user_id == telegram_user_id,
                VoiceNote.telegram_message_id == telegram_message_id,
            )
        )
        existing = result.scalar_one()
        return existing, False


async def update_voice_note_status(
    db: AsyncSession,
    voice_note: VoiceNote,
    status: str,
    *,
    error: str | None = None,
    finished: bool = False,
) -> VoiceNote:
    voice_note.processing_status = status
    voice_note.processing_error = error[:2000] if error else None
    if not voice_note.processing_started_at:
        voice_note.processing_started_at = datetime.now(tz=timezone.utc)
    if finished:
        voice_note.processing_finished_at = datetime.now(tz=timezone.utc)
    await db.commit()
    await db.refresh(voice_note)
    return voice_note


async def complete_voice_note_job(
    db: AsyncSession,
    voice_note: VoiceNote,
    *,
    lead: Lead,
    audio_url: str,
    transcript: str,
    language: str,
) -> VoiceNote:
    voice_note.lead_id = lead.id
    voice_note.audio_url = audio_url
    voice_note.transcript = transcript
    voice_note.language = language
    voice_note.processing_status = "ready"
    voice_note.processing_error = None
    voice_note.processing_finished_at = datetime.now(tz=timezone.utc)
    await db.commit()
    await db.refresh(voice_note)
    logger.info("Completed voice note job id=%d", voice_note.id)
    return voice_note


async def save_voice_note(
    db: AsyncSession,
    lead: Lead,
    telegram_user_id: int,
    telegram_message_id: int,
    audio_url: str,
    transcript: str,
    language: str,
) -> VoiceNote:
    """Backward-compatible wrapper used by older code paths."""
    note, _ = await get_or_create_voice_note_job(
        db,
        telegram_user_id=telegram_user_id,
        telegram_message_id=telegram_message_id,
    )
    return await complete_voice_note_job(
        db,
        note,
        lead=lead,
        audio_url=audio_url,
        transcript=transcript,
        language=language,
    )


async def save_ai_report(
    db: AsyncSession, voice_note: VoiceNote, analysis: dict
) -> AIReport:
    email = analysis.get("email", {})
    calendar = analysis.get("calendar", {})
    whatsapp = analysis.get("whatsapp", {})

    existing_result = await db.execute(
        select(AIReport).where(AIReport.voice_note_id == voice_note.id)
    )
    report = existing_result.scalar_one_or_none()
    values = {
        "conversation_summary": analysis.get("conversation_summary"),
        "what_manager_said": analysis.get("confirmed_facts")
        or analysis.get("what_manager_said"),
        "mistakes_or_weak_points": analysis.get("risks")
        or analysis.get("mistakes_or_weak_points"),
        "missing_questions": analysis.get("missing_questions"),
        "recommended_next_step": analysis.get("recommended_next_step"),
        "email_subject": email.get("subject"),
        "email_body": email.get("body"),
        "whatsapp_message": whatsapp.get("message"),
        "calendar_title": calendar.get("title"),
        "calendar_description": calendar.get("description"),
        "calendar_start_time": calendar.get("start_time"),
        "calendar_duration_minutes": calendar.get("duration_minutes", 15),
        "confidence_score": analysis.get("confidence_score"),
        "needs_human_review": analysis.get("needs_human_review", True),
        "raw_json": analysis,
    }
    if report:
        for key, value in values.items():
            setattr(report, key, value)
    else:
        report = AIReport(voice_note_id=voice_note.id, **values)
        db.add(report)
    await db.commit()
    await db.refresh(report)
    logger.info("Saved AI report id=%d", report.id)
    return report


async def create_action(
    db: AsyncSession,
    lead: Lead,
    action_type: str,
    payload: dict,
    *,
    idempotency_key: str | None = None,
) -> Action:
    if idempotency_key:
        result = await db.execute(
            select(Action).where(Action.idempotency_key == idempotency_key)
        )
        existing = result.scalar_one_or_none()
        if existing:
            return existing

    action = Action(
        lead_id=lead.id,
        action_type=action_type,
        status="pending",
        idempotency_key=idempotency_key,
        payload=payload,
        approved_by_user=False,
    )
    db.add(action)
    try:
        await db.commit()
        await db.refresh(action)
        return action
    except IntegrityError:
        await db.rollback()
        if not idempotency_key:
            raise
        result = await db.execute(
            select(Action).where(Action.idempotency_key == idempotency_key)
        )
        return result.scalar_one()


async def update_action_status(
    db: AsyncSession,
    action: Action,
    status: str,
    approved: bool = False,
    executed_at: datetime | None = None,
    error_message: str | None = None,
) -> Action:
    action.status = status
    action.approved_by_user = approved
    action.error_message = error_message[:2000] if error_message else None
    if executed_at:
        action.executed_at = executed_at
    await db.commit()
    await db.refresh(action)
    return action


async def save_kommo_mapping(
    db: AsyncSession,
    *,
    lead_id: int,
    kommo_lead_id: int,
    kommo_contact_id: int | None,
    pipeline_id: int | None,
    status_id: int | None,
    url: str | None,
) -> None:
    result = await db.execute(
        select(Lead).options(selectinload(Lead.client)).where(Lead.id == lead_id)
    )
    lead = result.scalar_one_or_none()
    if not lead:
        return
    lead.kommo_lead_id = kommo_lead_id
    lead.kommo_pipeline_id = pipeline_id
    lead.kommo_status_id = status_id
    lead.kommo_url = url
    if lead.client and kommo_contact_id:
        lead.client.kommo_contact_id = kommo_contact_id
    await db.commit()


async def save_notion_mapping(
    db: AsyncSession,
    *,
    client_id: int | None = None,
    lead_id: int | None = None,
    voice_note_id: int | None = None,
    client_page_id: str | None = None,
    lead_page_id: str | None = None,
    call_page_id: str | None = None,
) -> None:
    if client_id and client_page_id:
        client = await db.get(Client, client_id)
        if client:
            client.notion_page_id = client_page_id
    if lead_id and lead_page_id:
        lead = await db.get(Lead, lead_id)
        if lead:
            lead.notion_page_id = lead_page_id
    if voice_note_id and call_page_id:
        voice_note = await db.get(VoiceNote, voice_note_id)
        if voice_note:
            voice_note.notion_page_id = call_page_id
    await db.commit()


async def save_spreadsheet_lead_mapping(
    db: AsyncSession,
    *,
    kommo_lead_id: int,
    spreadsheet_lead_number: str,
    original_product: str | None,
    short_product_ru: str | None,
    old_kommo_name: str | None,
    new_kommo_name: str | None,
    spreadsheet_row_number: int | None,
    matched_by: str | None,
    matched_value_hash: str | None,
    created_by_telegram_user_id: int | None,
) -> SpreadsheetLeadMapping:
    record = SpreadsheetLeadMapping(
        kommo_lead_id=kommo_lead_id,
        spreadsheet_lead_number=spreadsheet_lead_number[:32],
        original_product=original_product,
        short_product_ru=(short_product_ru or "")[:120] or None,
        old_kommo_name=old_kommo_name,
        new_kommo_name=new_kommo_name,
        spreadsheet_row_number=spreadsheet_row_number,
        matched_by=(matched_by or "")[:32] or None,
        matched_value_hash=(matched_value_hash or "")[:64] or None,
        created_by_telegram_user_id=created_by_telegram_user_id,
    )
    db.add(record)
    await db.commit()
    await db.refresh(record)
    return record


async def get_calendar_event_by_key(
    db: AsyncSession, idempotency_key: str
) -> CalendarEvent | None:
    if not idempotency_key:
        return None
    result = await db.execute(
        select(CalendarEvent).where(CalendarEvent.idempotency_key == idempotency_key)
    )
    return result.scalar_one_or_none()


async def create_calendar_event_record(
    db: AsyncSession,
    *,
    provider: str,
    kommo_lead_id: int | None,
    title: str | None,
    description: str | None,
    start_at: datetime | None,
    end_at: datetime | None,
    timezone: str | None,
    reminder_minutes: int | None,
    telegram_user_id: int | None,
    idempotency_key: str,
) -> CalendarEvent:
    record = CalendarEvent(
        provider=provider[:32],
        kommo_lead_id=kommo_lead_id,
        title=(title or "")[:500] or None,
        description=description,
        start_at=start_at,
        end_at=end_at,
        timezone=(timezone or "")[:64] or None,
        reminder_minutes=reminder_minutes,
        telegram_user_id=telegram_user_id,
        idempotency_key=idempotency_key[:255],
        status="pending",
    )
    db.add(record)
    await db.commit()
    await db.refresh(record)
    return record


async def mark_calendar_event_success(
    db: AsyncSession,
    record: CalendarEvent,
    *,
    external_event_id: str,
    external_event_url: str | None,
) -> CalendarEvent:
    record.external_event_id = external_event_id[:255]
    record.external_event_url = external_event_url
    record.status = "success"
    record.error = None
    await db.commit()
    await db.refresh(record)
    return record


async def mark_calendar_event_failed(
    db: AsyncSession,
    record: CalendarEvent,
    *,
    error: str,
) -> CalendarEvent:
    record.status = "failed"
    record.error = error[:2000]
    await db.commit()
    await db.refresh(record)
    return record


async def attach_kommo_task_to_calendar_event(
    db: AsyncSession,
    record: CalendarEvent,
    kommo_task_id: int | None,
) -> CalendarEvent:
    if kommo_task_id:
        record.kommo_task_id = kommo_task_id
        if record.status == "pending":
            record.status = "success"
    await db.commit()
    await db.refresh(record)
    return record


async def get_user_command_context(
    db: AsyncSession,
    *,
    telegram_user_id: int,
) -> dict[str, Any]:
    try:
        result = await db.execute(
            select(VoiceNote)
            .options(
                selectinload(VoiceNote.lead).selectinload(Lead.client),
                selectinload(VoiceNote.ai_report),
            )
            .where(VoiceNote.telegram_user_id == telegram_user_id)
            .order_by(VoiceNote.created_at.desc())
            .limit(1)
        )
        voice_note = result.scalar_one_or_none()
    except Exception as exc:
        logger.warning(
            "Could not load command context for user %s: %s",
            telegram_user_id,
            exc,
        )
        return {"telegram_user_id": telegram_user_id}
    if not voice_note:
        return {"telegram_user_id": telegram_user_id}
    lead = voice_note.lead
    client = lead.client if lead else None
    return {
        "telegram_user_id": telegram_user_id,
        "local_lead_id": lead.id if lead else None,
        "voice_note_id": voice_note.id,
        "kommo_lead_id": lead.kommo_lead_id if lead else None,
        "lead_name": (
            lead.product_requested if lead and lead.product_requested else None
        ),
        "notion_client_page_id": client.notion_page_id if client else None,
        "notion_lead_page_id": lead.notion_page_id if lead else None,
        "notion_call_page_id": voice_note.notion_page_id,
    }


async def recent_audio_jobs(
    db: AsyncSession,
    *,
    telegram_user_id: int,
    limit: int = 8,
) -> list[VoiceNote]:
    result = await db.execute(
        select(VoiceNote)
        .where(VoiceNote.telegram_user_id == telegram_user_id)
        .order_by(VoiceNote.created_at.desc())
        .limit(max(1, min(limit, 20)))
    )
    return list(result.scalars().all())
