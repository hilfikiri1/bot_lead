from __future__ import annotations

import asyncio
import html
import time
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.agent import audit, notion_gateway
from app.models.pending_agent_action import PendingAgentAction
from app.models.ai_report import AIReport
from app.models.lead import Lead
from app.models.voice_note import VoiceNote
from app.services import (
    calendar_event_builder,
    calendar_scheduling_service,
    crm_service,
    gmail_service,
    kommo_service,
    notion_service,
)
from app.services.calendar_event_builder import ScheduledEventDraft


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _lead_contact(lead: dict[str, Any]) -> dict[str, Any]:
    contacts = lead.get("contacts") or []
    return contacts[0] if contacts else {}


def _result_link(label: str, url: str | None) -> str:
    if not url:
        return ""
    return f'<a href="{html.escape(str(url), quote=True)}">{html.escape(label)}</a>'


async def execute_action(
    db: AsyncSession,
    *,
    action: PendingAgentAction,
    telegram_user_id: int,
) -> str:
    # Re-read with a row lock so two near-simultaneous Telegram callbacks cannot
    # start the same external write twice. The lock is released after status is
    # committed as `executing`; later callbacks then exit safely.
    locked_result = await db.execute(
        select(PendingAgentAction)
        .where(PendingAgentAction.id == int(action.id))
        .with_for_update()
    )
    action = locked_result.scalar_one_or_none()
    if action is None:
        return "⌛ Действие не найдено или уже удалено."
    if int(action.telegram_user_id) != int(telegram_user_id):
        raise PermissionError("Это действие принадлежит другому пользователю.")
    if action.status == "executed":
        return "ℹ️ Это действие уже было выполнено."
    if action.status == "executing":
        return "⏳ Это действие уже выполняется. Не нажимай кнопку повторно."
    if action.status == "rejected":
        return "ℹ️ Действие уже отменено."
    if action.status == "failed":
        return "⚠️ Действие ранее завершилось ошибкой. Создай его заново после проверки данных."
    if _aware(action.expires_at) < datetime.now(timezone.utc):
        action.status = "expired"
        await db.commit()
        return "⌛ Подтверждение устарело. Повтори команду."

    action.status = "executing"
    action.approved_at = datetime.now(timezone.utc)
    await db.commit()

    started = time.monotonic()
    try:
        result = await _execute(db, action)
        action.status = "executed"
        action.executed_at = datetime.now(timezone.utc)
        action.result = result.get("data") or {}
        action.error_message = None
        await db.commit()
        await audit.record_event(
            db,
            service="agent",
            operation=action.action_type,
            status="ok",
            external_id=str(action.id),
            telegram_user_id=telegram_user_id,
            duration_ms=int((time.monotonic() - started) * 1000),
            payload=action.payload,
            result=result.get("data") or {},
        )
        return str(result.get("text") or "✅ Выполнено.")
    except Exception as exc:
        action.status = "failed"
        action.error_message = str(exc)[:4000]
        await db.commit()
        await audit.record_event(
            db,
            service="agent",
            operation=action.action_type,
            status="error",
            external_id=str(action.id),
            telegram_user_id=telegram_user_id,
            duration_ms=int((time.monotonic() - started) * 1000),
            payload=action.payload,
            error_message=str(exc),
        )
        raise


