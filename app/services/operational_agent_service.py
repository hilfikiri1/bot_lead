"""High-level B&BS operational workflows used by Telegram commands."""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.services import draft_service, integration_event_service, kommo_service
from app.services import operational_notion_service as notion

DRAFT_TASK_TYPES = {
    "commercial_offer": "КП",
    "supplier_brief": "Поставщик",
    "catalog_outline": "Документы",
    "followup_message": "Сообщение",
}


async def generate_and_store_draft(
    db: AsyncSession,
    *,
    kind: str,
    lead_id: int,
    telegram_user_id: int,
) -> dict[str, Any]:
    if kind not in DRAFT_TASK_TYPES:
        raise ValueError(f"Unsupported draft kind: {kind}")
    started = time.monotonic()
    try:
        lead = await kommo_service.get_lead_details(lead_id)
        draft = await draft_service.generate(kind, lead)
        lead_summary = {
            "id": lead_id,
            "name": lead.get("name") or f"Kommo {lead_id}",
            "url": lead.get("url") or lead.get("kommo_url"),
            "updated_at": int(datetime.now(timezone.utc).timestamp()),
        }
        project = await notion.upsert_project_from_kommo(
            lead_summary,
            priority="Средний",
            next_step=str(draft.get("next_action") or "Проверить черновик"),
            next_action_at=datetime.now(timezone.utc) + timedelta(days=1),
        )
        external_id = (
            f"draft:{kind}:kommo:{lead_id}:"
            f"{datetime.now(timezone.utc).date().isoformat()}"
        )
        task = await notion.create_task(
            title=str(draft.get("title") or f"Черновик для Kommo #{lead_id}"),
            lead_id=lead_id,
            project_page_id=project.get("id"),
            priority="Средний",
            task_type=DRAFT_TASK_TYPES[kind],
            due_at=datetime.now(timezone.utc) + timedelta(days=1),
            next_step=str(draft.get("next_action") or "Проверить и дополнить черновик"),
            source="Telegram",
            external_id=external_id,
            result=str(draft.get("body") or ""),
        )

        record = None
        if kind == "commercial_offer":
            record = await notion.create_offer_draft(
                lead=lead_summary,
                draft=draft,
                project_page_id=project.get("id"),
            )
        elif kind == "catalog_outline":
            record = await notion.create_catalog_draft(
                lead=lead_summary,
                draft=draft,
                project_page_id=project.get("id"),
            )
        elif kind == "followup_message":
            record = await notion.create_communication_draft(
                lead=lead_summary,
                draft=draft,
                project_page_id=project.get("id"),
            )

        result = {
            "lead": lead,
            "draft": draft,
            "project": project,
            "task": task,
            "record": record,
        }
        await integration_event_service.record(
            db,
            service="operational_agent",
            operation=f"generate_{kind}",
            status="ok",
            external_id=str(lead_id),
            telegram_user_id=telegram_user_id,
            duration_ms=int((time.monotonic() - started) * 1000),
            result={
                "task_id": task.get("id"),
                "record_id": record.get("id") if record else None,
            },
        )
        return result
    except Exception as exc:
        await integration_event_service.record(
            db,
            service="operational_agent",
            operation=f"generate_{kind}",
            status="error",
            external_id=str(lead_id),
            telegram_user_id=telegram_user_id,
            duration_ms=int((time.monotonic() - started) * 1000),
            error_message=str(exc),
        )
        raise
