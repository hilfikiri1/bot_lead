"""
crm_service.py
Saves clients, leads, voice notes, and AI reports to the database.
"""
from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.models import Client, Lead, VoiceNote, AIReport, Action

logger = logging.getLogger(__name__)


async def upsert_client(db: AsyncSession, client_data: dict) -> Client:
    """Find an existing client by phone/email or create a new one."""
    phone = client_data.get("phone")
    email = client_data.get("email")

    existing = None
    if phone:
        result = await db.execute(select(Client).where(Client.phone == phone))
        existing = result.scalar_one_or_none()
    if not existing and email:
        result = await db.execute(select(Client).where(Client.email == email))
        existing = result.scalar_one_or_none()

    if existing:
        # Update known fields
        for field in ("name", "email", "company", "language"):
            val = client_data.get(field)
            if val:
                setattr(existing, field, val)
        await db.commit()
        await db.refresh(existing)
        logger.info("Updated existing client id=%d", existing.id)
        return existing

    client = Client(
        name=client_data.get("name"),
        phone=phone,
        email=email,
        company=client_data.get("company"),
        language=client_data.get("language"),
        source="telegram_voice_note",
    )
    db.add(client)
    await db.commit()
    await db.refresh(client)
    logger.info("Created new client id=%d", client.id)
    return client


async def create_lead(db: AsyncSession, client: Client, lead_data: dict) -> Lead:
    """Create a new lead for the client."""
    # Parse urgency → priority
    urgency_map = {"high": "high", "medium": "medium", "low": "low", "unknown": "medium"}
    priority = urgency_map.get(lead_data.get("urgency", "unknown"), "medium")

    followup_at = None
    lead = Lead(
        client_id=client.id,
        product_requested=lead_data.get("product_requested"),
        budget=lead_data.get("budget"),
        country=lead_data.get("country"),
        city=lead_data.get("city"),
        status=lead_data.get("status", "new"),
        priority=priority,
        next_action=None,
        next_followup_at=followup_at,
    )
    db.add(lead)
    await db.commit()
    await db.refresh(lead)
    logger.info("Created lead id=%d for client id=%d", lead.id, client.id)
    return lead


async def save_voice_note(
    db: AsyncSession,
    lead: Lead,
    telegram_user_id: int,
    telegram_message_id: int,
    audio_url: str,
    transcript: str,
    language: str,
) -> VoiceNote:
    vn = VoiceNote(
        lead_id=lead.id,
        telegram_user_id=telegram_user_id,
        telegram_message_id=telegram_message_id,
        audio_url=audio_url,
        transcript=transcript,
        language=language,
    )
    db.add(vn)
    await db.commit()
    await db.refresh(vn)
    logger.info("Saved voice note id=%d", vn.id)
    return vn


async def save_ai_report(db: AsyncSession, voice_note: VoiceNote, analysis: dict) -> AIReport:
    email = analysis.get("email", {})
    calendar = analysis.get("calendar", {})
    whatsapp = analysis.get("whatsapp", {})

    report = AIReport(
        voice_note_id=voice_note.id,
        conversation_summary=analysis.get("conversation_summary"),
        what_manager_said=analysis.get("what_manager_said"),
        mistakes_or_weak_points=analysis.get("mistakes_or_weak_points"),
        missing_questions=analysis.get("missing_questions"),
        recommended_next_step=analysis.get("recommended_next_step"),
        email_subject=email.get("subject"),
        email_body=email.get("body"),
        whatsapp_message=whatsapp.get("message"),
        calendar_title=calendar.get("title"),
        calendar_description=calendar.get("description"),
        calendar_start_time=calendar.get("start_time"),
        calendar_duration_minutes=calendar.get("duration_minutes", 15),
        confidence_score=analysis.get("confidence_score"),
        needs_human_review=analysis.get("needs_human_review", True),
        raw_json=analysis,
    )
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
) -> Action:
    action = Action(
        lead_id=lead.id,
        action_type=action_type,
        status="pending",
        payload=payload,
        approved_by_user=False,
    )
    db.add(action)
    await db.commit()
    await db.refresh(action)
    return action


async def update_action_status(
    db: AsyncSession,
    action: Action,
    status: str,
    approved: bool = False,
    executed_at: datetime | None = None,
) -> Action:
    action.status = status
    action.approved_by_user = approved
    if executed_at:
        action.executed_at = executed_at
    await db.commit()
    await db.refresh(action)
    return action
