"""Human-confirmed actions executed from Telegram."""

from __future__ import annotations

import asyncio
import html
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models import Action, AIReport, Lead, VoiceNote
from app.services import calendar_service, crm_service, gmail_service, kommo_service
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


async def _load_context(
    db: AsyncSession,
    lead_id: int,
    voice_note_id: int,
) -> tuple[Lead, VoiceNote, AIReport]:
    voice_result = await db.execute(
        select(VoiceNote)
        .options(selectinload(VoiceNote.ai_report))
        .where(VoiceNote.id == voice_note_id)
    )
    voice_note = voice_result.scalar_one_or_none()
    if not voice_note or not voice_note.ai_report:
        raise ValueError("Отчёт анализа не найден.")

    lead_result = await db.execute(
        select(Lead).options(selectinload(Lead.client)).where(Lead.id == lead_id)
    )
    lead = lead_result.scalar_one_or_none()
    if not lead:
        raise ValueError("Локальный лид не найден.")
    return lead, voice_note, voice_note.ai_report


async def build_kommo_creation_draft(
    db: AsyncSession,
    *,
    lead_id: int,
    voice_note_id: int,
) -> dict[str, Any]:
    """Build a safe editable preview before any Kommo write operation."""
    lead, voice_note, report = await _load_context(db, lead_id, voice_note_id)
    raw = report.raw_json or {}
    raw_client = raw.get("client") or {}
    raw_lead = raw.get("lead") or {}
    placement = await kommo_service.get_default_lead_placement_preview()

    product = str(
        raw_lead.get("product_requested") or lead.product_requested or "Новый запрос"
    ).strip()
    proposed = str(raw_lead.get("proposed_name") or product).strip()[:255]
    number = str(raw_lead.get("lead_number") or "").strip()
    title = f"{number} {proposed}".strip() if number else proposed

    client = lead.client
    return {
        "local_lead_id": lead.id,
        "voice_note_id": voice_note.id,
        "lead_number": number or None,
        "lead_name": title[:255],
        "client_name": raw_client.get("name") or (client.name if client else None),
        "company": raw_client.get("company") or (client.company if client else None),
        "phone": raw_client.get("phone") or (client.phone if client else None),
        "email": raw_client.get("email") or (client.email if client else None),
        "language": raw_client.get("language") or (client.language if client else None),
        "product_requested": product,
        "budget": raw_lead.get("budget") or lead.budget,
        "country": raw_lead.get("country") or lead.country,
        "city": raw_lead.get("city") or lead.city,
        "next_step": report.recommended_next_step or lead.next_action,
        "pipeline_id": placement.get("pipeline_id"),
        "pipeline_name": placement.get("pipeline_name"),
        "status_id": placement.get("status_id"),
        "status_name": placement.get("status_name"),
    }


def _safe_creation_error(exc: Exception) -> str:
    if isinstance(exc, kommo_service.KommoAPIError):
        code = f" (HTTP {exc.status_code})" if exc.status_code else ""
        if exc.status_code == 400:
            return (
                f"Kommo отклонил данные{code}. Проверьте название, воронку и этап. "
                "Техническая ошибка сохранена в журнале."
            )
        if exc.status_code == 401:
            return f"Токен Kommo недействителен или истёк{code}."
        if exc.status_code == 403:
            return f"Интеграции не хватает прав на создание сделок{code}."
        if exc.status_code == 429:
            return (
                f"Kommo временно ограничил количество запросов{code}. Повторите позже."
            )
        return f"Kommo не выполнил операцию{code}."
    return "Не удалось создать лид. Технические детали сохранены в Railway Logs."


