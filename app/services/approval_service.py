"""Process Telegram inline-button approvals."""
from __future__ import annotations

import asyncio
import html
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models import Action, AIReport, Lead, VoiceNote
from app.services import (
    calendar_service,
    crm_service,
    gmail_service,
    kommo_service,
)
from app.services.telegram_service import send_message

logger = logging.getLogger(__name__)


def _pending_action_is_recent(action: Action, minutes: int = 10) -> bool:
    if action.status != "pending":
        return False
    created_at = action.created_at
    if created_at is None:
        return True
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    return datetime.now(tz=timezone.utc) - created_at < timedelta(minutes=minutes)


async def handle_callback(
    db: AsyncSession,
    callback_data: str,
    telegram_user_id: int,
    chat_id: int,
) -> str:
    parts = callback_data.split(":")
    if len(parts) not in {4, 5} or parts[0] != "action":
        return "Неизвестное действие."

    _, action_type, lead_id_str, voice_note_id_str, *extra = parts
    try:
        lead_id = int(lead_id_str)
        voice_note_id = int(voice_note_id_str)
        target_kommo_lead_id = int(extra[0]) if extra else None
    except ValueError:
        return "❌ Некорректные данные кнопки."

    voice_result = await db.execute(
        select(VoiceNote)
        .options(selectinload(VoiceNote.ai_report))
        .where(VoiceNote.id == voice_note_id)
    )
    voice_note = voice_result.scalar_one_or_none()
    if not voice_note or not voice_note.ai_report:
        return "❌ Отчёт не найден."

    report: AIReport = voice_note.ai_report
    lead_result = await db.execute(
        select(Lead)
        .options(selectinload(Lead.client))
        .where(Lead.id == lead_id)
    )
    lead = lead_result.scalar_one_or_none()
    if not lead:
        return "❌ Локальный лид не найден."

    if action_type == "cancel":
        return "❌ Отменено. Данные в Kommo не изменялись."
    if action_type == "gmail":
        return await _execute_gmail_draft(db, lead, report)
    if action_type == "calendar":
        return await _execute_calendar_event(db, lead, report)
    if action_type == "whatsapp":
        return await _execute_whatsapp_draft(db, lead, report, chat_id)
    if action_type == "kommo_create":
        return await _execute_kommo_create(db, lead, voice_note, report)
    if action_type == "kommo_update":
        if not target_kommo_lead_id:
            return "❌ Не указана сделка Kommo для обновления."
        return await _execute_kommo_update(
            db,
            lead,
            voice_note,
            report,
            target_kommo_lead_id,
        )
    # Backward compatibility for older Telegram reports.
    if action_type == "crm":
        return await _execute_local_crm_save(db, lead, report)
    if action_type == "edit":
        return "✏️ Редактирование пока выполняется напрямую в Kommo."

    return f"Неизвестный тип действия: {html.escape(action_type)}"


