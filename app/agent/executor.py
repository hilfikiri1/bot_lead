from __future__ import annotations

import asyncio
import html
import logging
import time
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.agent import audit, notion_gateway, project_drive, project_linking
from app.agent.security import sanitize_text
from app.models.pending_agent_action import PendingAgentAction
from app.models.ai_report import AIReport
from app.models.lead import Lead
from app.models.voice_note import VoiceNote
from app.services import (
    calendar_event_builder,
    calendar_scheduling_service,
    client_message_service,
    crm_service,
    gmail_service,
    google_drive_service,
    kommo_service,
    notion_service,
    project_link_service,
    project_artifact_service,
    storage_service,
)
from app.services.calendar_event_builder import ScheduledEventDraft

logger = logging.getLogger(__name__)

# If a worker dies after marking the row `executing`, recover after this window so
# the manager can stage a fresh confirmation instead of being stuck forever.
_STALE_EXECUTING_SECONDS = 120


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
        started_at = action.approved_at or action.updated_at
        if started_at is not None:
            age = (datetime.now(timezone.utc) - _aware(started_at)).total_seconds()
            if age < _STALE_EXECUTING_SECONDS:
                return "⏳ Это действие уже выполняется. Не нажимай кнопку повторно."
            action.status = "failed"
            action.error_message = sanitize_text(
                "Исполнение прервано (таймаут executing). Проверь внешний сервис "
                "перед повторной командой."
            )
            await db.commit()
            return (
                "⚠️ Предыдущее выполнение зависло и помечено как ошибка. "
                "Проверь Kommo/Notion/Gmail/Calendar и при необходимости повтори команду."
            )
        return "⏳ Это действие уже выполняется. Не нажимай кнопку повторно."
    if action.status == "rejected":
        return "ℹ️ Действие уже отменено."
    if action.status == "failed":
        if not (
            action.action_type.endswith("_batch")
            and _batch_has_partial_success(action)
        ):
            return "⚠️ Действие ранее завершилось ошибкой. Создай его заново после проверки данных."
    if _aware(action.expires_at) < datetime.now(timezone.utc):
        action.status = "expired"
        await db.commit()
        if action.action_type == "save_file_to_drive_project":
            artifact_id = (action.payload or {}).get("artifact_id")
            if artifact_id:
                artifact = await project_artifact_service.get_artifact(
                    db, int(artifact_id)
                )
                if artifact is not None and artifact.status == "pending":
                    storage_path = artifact.storage_path
                    await project_artifact_service.mark_cancelled(
                        db, artifact=artifact, status="expired"
                    )
                    if storage_path:
                        try:
                            await asyncio.to_thread(
                                storage_service.delete_project_file,
                                str(storage_path),
                            )
                        except Exception as exc:
                            # Expiry must remain final even if temporary storage
                            # cleanup needs later operational attention.
                            logger.warning(
                                "Could not clean expired project artifact %s: %s",
                                artifact.id,
                                exc,
                            )
        return "⌛ Подтверждение устарело. Повтори команду."

    action.status = "executing"
    action.approved_at = datetime.now(timezone.utc)
    action.approved_by_telegram_user_id = int(telegram_user_id)
    await db.commit()

    started = time.monotonic()
    try:
        result = await _execute(db, action)
        partial_failed = bool(result.get("partial_failed"))
        action.status = "failed" if partial_failed else "executed"
        action.executed_at = datetime.now(timezone.utc)
        action.executed_by_telegram_user_id = int(telegram_user_id)
        action.result = result.get("data") or {}
        action.error_message = sanitize_text(result.get("error_message"), limit=4000) if partial_failed else None
        await db.commit()
        await audit.record_event(
            db,
            service="agent",
            operation=action.action_type,
            status="error" if partial_failed else "ok",
            external_id=str(action.id),
            telegram_user_id=telegram_user_id,
            duration_ms=int((time.monotonic() - started) * 1000),
            payload=action.payload,
            result=result.get("data") or {},
        )
        return str(result.get("text") or "✅ Выполнено.")
    except Exception as exc:
        action.status = "failed"
        action.error_message = sanitize_text(str(exc), limit=4000)
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


