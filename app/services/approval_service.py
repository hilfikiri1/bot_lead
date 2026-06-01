"""
approval_service.py
Processes inline button callbacks from Telegram.
Routes approved actions to the correct services.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.models import Action, Lead, VoiceNote, AIReport
from app.services import gmail_service, calendar_service, whatsapp_service, crm_service
from app.services.telegram_service import send_message

logger = logging.getLogger(__name__)


async def handle_callback(
    db: AsyncSession,
    callback_data: str,
    telegram_user_id: int,
    chat_id: int,
) -> str:
    """
    Parse callback_data like 'action:gmail:3:7' and execute the approved action.
    Returns a human-readable result message.
    """
    parts = callback_data.split(":")
    if len(parts) != 4 or parts[0] != "action":
        return "Unknown action."

    _, action_type, lead_id_str, voice_note_id_str = parts
    lead_id = int(lead_id_str)
    voice_note_id = int(voice_note_id_str)

    # Load voice note + report
    result = await db.execute(
        select(VoiceNote).where(VoiceNote.id == voice_note_id)
    )
    voice_note = result.scalar_one_or_none()
    if not voice_note or not voice_note.ai_report:
        return "❌ Report not found."

    report: AIReport = voice_note.ai_report
    lead_result = await db.execute(select(Lead).where(Lead.id == lead_id))
    lead = lead_result.scalar_one_or_none()

    if action_type == "cancel":
        return "❌ Cancelled. No actions were taken."

    if action_type == "gmail":
        return await _execute_gmail_draft(db, lead, report)

    if action_type == "calendar":
        return await _execute_calendar_event(db, lead, report)

    if action_type == "whatsapp":
        return await _execute_whatsapp_draft(db, lead, report, chat_id, telegram_user_id)

    if action_type == "crm":
        return await _execute_crm_save(db, lead, report)

    if action_type == "edit":
        return "✏️ Edit feature coming soon. Please edit directly in the CRM."

    return f"Unknown action type: {action_type}"


async def _execute_gmail_draft(db, lead, report: AIReport) -> str:
    action = await crm_service.create_action(
        db, lead, "gmail_draft",
        {"subject": report.email_subject, "body": report.email_body},
    )
    try:
        client = lead.client if lead else None
        to_email = client.email if client and client.email else ""
        if not to_email:
            return "⚠️ No client email found. Draft could not be created."
        draft_id = gmail_service.create_draft(
            to=to_email,
            subject=report.email_subject or "(no subject)",
            body=report.email_body or "",
        )
        await crm_service.update_action_status(
            db, action, "executed", approved=True, executed_at=datetime.now(tz=timezone.utc)
        )
        return f"✅ Gmail draft created (ID: {draft_id}). Check your Drafts folder."
    except Exception as e:
        logger.error("Gmail draft failed: %s", e)
        await crm_service.update_action_status(db, action, "failed")
        return f"❌ Gmail draft failed: {e}"


async def _execute_calendar_event(db, lead, report: AIReport) -> str:
    action = await crm_service.create_action(
        db, lead, "calendar_event",
        {
            "title": report.calendar_title,
            "description": report.calendar_description,
            "start_time": report.calendar_start_time,
        },
    )
    try:
        event_id = calendar_service.create_event(
            title=report.calendar_title or "Follow-up call",
            description=report.calendar_description or "",
            start_time_iso=report.calendar_start_time,
            duration_minutes=report.calendar_duration_minutes or 15,
        )
        await crm_service.update_action_status(
            db, action, "executed", approved=True, executed_at=datetime.now(tz=timezone.utc)
        )
        return f"✅ Calendar event created (ID: {event_id})."
    except Exception as e:
        logger.error("Calendar event failed: %s", e)
        await crm_service.update_action_status(db, action, "failed")
        return f"❌ Calendar event failed: {e}"


async def _execute_whatsapp_draft(db, lead, report: AIReport, chat_id: int, user_id: int) -> str:
    """Send the WhatsApp draft text to the manager's own Telegram chat for review."""
    action = await crm_service.create_action(
        db, lead, "whatsapp_draft",
        {"message": report.whatsapp_message},
    )
    try:
        msg = report.whatsapp_message or "(empty)"
        # Send draft to manager via Telegram for review (NOT to the client)
        await send_message(
            chat_id=chat_id,
            text=(
                f"💬 <b>WhatsApp Draft</b> (for your review — NOT sent to client)\n\n"
                f"<pre>{msg}</pre>\n\n"
                f"<i>Copy and send manually when ready.</i>"
            ),
        )
        await crm_service.update_action_status(
            db, action, "executed", approved=True, executed_at=datetime.now(tz=timezone.utc)
        )
        return "✅ WhatsApp draft sent to you for review."
    except Exception as e:
        logger.error("WhatsApp draft relay failed: %s", e)
        await crm_service.update_action_status(db, action, "failed")
        return f"❌ Failed to send WhatsApp draft: {e}"


async def _execute_crm_save(db, lead, report: AIReport) -> str:
    """Update lead status and next action from AI report."""
    action = await crm_service.create_action(
        db, lead, "crm_save",
        {"recommended_next_step": report.recommended_next_step},
    )
    try:
        if lead:
            lead.next_action = report.recommended_next_step
            lead.status = "follow_up"
            await db.commit()
        await crm_service.update_action_status(
            db, action, "executed", approved=True, executed_at=datetime.now(tz=timezone.utc)
        )
        return "✅ Lead saved to CRM with next action updated."
    except Exception as e:
        logger.error("CRM save failed: %s", e)
        await crm_service.update_action_status(db, action, "failed")
        return f"❌ CRM save failed: {e}"