async def _execute_kommo_create(
    db: AsyncSession,
    lead: Lead,
    voice_note: VoiceNote,
    report: AIReport,
) -> str:
    """Create a Kommo lead once, only after the manager presses the button."""
    existing_result = await db.execute(
        select(Action)
        .where(
            Action.lead_id == lead.id,
            Action.action_type == "kommo_create",
            Action.status.in_(["pending", "executed"]),
        )
        .order_by(Action.id.desc())
    )
    existing = existing_result.scalars().first()
    if existing:
        payload = existing.payload or {}
        if existing.status == "executed" and payload.get("kommo_lead_id"):
            url = payload.get("kommo_url") or ""
            return (
                "ℹ️ Этот лид уже был добавлен в Kommo.\n"
                f"ID: <code>{payload['kommo_lead_id']}</code>\n"
                + (f"<a href=\"{html.escape(url, quote=True)}\">Открыть сделку</a>" if url else "")
            )
        if _pending_action_is_recent(existing):
            return "⏳ Создание лида в Kommo уже выполняется."
        # A previous deployment could leave a pending action forever after an
        # exception. Mark stale pending actions failed and allow a safe retry.
        if existing.status == "pending":
            existing.status = "failed"
            existing.payload = {
                **payload,
                "safe_error": "stale pending action automatically released",
            }
            await db.commit()

    action = await crm_service.create_action(
        db,
        lead,
        "kommo_create",
        {"source": "telegram_voice_note", "voice_note_id": voice_note.id},
    )
    # Save the scalar ID before any rollback. ORM attributes may be expired after
    # rollback and touching action.id can otherwise trigger MissingGreenlet.
    action_id = int(action.id)

    try:
        raw = report.raw_json or {}
        raw_client = raw.get("client") or {}
        raw_lead = raw.get("lead") or {}

        client_data = {
            "name": raw_client.get("name") or (lead.client.name if lead.client else None),
            "phone": raw_client.get("phone") or (lead.client.phone if lead.client else None),
            "email": raw_client.get("email") or (lead.client.email if lead.client else None),
            "company": raw_client.get("company") or (lead.client.company if lead.client else None),
            "language": raw_client.get("language") or (lead.client.language if lead.client else None),
        }
        lead_data = {
            "product_requested": raw_lead.get("product_requested") or lead.product_requested,
            "budget": raw_lead.get("budget") or lead.budget,
            "country": raw_lead.get("country") or lead.country,
            "city": raw_lead.get("city") or lead.city,
            "urgency": raw_lead.get("urgency") or lead.priority,
            "status": raw_lead.get("status") or lead.status,
        }

        created = await kommo_service.create_lead_from_analysis(
            client_data=client_data,
            lead_data=lead_data,
            conversation_summary=report.conversation_summary,
            recommended_next_step=report.recommended_next_step,
            missing_questions=report.missing_questions or [],
            transcript=voice_note.transcript,
        )

        action.payload = {
            **(action.payload or {}),
            "kommo_lead_id": created["lead_id"],
            "kommo_contact_id": created.get("contact_id"),
            "kommo_url": created.get("url"),
            "note_saved": created.get("note_saved", False),
        }
        await crm_service.update_action_status(
            db,
            action,
            "executed",
            approved=True,
            executed_at=datetime.now(tz=timezone.utc),
        )

        note_text = "Заметка добавлена" if created.get("note_saved") else "сделка создана, но заметка не добавилась"
        return (
            "✅ <b>Лид добавлен в Kommo</b>\n\n"
            f"Название: {html.escape(str(created.get('lead_name') or '—'))}\n"
            f"ID: <code>{created['lead_id']}</code>\n"
            f"Статус: {note_text}\n"
            f"<a href=\"{html.escape(created['url'], quote=True)}\">Открыть сделку в Kommo</a>"
        )
    except Exception as exc:
        logger.exception("Kommo lead creation failed")
        await db.rollback()
        # Re-load after rollback before changing action state.
        refreshed = await db.get(Action, action_id)
        if refreshed:
            refreshed.status = "failed"
            refreshed.payload = {
                **(refreshed.payload or {}),
                "safe_error": str(exc)[:500],
            }
            await db.commit()
        return f"❌ Не удалось создать лид в Kommo: {html.escape(str(exc))}"


async def _execute_kommo_update(
    db: AsyncSession,
    lead: Lead,
    voice_note: VoiceNote,
    report: AIReport,
    target_kommo_lead_id: int,
) -> str:
    """Append a reviewed second-call report to an existing Kommo lead once."""
    existing_result = await db.execute(
        select(Action)
        .where(
            Action.lead_id == lead.id,
            Action.action_type == "kommo_update",
            Action.status.in_(["pending", "executed"]),
        )
        .order_by(Action.id.desc())
    )
    existing = existing_result.scalars().first()
    if existing:
        payload = existing.payload or {}
        if existing.status == "executed":
            url = payload.get("kommo_url") or ""
            return (
                "ℹ️ Этот разговор уже добавлен в выбранную сделку Kommo.\n"
                f"ID: <code>{payload.get('kommo_lead_id') or target_kommo_lead_id}</code>\n"
                + (f"<a href=\"{html.escape(url, quote=True)}\">Открыть сделку</a>" if url else "")
            )
        if _pending_action_is_recent(existing):
            return "⏳ Обновление сделки Kommo уже выполняется."
        if existing.status == "pending":
            existing.status = "failed"
            existing.payload = {
                **payload,
                "safe_error": "stale pending action automatically released",
            }
            await db.commit()

    action = await crm_service.create_action(
        db,
        lead,
        "kommo_update",
        {
            "source": "telegram_followup_audio",
            "voice_note_id": voice_note.id,
            "kommo_lead_id": target_kommo_lead_id,
        },
    )
    action_id = int(action.id)

    try:
        raw = report.raw_json or {}
        updated = await kommo_service.add_followup_note_from_analysis(
            lead_id=target_kommo_lead_id,
            conversation_summary=report.conversation_summary,
            recommended_next_step=report.recommended_next_step,
            missing_questions=report.missing_questions or [],
            transcript=voice_note.transcript,
            client_data=raw.get("client") or {},
            lead_data=raw.get("lead") or {},
        )

        action.payload = {
            **(action.payload or {}),
            "kommo_lead_id": target_kommo_lead_id,
            "kommo_url": updated.get("url"),
        }
        await crm_service.update_action_status(
            db,
            action,
            "executed",
            approved=True,
            executed_at=datetime.now(tz=timezone.utc),
        )
        return (
            "✅ <b>Разговор добавлен в существующую сделку Kommo</b>\n\n"
            f"Сделка: {html.escape(str(updated.get('lead_name') or '—'))}\n"
            f"ID: <code>{target_kommo_lead_id}</code>\n"
            "Добавлено: резюме, следующий шаг, вопросы и транскрипт.\n"
            f"<a href=\"{html.escape(updated['url'], quote=True)}\">Открыть сделку в Kommo</a>"
        )
    except Exception as exc:
        logger.exception("Kommo lead update failed")
        await db.rollback()
        refreshed = await db.get(Action, action_id)
        if refreshed:
            refreshed.status = "failed"
            refreshed.payload = {
                **(refreshed.payload or {}),
                "safe_error": str(exc)[:500],
            }
            await db.commit()
        return f"❌ Не удалось добавить разговор в Kommo: {html.escape(str(exc))}"