async def execute_kommo_create_from_draft(
    db: AsyncSession,
    *,
    lead_id: int,
    voice_note_id: int,
    draft: dict[str, Any],
    telegram_user_id: int | None = None,
) -> str:
    """Create exactly one Kommo lead from an explicitly confirmed draft."""
    lead, voice_note, report = await _load_context(db, lead_id, voice_note_id)

    if lead.kommo_lead_id:
        url = lead.kommo_url or ""
        return (
            "ℹ️ <b>Этот разговор уже связан с лидом Kommo</b>\n\n"
            f"ID: <code>{lead.kommo_lead_id}</code>\n"
            + (
                f'<a href="{html.escape(url, quote=True)}">Открыть сделку</a>'
                if url
                else ""
            )
        )

    idempotency_key = f"kommo_create:voice_note:{voice_note.id}"
    action = await crm_service.create_action(
        db,
        lead,
        "kommo_create",
        {
            "source": "telegram_voice_note",
            "voice_note_id": voice_note.id,
            "telegram_user_id": telegram_user_id,
            "draft": draft,
        },
        idempotency_key=idempotency_key,
    )
    action_id = int(action.id)

    if action.status == "executed":
        payload = action.payload or {}
        return (
            "ℹ️ <b>Лид уже создан</b>\n\n"
            f"ID: <code>{payload.get('kommo_lead_id') or '—'}</code>\n"
            + (
                f'<a href="{html.escape(str(payload.get("kommo_url")), quote=True)}">Открыть сделку</a>'
                if payload.get("kommo_url")
                else ""
            )
        )
    if (
        action.status == "pending"
        and _pending_action_is_recent(action)
        and (action.payload or {}).get("started_at")
    ):
        return "⏳ Создание лида уже выполняется. Подождите несколько секунд."

    action.status = "pending"
    action.approved_by_user = True
    action.error_message = None
    action.payload = {
        **(action.payload or {}),
        "draft": draft,
        "started_at": datetime.now(tz=timezone.utc).isoformat(),
    }
    await db.commit()

    try:
        raw = report.raw_json or {}
        raw_client = raw.get("client") or {}
        raw_lead = raw.get("lead") or {}
        client_data = {
            "name": draft.get("client_name") or raw_client.get("name"),
            "phone": draft.get("phone") or raw_client.get("phone"),
            "email": draft.get("email") or raw_client.get("email"),
            "company": draft.get("company") or raw_client.get("company"),
            "language": draft.get("language") or raw_client.get("language"),
        }
        lead_data = {
            **raw_lead,
            "lead_number": draft.get("lead_number"),
            "proposed_name": draft.get("lead_name"),
            "product_requested": draft.get("product_requested")
            or lead.product_requested,
            "budget": draft.get("budget") or lead.budget,
            "country": draft.get("country") or lead.country,
            "city": draft.get("city") or lead.city,
            "urgency": raw_lead.get("urgency") or lead.priority,
            "status": raw_lead.get("status") or lead.status,
        }

        created = await kommo_service.create_lead_from_analysis(
            client_data=client_data,
            lead_data=lead_data,
            conversation_summary=report.conversation_summary,
            recommended_next_step=draft.get("next_step")
            or report.recommended_next_step,
            missing_questions=report.missing_questions or [],
            transcript=voice_note.transcript,
            lead_name_override=str(draft.get("lead_name") or "").strip() or None,
        )

        refreshed = await db.get(Action, action_id)
        if not refreshed:
            raise RuntimeError("Не удалось обновить журнал действия.")
        refreshed.payload = {
            **(refreshed.payload or {}),
            "kommo_lead_id": created["lead_id"],
            "kommo_contact_id": created.get("contact_id"),
            "kommo_url": created.get("url"),
            "pipeline_id": created.get("pipeline_id"),
            "status_id": created.get("status_id"),
            "note_saved": created.get("note_saved", False),
        }
        lead.product_requested = (
            draft.get("product_requested") or lead.product_requested
        )
        lead.budget = draft.get("budget") or lead.budget
        lead.country = draft.get("country") or lead.country
        lead.city = draft.get("city") or lead.city
        lead.next_action = draft.get("next_step") or lead.next_action
        if lead.client:
            lead.client.name = draft.get("client_name") or lead.client.name
            lead.client.company = draft.get("company") or lead.client.company

        # Save the external IDs before marking the action executed. If the process
        # stops after Kommo creates the lead, the local mapping prevents a duplicate.
        await crm_service.save_kommo_mapping(
            db,
            lead_id=lead.id,
            kommo_lead_id=created["lead_id"],
            kommo_contact_id=created.get("contact_id"),
            pipeline_id=created.get("pipeline_id"),
            status_id=created.get("status_id"),
            url=created.get("url"),
        )
        refreshed = await db.get(Action, action_id)
        if refreshed:
            await crm_service.update_action_status(
                db,
                refreshed,
                "executed",
                approved=True,
                executed_at=datetime.now(tz=timezone.utc),
            )

        note_status = (
            "✅ примечание добавлено"
            if created.get("note_saved")
            else "⚠️ сделка создана, примечание не добавлено"
        )
        return (
            "✅ <b>Лид создан в Kommo</b>\n\n"
            f"<b>{html.escape(str(created.get('lead_name') or draft.get('lead_name') or '—'))}</b>\n"
            f"ID: <code>{created['lead_id']}</code>\n"
            f"Воронка: {html.escape(str(created.get('pipeline_name') or '—'))}\n"
            f"Этап: {html.escape(str(created.get('status_name') or '—'))}\n"
            f"Результат: {note_status}\n\n"
            f'<a href="{html.escape(str(created.get("url") or ""), quote=True)}">Открыть сделку в Kommo</a>'
        )
    except Exception as exc:
        logger.exception("Kommo lead creation failed")
        await db.rollback()
        refreshed = await db.get(Action, action_id)
        if refreshed:
            await crm_service.update_action_status(
                db,
                refreshed,
                "failed",
                approved=True,
                error_message=str(exc),
            )
        return f"❌ <b>Лид не создан</b>\n\n{html.escape(_safe_creation_error(exc))}"