def _batch_item_results(action: PendingAgentAction) -> dict[str, Any]:
    payload = dict(action.payload or {})
    stored = payload.get("item_results") or action.result or {}
    return dict(stored) if isinstance(stored, dict) else {}


def _batch_has_partial_success(action: PendingAgentAction) -> bool:
    results = _batch_item_results(action)
    statuses = [item.get("status") for item in results.values() if isinstance(item, dict)]
    return any(status == "ok" for status in statuses) and any(
        status == "failed" for status in statuses
    )


def _batch_label(item: dict[str, Any]) -> str:
    internal = item.get("internal_lead_number")
    name = str(item.get("name") or item.get("lead_id") or "—")
    if internal:
        return f"№{internal} — {name}"
    return name


async def _execute_batch(
    action: PendingAgentAction,
    items: list[dict[str, Any]],
    handler,
) -> dict[str, Any]:
    payload = dict(action.payload or {})
    item_results = dict(payload.get("item_results") or {})
    lines = ["<b>Результат пакетной операции</b>", ""]
    failures = 0
    for item in items:
        lead_id = int(item["lead_id"])
        key = str(lead_id)
        prior = item_results.get(key) or {}
        if prior.get("status") == "ok":
            lines.append(f"✅ {html.escape(_batch_label(item))} — уже выполнено")
            continue
        try:
            result = await handler(item)
            item_results[key] = {"status": "ok", "result": result}
            lines.append(f"✅ {html.escape(_batch_label(item))} — выполнено")
        except Exception as exc:
            failures += 1
            item_results[key] = {
                "status": "failed",
                "error": sanitize_text(str(exc), limit=500),
            }
            lines.append(
                f"❌ {html.escape(_batch_label(item))} — "
                f"{html.escape(sanitize_text(str(exc), limit=200) or 'ошибка')}"
            )
    payload["item_results"] = item_results
    action.payload = payload
    partial_failed = failures > 0
    if failures == len(items):
        raise RuntimeError("Пакетная операция не выполнена для всех сделок.")
    return {
        "text": "\n".join(lines),
        "data": {"item_results": item_results, "items": items},
        "partial_failed": partial_failed,
        "error_message": "Часть элементов завершилась ошибкой." if partial_failed else None,
    }


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
            force=True,
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

    if action_type == "create_notion_communication":
        lead = await kommo_service.get_lead_details(
            int(payload["kommo_lead_id"])
        )
        link = await project_link_service.get_by_kommo_lead_id(
            db, int(lead["id"])
        )
        record = await notion_gateway.create_project_communication(
            lead=lead,
            summary=str(payload.get("summary") or ""),
            full_text=str(payload.get("full_text") or payload.get("summary") or ""),
            project_page_id=(
                link.notion_project_page_id if link else None
            ),
            channel=str(payload.get("channel") or "Голос"),
        )
        if record is None:
            raise ValueError(
                "База Notion «Переписка и касания» не настроена."
            )
        return {
            "text": (
                "✅ <b>Переговоры сохранены в Notion</b>\n\n"
                + _result_link("Открыть запись", record.get("url"))
            ),
            "data": {
                "kommo_lead_id": int(lead["id"]),
                "notion_page_id": record.get("id"),
                "notion_url": record.get("url"),
            },
        }

    if action_type == "create_project_task":
        lead = await kommo_service.get_lead_details(
            int(payload["kommo_lead_id"])
        )
        start_at, _ = calendar_event_builder.parse_natural_datetime(
            str(payload["due_at"]),
            duration_minutes=30,
        )
        task_text = str(payload.get("task_text") or "Следующее действие")[:1000]
        kommo_task = await kommo_service.create_lead_task(
            lead_id=int(lead["id"]),
            text=task_text,
            complete_till=int(start_at.timestamp()),
        )
        link = await project_link_service.get_by_kommo_lead_id(
            db, int(lead["id"])
        )
        warnings: list[str] = []
        notion_task: dict[str, Any] | None = None
        try:
            notion_task = await notion_gateway.create_task(
                title=task_text,
                lead_id=int(lead["id"]),
                project_page_id=(
                    link.notion_project_page_id if link else None
                ),
                priority="Высокий",
                task_type="Следующий шаг",
                due_at=start_at,
                next_step=task_text,
                source="Telegram",
                external_id=f"agent-action:{int(action.id)}:project-task",
                update_kommo=False,
            )
        except Exception as exc:
            warnings.append(f"Notion: {sanitize_text(str(exc), limit=300)}")
        lines = [
            "✅ <b>Задача создана</b>",
            "",
            f"Kommo: {html.escape(task_text)}",
            f"Срок: {html.escape(calendar_event_builder.format_date_ru(start_at))} "
            f"{start_at.astimezone(calendar_event_builder.manager_timezone()).strftime('%H:%M')}",
        ]
        if notion_task:
            lines.append(_result_link("Открыть задачу в Notion", notion_task.get("url")))
        if warnings:
            lines.append("⚠️ " + html.escape("; ".join(warnings)))
        return {
            "text": "\n".join(lines),
            "data": {
                "kommo_lead_id": int(lead["id"]),
                "kommo_task": kommo_task,
                "notion_task": notion_task,
                "warnings": warnings,
            },
        }

    if action_type == "update_project_next_step":
        lead = await kommo_service.get_lead_details(
            int(payload["kommo_lead_id"])
        )
        link = await project_link_service.get_by_kommo_lead_id(
            db, int(lead["id"])
        )
        if link is None:
            raise ValueError("ProjectLink не создан для этой сделки.")
        metadata = dict(link.metadata_json or {})
        metadata.update(
            {
                "next_step": str(payload.get("next_step") or "")[:2000],
                "next_step_due_at": payload.get("due_at"),
                "next_step_updated_by": int(action.telegram_user_id),
                "next_step_updated_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        link.metadata_json = metadata
        await db.commit()
        notion_project: dict[str, Any] | None = None
        warnings: list[str] = []
        try:
            due_at = None
            if payload.get("due_at"):
                due_at, _ = calendar_event_builder.parse_natural_datetime(
                    str(payload["due_at"]), duration_minutes=30
                )
            notion_project = await notion_gateway.upsert_project_from_kommo(
                lead,
                next_step=str(payload.get("next_step") or ""),
                next_action_at=due_at,
            )
            if notion_project.get("id") and not link.notion_project_page_id:
                link.notion_project_page_id = str(notion_project["id"])
                link.notion_project_url = notion_project.get("url")
                await db.commit()
        except Exception as exc:
            warnings.append(f"Notion: {sanitize_text(str(exc), limit=300)}")
        return {
            "text": (
                "✅ <b>Следующий шаг обновлён</b>\n\n"
                f"{html.escape(str(payload.get('next_step') or '—'))}"
                + (
                    "\n⚠️ " + html.escape("; ".join(warnings))
                    if warnings
                    else ""
                )
            ),
            "data": {
                "kommo_lead_id": int(lead["id"]),
                "project_key": link.project_key,
                "next_step": payload.get("next_step"),
                "notion_project": notion_project,
                "warnings": warnings,
            },
        }

    if action_type == "prepare_client_followup":
        lead = await kommo_service.get_lead_details(
            int(payload["kommo_lead_id"])
        )
        record = await client_message_service.create_client_message_draft(
            db,
            telegram_user_id=int(action.telegram_user_id),
            lead=lead,
            draft=dict(payload.get("draft") or {}),
            language_source=str(payload.get("language_source") or "project_update"),
            client_id=(
                int(payload["client_id"]) if payload.get("client_id") else None
            ),
            channel="whatsapp",
        )
        return {
            "text": client_message_service.format_client_message_draft(record),
            "data": {
                "kommo_lead_id": int(lead["id"]),
                "client_message_draft_id": int(record.id),
                "communication_language": record.communication_language,
            },
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

    if action_type == "create_kommo_tasks_batch":
        items = list(payload.get("items") or [])
        due_at = str(payload.get("due_at") or "")
        task_text = str(payload.get("task_text") or "Связаться с клиентом")[:1000]
        start_at, _ = calendar_event_builder.parse_natural_datetime(due_at, duration_minutes=30)

        async def _task(item: dict[str, Any]) -> dict[str, Any]:
            return await kommo_service.create_lead_task(
                lead_id=int(item["lead_id"]),
                text=task_text,
                complete_till=int(start_at.timestamp()),
            )

        return await _execute_batch(action, items, _task)

    if action_type == "add_kommo_notes_batch":
        items = list(payload.get("items") or [])
        note_text = str(payload.get("note_text") or "")

        async def _note(item: dict[str, Any]) -> dict[str, Any]:
            return await kommo_service.add_text_note(
                int(item["lead_id"]),
                note_text,
                source="B&BS AI Agent",
            )

        return await _execute_batch(action, items, _note)

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

    if action_type == "create_drive_project":
        result = await project_drive.execute_drive_project(db, payload=payload)
        lines = [
            "✅ <b>Проект создан в Google Drive</b>",
            "",
            f"Project key: <code>{html.escape(str(result.get('project_key') or '—'))}</code>",
            f"Подпапок: <b>{int(result.get('subfolder_count') or 0)}</b>",
        ]
        link = _result_link("Открыть папку", result.get("drive_folder_url"))
        if link:
            lines.append(link)
        return {"text": "\n".join(lines), "data": result}

    if action_type == "link_project_systems":
        lead = await kommo_service.get_lead_details(int(payload["kommo_lead_id"]))
        result = await project_linking.execute_link_systems(db, lead=lead, payload=payload)
        links = [
            _result_link("Kommo", result.get("kommo_url")),
            _result_link("Notion", result.get("notion_url")),
            _result_link("Drive", result.get("drive_folder_url")),
        ]
        return {
            "text": (
                "✅ <b>Системы проекта связаны</b>\n\n"
                f"Project key: <code>{html.escape(str(result.get('project_key') or '—'))}</code>\n"
                + "\n".join(x for x in links if x)
            ),
            "data": result,
        }

    if action_type == "save_file_to_drive_project":
        kommo_id = int(payload["kommo_lead_id"])
        # Revalidate Manager assignment at confirmation time.
        await kommo_service.get_lead_details(kommo_id)
        link = await project_link_service.get_by_kommo_lead_id(db, kommo_id)
        if not link or not link.drive_folder_id:
            raise ValueError("Сначала создайте проект в Google Drive для этой сделки.")
        artifact = None
        if payload.get("artifact_id"):
            artifact = await project_artifact_service.get_artifact(
                db, int(payload["artifact_id"]), lock=True
            )
            if artifact is None:
                raise ValueError("Аудит-запись файла не найдена.")
            if int(artifact.kommo_lead_id) != kommo_id:
                raise PermissionError("Файл привязан к другому проекту.")
        parent_id = str(link.drive_folder_id)
        subfolder = str(payload.get("subfolder_name") or "").strip()
        if subfolder:
            children = await google_drive_service.list_project_files(parent_id, limit=100)
            match = next(
                (item for item in children if str(item.get("name") or "") == subfolder),
                None,
            )
            if match and match.get("id"):
                parent_id = str(match["id"])
            else:
                raise ValueError(
                    f"Подпапка Drive «{subfolder}» не найдена. "
                    "Восстановите структуру проекта и повторите загрузку."
                )
        content = await asyncio.to_thread(
            storage_service.read_project_file_bytes, str(payload["storage_path"])
        )
        try:
            uploaded = await google_drive_service.upload_file(
                parent_folder_id=parent_id,
                filename=str(payload.get("filename") or "file"),
                content=content,
                mime_type=str(payload.get("mime_type") or "application/octet-stream"),
            )
        except Exception as exc:
            cleanup_error = None
            try:
                await asyncio.to_thread(
                    storage_service.delete_project_file,
                    str(payload["storage_path"]),
                )
            except Exception as cleanup_exc:
                cleanup_error = cleanup_exc
            if artifact is not None:
                await project_artifact_service.mark_failed(
                    db,
                    artifact=artifact,
                    error=(
                        str(exc)
                        + (
                            f"; cleanup: {cleanup_error}"
                            if cleanup_error is not None
                            else ""
                        )
                    ),
                )
            raise
        warnings: list[str] = []
        notion_record: dict[str, Any] | None = None
        if link.notion_project_page_id:
            try:
                notion_record = await notion_gateway.append_project_file_record(
                    project_page_id=str(link.notion_project_page_id),
                    filename=str(uploaded.get("name") or payload.get("filename") or "file"),
                    artifact_type=str(
                        payload.get("artifact_type_label")
                        or payload.get("artifact_type")
                        or "Документ"
                    ),
                    drive_url=uploaded.get("webViewLink"),
                    uploaded_by=int(action.telegram_user_id),
                )
            except Exception as exc:
                warnings.append(f"Notion: {sanitize_text(str(exc), limit=300)}")
        else:
            warnings.append("Notion: проект не связан")

        kommo_note_created = False
        try:
            marker = (
                f"BBS-FILE-{int(artifact.id)}"
                if artifact is not None
                else f"BBS-FILE-ACTION-{int(action.id)}"
            )
            await kommo_service.add_common_note(
                kommo_id,
                (
                    f"[{marker}]\n"
                    "Файл проекта загружен через B&BS Telegram Agent.\n"
                    f"Тип: {payload.get('artifact_type_label') or payload.get('artifact_type') or 'Документ'}\n"
                    f"Имя: {uploaded.get('name') or payload.get('filename')}\n"
                    f"Папка: {subfolder or 'корень проекта'}\n"
                    f"Drive: {uploaded.get('webViewLink') or '—'}\n"
                    f"Пользователь Telegram: {action.telegram_user_id}"
                ),
            )
            kommo_note_created = True
        except Exception as exc:
            warnings.append(f"Kommo: {sanitize_text(str(exc), limit=300)}")

        try:
            await asyncio.to_thread(
                storage_service.delete_project_file,
                str(payload["storage_path"]),
            )
        except Exception as exc:
            warnings.append(
                f"Временное хранилище: {sanitize_text(str(exc), limit=300)}"
            )
        if artifact is not None:
            artifact = await project_artifact_service.mark_uploaded(
                db,
                artifact=artifact,
                uploaded_by_telegram_user_id=int(action.telegram_user_id),
                uploaded=uploaded,
                notion=notion_record,
                kommo_note_created=kommo_note_created,
                warnings=warnings,
            )
        result = {
            "artifact_id": int(artifact.id) if artifact is not None else None,
            "kommo_lead_id": kommo_id,
            "file_id": uploaded.get("id"),
            "file_url": uploaded.get("webViewLink"),
            "filename": uploaded.get("name"),
            "project_key": link.project_key,
            "notion": notion_record,
            "kommo_note_created": kommo_note_created,
            "warnings": warnings,
        }
        warning_line = (
            "\n⚠️ " + html.escape("; ".join(warnings)) if warnings else ""
        )
        return {
            "text": (
                "✅ <b>Файл загружен в Google Drive</b>\n\n"
                f"Проект: <code>{html.escape(str(link.project_key))}</code>\n"
                f"Файл: {html.escape(str(uploaded.get('name') or payload.get('filename') or '—'))}\n"
                + _result_link("Открыть файл", uploaded.get("webViewLink"))
                + warning_line
            ),
            "data": result,
        }

    raise ValueError(f"Unsupported agent action: {action_type}")