async def _execute_gmail_draft(db: AsyncSession, lead: Lead, report: AIReport) -> str:
    action = await crm_service.create_action(
        db,
        lead,
        "gmail_draft",
        {"subject": report.email_subject, "body": report.email_body},
    )
    try:
        to_email = lead.client.email if lead.client and lead.client.email else ""
        if not to_email:
            return "⚠️ Email клиента не найден."
        draft_id = gmail_service.create_draft(
            to=to_email,
            subject=report.email_subject or "(без темы)",
            body=report.email_body or "",
        )
        await crm_service.update_action_status(
            db,
            action,
            "executed",
            approved=True,
            executed_at=datetime.now(tz=timezone.utc),
        )
        return f"✅ Черновик Gmail создан (ID: {html.escape(str(draft_id))})."
    except Exception as exc:
        logger.error("Gmail draft failed: %s", exc)
        await crm_service.update_action_status(db, action, "failed")
        return f"❌ Не удалось создать черновик Gmail: {html.escape(str(exc))}"


async def _execute_calendar_event(db: AsyncSession, lead: Lead, report: AIReport) -> str:
    action = await crm_service.create_action(
        db,
        lead,
        "calendar_event",
        {
            "title": report.calendar_title,
            "description": report.calendar_description,
            "start_time": report.calendar_start_time,
        },
    )
    try:
        event_id = await asyncio.to_thread(
            calendar_service.create_event,
            report.calendar_title or "Follow-up call",
            report.calendar_description or "",
            report.calendar_start_time,
            report.calendar_duration_minutes or 15,
        )
        await crm_service.update_action_status(
            db,
            action,
            "executed",
            approved=True,
            executed_at=datetime.now(tz=timezone.utc),
        )
        return (
            f"✅ Событие создано в {html.escape(calendar_service.provider_label())} "
            f"(ID: {html.escape(str(event_id))})."
        )
    except Exception as exc:
        logger.error("Calendar event failed: %s", exc)
        await crm_service.update_action_status(db, action, "failed")
        return f"❌ Не удалось создать событие: {html.escape(str(exc))}"


async def _execute_whatsapp_draft(
    db: AsyncSession,
    lead: Lead,
    report: AIReport,
    chat_id: int,
) -> str:
    action = await crm_service.create_action(
        db,
        lead,
        "whatsapp_draft",
        {"message": report.whatsapp_message},
    )
    try:
        message = html.escape(report.whatsapp_message or "(пусто)")
        await send_message(
            chat_id=chat_id,
            text=(
                "💬 <b>Черновик WhatsApp — клиенту не отправлен</b>\n\n"
                f"<pre>{message}</pre>\n\n"
                "<i>Проверьте и отправьте вручную.</i>"
            ),
        )
        await crm_service.update_action_status(
            db,
            action,
            "executed",
            approved=True,
            executed_at=datetime.now(tz=timezone.utc),
        )
        return "✅ Черновик отправлен вам для проверки."
    except Exception as exc:
        logger.error("WhatsApp draft relay failed: %s", exc)
        await crm_service.update_action_status(db, action, "failed")
        return f"❌ Не удалось показать черновик: {html.escape(str(exc))}"


async def _execute_local_crm_save(db: AsyncSession, lead: Lead, report: AIReport) -> str:
    """Backward-compatible local-only action for old Telegram messages."""
    action = await crm_service.create_action(
        db,
        lead,
        "crm_save",
        {"recommended_next_step": report.recommended_next_step},
    )
    try:
        lead.next_action = report.recommended_next_step
        lead.status = "follow_up"
        await db.commit()
        await crm_service.update_action_status(
            db,
            action,
            "executed",
            approved=True,
            executed_at=datetime.now(tz=timezone.utc),
        )
        return "✅ Локальный лид обновлён. В Kommo ничего не создавалось."
    except Exception as exc:
        logger.error("Local CRM save failed: %s", exc)
        await crm_service.update_action_status(db, action, "failed")
        return f"❌ Локальное сохранение не удалось: {html.escape(str(exc))}"