async def handle_callback(
    db: AsyncSession,
    callback_data: str,
    telegram_user_id: int,
    chat_id: int,
) -> str:
    parts = callback_data.split(":")
    if len(parts) not in {4, 5} or parts[0] != "action":
        return "Неизвестное действие."

    _, action_type, lead_id_raw, voice_note_id_raw, *extra = parts
    try:
        lead_id = int(lead_id_raw)
        voice_note_id = int(voice_note_id_raw)
        target_kommo_lead_id = int(extra[0]) if extra else None
    except ValueError:
        return "❌ Некорректные данные кнопки."

    if action_type == "cancel":
        return "❌ Отменено. Данные в Kommo не изменялись."

    try:
        lead, voice_note, report = await _load_context(db, lead_id, voice_note_id)
    except ValueError as exc:
        return f"❌ {html.escape(str(exc))}"
    if action_type == "gmail":
        return await _execute_gmail_draft(db, lead, report)
    if action_type == "calendar":
        return await _execute_calendar_event(db, lead, report)
    if action_type == "whatsapp":
        return await _execute_whatsapp_draft(db, lead, report, chat_id)
    if action_type == "kommo_create":
        draft = await build_kommo_creation_draft(
            db, lead_id=lead_id, voice_note_id=voice_note_id
        )
        return await execute_kommo_create_from_draft(
            db,
            lead_id=lead_id,
            voice_note_id=voice_note_id,
            draft=draft,
            telegram_user_id=telegram_user_id,
        )
    if action_type == "kommo_update":
        if not target_kommo_lead_id:
            return "❌ Не указана сделка Kommo для обновления."
        return await _execute_kommo_update(
            db, lead, voice_note, report, target_kommo_lead_id
        )
    if action_type == "crm":
        return await _execute_local_crm_save(db, lead, report)
    return f"Неизвестный тип действия: {html.escape(action_type)}"