async def _execute(db: AsyncSession, action: PendingAgentAction) -> dict[str, Any]:
    payload = dict(action.payload or {})
    action_type = action.action_type


    if action_type == "sync_call_analysis_to_notion":
        lead_id = int(payload["local_lead_id"])
        voice_note_id = int(payload["voice_note_id"])
        result = await db.execute(
            select(VoiceNote)
            .options(
                selectinload(VoiceNote.lead).selectinload(Lead.client),
                selectinload(VoiceNote.ai_report),
            )
            .where(VoiceNote.id == voice_note_id)
        )
        voice_note = result.scalar_one_or_none()
        if not voice_note or not voice_note.lead or not voice_note.ai_report:
            raise ValueError("Локальный анализ разговора не найден.")
        lead = voice_note.lead
        client = lead.client
        report: AIReport = voice_note.ai_report
        raw = report.raw_json or {}
        lead_title = str(
            (raw.get("lead") or {}).get("proposed_name")
            or lead.product_requested
            or "Новый запрос"
        )
        notion_result = await notion_service.sync_analyzed_call(
            client_id=client.id,
            client_name=client.name,
            client_company=client.company,
            client_phone=client.phone,
            client_email=client.email,
            client_language=client.language,
            client_notion_page_id=client.notion_page_id,
            lead_id=lead.id,
            lead_title=lead_title,
            lead_product=lead.product_requested,
            lead_budget=lead.budget,
            lead_country=lead.country,
            lead_city=lead.city,
            lead_kommo_url=lead.kommo_url,
            lead_kommo_id=lead.kommo_lead_id,
            lead_notion_page_id=lead.notion_page_id,
            voice_note_id=voice_note.id,
            transcript=voice_note.transcript,
            audio_url=voice_note.audio_url,
            analysis=raw,
        )
        await crm_service.save_notion_mapping(
            db,
            client_id=client.id,
            lead_id=lead.id,
            voice_note_id=voice_note.id,
            client_page_id=notion_result.client_page_id,
            lead_page_id=notion_result.lead_page_id,
            call_page_id=notion_result.call_page_id,
        )
        data = {
            "client_page_id": notion_result.client_page_id,
            "lead_page_id": notion_result.lead_page_id,
            "call_page_id": notion_result.call_page_id,
            "message": notion_result.message,
            "local_lead_id": lead_id,
        }
        return {
            "text": "✅ <b>Анализ разговора сохранён в Notion</b>\n\n"
            + html.escape(str(notion_result.message or "Готово")),
            "data": data,
        }

    if action_type == "add_kommo_note":
        lead_id = int(payload["lead_id"])
        result = await kommo_service.add_text_note(
            lead_id,
            str(payload["note_text"]),
            source="B&BS AI Agent",
        )
        return {
            "text": (
                "✅ <b>Примечание добавлено в Kommo</b>\n\n"
                f"Сделка: {html.escape(str(result.get('lead_name') or lead_id))}\n"
                + _result_link("Открыть сделку", result.get("url"))
            ),
            "data": result,
        }

    if action_type == "create_kommo_task":
        lead_id = int(payload["lead_id"])
        start_at, _ = calendar_event_builder.parse_natural_datetime(
            str(payload["due_at"]),
            duration_minutes=30,
        )
        result = await kommo_service.create_lead_task(
            lead_id=lead_id,
            text=str(payload.get("task_text") or "Связаться с клиентом")[:1000],
            complete_till=int(start_at.timestamp()),
        )
        return {
            "text": (
                "✅ <b>Задача создана в Kommo</b>\n\n"
                f"Сделка: {html.escape(str(result.get('lead_name') or lead_id))}\n"
                f"Срок: {html.escape(calendar_event_builder.format_date_ru(start_at))} "
                f"{start_at.astimezone(calendar_event_builder.manager_timezone()).strftime('%H:%M')}\n"
                + _result_link("Открыть сделку", result.get("url"))
            ),
            "data": result,
        }

    if action_type == "update_kommo_lead":
        lead_id = int(payload["lead_id"])
        fields = dict(payload.get("fields") or {})
        result = await kommo_service.update_kommo_lead(
            lead_id,
            name=fields.get("name"),
            price=fields.get("price"),
            status_id=fields.get("status_id"),
        )
        return {
            "text": (
                "✅ <b>Сделка обновлена</b>\n\n"
                f"{html.escape(str(result.get('lead_name') or lead_id))}\n"
                + _result_link("Открыть в Kommo", result.get("url"))
            ),
            "data": result,
        }

    if action_type == "create_calendar_event":
        lead_id = payload.get("lead_id")
        lead = await kommo_service.get_lead_details(int(lead_id)) if lead_id else {}
        contact = _lead_contact(lead)
        start_at, duration = calendar_event_builder.parse_natural_datetime(
            str(payload["due_at"]),
            duration_minutes=int(payload.get("duration_minutes") or 30),
        )
        event_type = str(payload.get("event_type") or "call")
        title = str(payload.get("title") or "").strip()
        if not title or len(title) > 180:
            title = calendar_event_builder.build_event_title(
                event_type, str(lead.get("name") or "") or None
            )
        description = calendar_event_builder.build_event_description(
            lead_name=lead.get("name"),
            kommo_lead_id=int(lead_id) if lead_id else None,
            lead_url=lead.get("url"),
            contact_name=contact.get("name"),
            contact_phone=(contact.get("phones") or [None])[0],
            contact_email=(contact.get("emails") or [None])[0],
            notes=str(payload.get("notes") or "") or None,
        )
        draft = ScheduledEventDraft(
            event_type=event_type,
            title=title[:255],
            description=description,
            start_at=start_at,
            duration_minutes=duration,
            reminder_minutes=int(payload.get("reminder_minutes") or 30),
            kommo_lead_id=int(lead_id) if lead_id else None,
            lead_name=lead.get("name"),
            lead_url=lead.get("url"),
            contact_name=contact.get("name"),
            contact_phone=(contact.get("phones") or [None])[0],
            contact_email=(contact.get("emails") or [None])[0],
        )
        result = await calendar_scheduling_service.schedule_confirmed_event(
            db,
            draft=draft,
            telegram_user_id=int(action.telegram_user_id),
            idempotency_key=action.idempotency_key,
        )
        success = result.get("calendar_success") or result.get("kommo_task_success")
        if not success:
            raise RuntimeError(
                str(result.get("calendar_error") or result.get("kommo_task_error") or "Calendar action failed")
            )
        lines = [
            "✅ <b>Событие запланировано</b>",
            "",
            html.escape(title),
            calendar_event_builder.format_time_range(draft.start_at, draft.end_at),
        ]
        if result.get("calendar_event_url"):
            lines.append(_result_link("Открыть событие", result.get("calendar_event_url")))
        if result.get("kommo_task_success"):
            lines.append("✅ Задача также создана в Kommo")
        return {"text": "\n".join(lines), "data": result}

    if action_type == "sync_leads_to_notion":
        leads = list(payload.get("leads") or [])
        result = await notion_gateway.sync_projects_from_kommo(
            leads, limit=int(payload.get("limit") or 50)
        )
        return {
            "text": (
                "✅ <b>Синхронизация Notion завершена</b>\n\n"
                f"Создано: <b>{result['created']}</b>\n"
                f"Обновлено: <b>{result['updated']}</b>\n"
                f"Ошибок: <b>{len(result['failed'])}</b>"
            ),
            "data": result,
        }

    if action_type == "save_draft_to_notion":
        lead = dict(payload["lead"])
        draft = dict(payload["draft"])
        result = await notion_gateway.save_generated_draft(lead=lead, draft=draft)
        record = result.get("record") or {}
        task = result.get("task") or {}
        links = [
            _result_link("Открыть документ", record.get("url")),
            _result_link("Открыть задачу", task.get("url")),
        ]
        return {
            "text": "✅ <b>Черновик сохранён в Notion</b>\n\n" + "\n".join(x for x in links if x),
            "data": result,
        }

    if action_type == "create_gmail_draft":
        draft_id = await asyncio.to_thread(
            gmail_service.create_draft,
            str(payload["to"]),
            str(payload["subject"]),
            str(payload["body"]),
        )
        result = {"draft_id": draft_id, "to": payload["to"]}
        return {
            "text": (
                "✅ <b>Черновик создан в Gmail</b>\n\n"
                f"Получатель: <code>{html.escape(str(payload['to']))}</code>\n"
                f"Draft ID: <code>{html.escape(str(draft_id))}</code>"
            ),
            "data": result,
        }

    raise ValueError(f"Unsupported agent action: {action_type}")
