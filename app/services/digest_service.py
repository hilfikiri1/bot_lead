"""Daily operational digest built from Kommo and synchronized to Notion."""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.services import integration_event_service, kommo_service
from app.services import operational_notion_service as notion
from app.services.digest_rules import rank_lead

settings = get_settings()


async def build_digest(
    db: AsyncSession,
    *,
    telegram_user_id: int,
    max_items: int | None = None,
) -> dict[str, Any]:
    started = time.monotonic()
    max_items = max(1, min(max_items or settings.digest_max_items, 20))

    if settings.notion_sync_enabled:
        schema = await notion.validate_schema()
        if not schema["ok"]:
            await integration_event_service.record(
                db,
                service="notion",
                operation="digest_schema_validation",
                status="error",
                telegram_user_id=telegram_user_id,
                result=schema,
                error_message="Notion schema validation failed",
            )
            raise RuntimeError(
                "Схема новой операционной системы Notion не прошла проверку. "
                "Запустите /notion_test."
            )

    kommo_data = await kommo_service.get_all_open_leads()
    leads = kommo_data.get("leads") or []
    now = datetime.now(tz=timezone.utc)
    ranked = [{**rank_lead(lead, int(now.timestamp())), "lead": lead} for lead in leads]
    ranked.sort(key=lambda item: item["score"], reverse=True)
    selected = ranked[:max_items]

    created = reused = failed = 0
    result_items: list[dict[str, Any]] = []
    for item in selected:
        lead = item["lead"]
        notion_task: dict[str, Any] | None = None
        notion_project: dict[str, Any] | None = None
        error: str | None = None
        try:
            if settings.notion_sync_enabled:
                notion_project = await notion.upsert_project_from_kommo(
                    lead,
                    priority=item["priority"],
                    next_step=item["next_step"],
                    next_action_at=now + timedelta(hours=8),
                )
                notion_task = await notion.create_digest_task(
                    title=f"{item['task_type']}: {lead.get('name') or lead.get('id')}",
                    lead=lead,
                    project_page_id=notion_project.get("id") if notion_project else None,
                    priority=item["priority"],
                    task_type=item["task_type"],
                    due_at=now + timedelta(hours=8),
                    next_step=item["next_step"],
                )
                if notion_task.get("created"):
                    created += 1
                else:
                    reused += 1
        except Exception as exc:
            failed += 1
            error = str(exc)[:500]

        result_items.append(
            {
                **item,
                "notion_task": notion_task,
                "notion_project": notion_project,
                "error": error,
            }
        )

    result = {
        "generated_at": now.isoformat(),
        "date_label": now.astimezone(ZoneInfo(settings.manager_timezone)).strftime("%d.%m.%Y"),
        "items": result_items,
        "open_count": len(leads),
        "created_count": created,
        "reused_count": reused,
        "failed_count": failed,
        "truncated": bool(kommo_data.get("truncated")),
    }
    await integration_event_service.record(
        db,
        service="digest",
        operation="build",
        status="ok" if not failed else "partial",
        telegram_user_id=telegram_user_id,
        duration_ms=int((time.monotonic() - started) * 1000),
        result={
            "open_count": result["open_count"],
            "items": len(result_items),
            "created": created,
            "reused": reused,
            "failed": failed,
        },
    )
    return result


async def sync_all_open_leads(
    db: AsyncSession,
    *,
    telegram_user_id: int,
) -> dict[str, int]:
    started = time.monotonic()
    schema = await notion.validate_schema(include_optional=False)
    if not schema["ok"]:
        raise RuntimeError("Схема Notion не прошла проверку. Запустите /notion_test.")
    data = await kommo_service.get_all_open_leads()
    leads = data.get("leads") or []
    created = updated = failed = 0
    for lead in leads:
        try:
            result = await notion.upsert_project_from_kommo(lead)
            created += int(bool(result.get("created")))
            updated += int(not result.get("created"))
        except Exception:
            failed += 1
    summary = {"total": len(leads), "created": created, "updated": updated, "failed": failed}
    await integration_event_service.record(
        db,
        service="notion",
        operation="sync_open_leads",
        status="ok" if not failed else "partial",
        telegram_user_id=telegram_user_id,
        duration_ms=int((time.monotonic() - started) * 1000),
        result=summary,
    )
    return summary


async def complete_task(
    db: AsyncSession,
    *,
    page_id: str,
    lead_id: int,
    telegram_user_id: int,
    result_text: str = "Выполнено через Telegram",
) -> None:
    try:
        await notion.update_task(
            page_id,
            status="Выполнено",
            result=result_text,
            sync_status="synced",
        )
        await kommo_service.add_common_note(
            lead_id,
            f"✅ Задача digest выполнена. {result_text}",
        )
    except Exception as exc:
        await integration_event_service.record(
            db,
            service="digest",
            operation="complete_task",
            status="error",
            external_id=page_id,
            telegram_user_id=telegram_user_id,
            payload={"lead_id": lead_id},
            error_message=str(exc),
        )
        raise
    await integration_event_service.record(
        db,
        service="digest",
        operation="complete_task",
        status="ok",
        external_id=page_id,
        telegram_user_id=telegram_user_id,
        result={"lead_id": lead_id},
    )


async def postpone_task(
    db: AsyncSession,
    *,
    page_id: str,
    lead_id: int,
    telegram_user_id: int,
    hours: int = 24,
) -> datetime:
    due_at = datetime.now(timezone.utc) + timedelta(hours=hours)
    await notion.update_task(
        page_id,
        status="Перенесено",
        due_at=due_at,
        result=f"Перенесено на {due_at.isoformat()}",
        sync_status="pending",
    )
    await integration_event_service.record(
        db,
        service="digest",
        operation="postpone_task",
        status="ok",
        external_id=page_id,
        telegram_user_id=telegram_user_id,
        result={"lead_id": lead_id, "due_at": due_at.isoformat()},
    )
    return due_at