async def _execute_kommo_update(
    db: AsyncSession,
    lead: Lead,
    voice_note: VoiceNote,
    report: AIReport,
    target_kommo_lead_id: int,
) -> str:
    key = f"kommo_update:voice_note:{voice_note.id}:lead:{target_kommo_lead_id}"
    action = await crm_service.create_action(
        db,
        lead,
        "kommo_update",
        {
            "source": "telegram_followup_audio",
            "voice_note_id": voice_note.id,
            "kommo_lead_id": target_kommo_lead_id,
        },
        idempotency_key=key,
    )
    action_id = int(action.id)
    if action.status == "executed":
        url = (action.payload or {}).get("kommo_url") or ""
        return (
            "ℹ️ Этот разговор уже добавлен в выбранную сделку.\n"
            f"ID: <code>{target_kommo_lead_id}</code>\n"
            + (
                f'<a href="{html.escape(url, quote=True)}">Открыть сделку</a>'
                if url
                else ""
            )
        )
    if (
        action.status == "pending"
        and _pending_action_is_recent(action)
        and (action.payload or {}).get("started_at")
    ):
        return "⏳ Обновление сделки уже выполняется."

    action.payload = {
        **(action.payload or {}),
        "started_at": datetime.now(tz=timezone.utc).isoformat(),
    }
    action.status = "pending"
    await db.commit()

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
        refreshed = await db.get(Action, action_id)
        if refreshed:
            refreshed.payload = {
                **(refreshed.payload or {}),
                "kommo_url": updated.get("url"),
            }
            await crm_service.update_action_status(
                db,
                refreshed,
                "executed",
                approved=True,
                executed_at=datetime.now(tz=timezone.utc),
            )
        return (
            "✅ <b>Разговор добавлен в сделку</b>\n\n"
            f"Сделка: {html.escape(str(updated.get('lead_name') or '—'))}\n"
            f"ID: <code>{target_kommo_lead_id}</code>\n"
            "Добавлены резюме, следующий шаг, вопросы и транскрипт.\n\n"
            f'<a href="{html.escape(str(updated.get("url") or ""), quote=True)}">Открыть сделку в Kommo</a>'
        )
    except Exception as exc:
        logger.exception("Kommo lead update failed")
        await db.rollback()
        refreshed = await db.get(Action, action_id)
        if refreshed:
            await crm_service.update_action_status(
                db, refreshed, "failed", approved=True, error_message=str(exc)
            )
        return (
            "❌ Не удалось добавить разговор в Kommo. Подробности сохранены в журнале."
        )


async def _execute_gmail_draft(db: AsyncSession, lead: Lead, report: AIReport) -> str:
    action = await crm_service.create_action(
        db,
        lead,
        "gmail_draft",
        {"subject": report.email_subject, "body": report.email_body},
        idempotency_key=f"gmail_draft:report:{report.id}",
    )
    if action.status == "executed":
        return "ℹ️ Черновик Gmail для этого отчёта уже создан."
    try:
        to_email = lead.client.email if lead.client and lead.client.email else ""
        if not to_email:
            await crm_service.update_action_status(
                db, action, "failed", error_message="Client email is missing"
            )
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
        await crm_service.update_action_status(
            db, action, "failed", error_message=str(exc)
        )
        return "❌ Не удалось создать черновик Gmail."


async def _execute_calendar_event(
    db: AsyncSession, lead: Lead, report: AIReport
) -> str:
    action = await crm_service.create_action(
        db,
        lead,
        "calendar_event",
        {
            "title": report.calendar_title,
            "description": report.calendar_description,
            "start_time": report.calendar_start_time,
        },
        idempotency_key=f"calendar_event:report:{report.id}",
    )
    if action.status == "executed":
        return "ℹ️ Событие для этого отчёта уже создано."
    try:
        event_id = await asyncio.to_thread(
            calendar_service.create_event,
            report.calendar_title or "Повторный контакт",
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
        await crm_service.update_action_status(
            db, action, "failed", error_message=str(exc)
        )
        return (
            "❌ Не удалось создать событие. Откройте раздел календаря для диагностики."
        )


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
        idempotency_key=f"whatsapp_draft:report:{report.id}",
    )
    try:
        await send_message(
            chat_id=chat_id,
            text=(
                "💬 <b>Сообщение клиенту — не отправлено</b>\n\n"
                f"<pre>{html.escape(report.whatsapp_message or '(пусто)')}</pre>\n\n"
                "<i>Скопируйте, проверьте и отправьте вручную.</i>"
            ),
        )
        await crm_service.update_action_status(
            db,
            action,
            "executed",
            approved=True,
            executed_at=datetime.now(tz=timezone.utc),
        )
        return "✅ Польский текст показан отдельным сообщением."
    except Exception as exc:
        await crm_service.update_action_status(
            db, action, "failed", error_message=str(exc)
        )
        return "❌ Не удалось показать черновик."


async def _execute_local_crm_save(
    db: AsyncSession, lead: Lead, report: AIReport
) -> str:
    action = await crm_service.create_action(
        db,
        lead,
        "crm_save",
        {"recommended_next_step": report.recommended_next_step},
        idempotency_key=f"local_crm_save:report:{report.id}",
    )
    action_id = int(action.id)
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
        await db.rollback()
        refreshed = await db.get(Action, action_id)
        if refreshed:
            await crm_service.update_action_status(
                db, refreshed, "failed", error_message=str(exc)
            )
        return "❌ Локальное сохранение не удалось."
