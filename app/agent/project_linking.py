"""Preview and execution for linking Kommo, Notion and Drive project records."""

from __future__ import annotations

import html
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.agent import notion_gateway
from app.agent.lead_refs import extract_internal_lead_number
from app.agent.project_drive import build_drive_project_preview
from app.config import get_settings
from app.services import project_link_service

settings = get_settings()


async def build_link_preview(
    db: AsyncSession,
    *,
    lead: dict[str, Any],
) -> dict[str, Any]:
    drive_preview = await build_drive_project_preview(db, lead=lead)
    kommo_id = int(lead["id"])
    notion_match: dict[str, Any] | None = None
    if settings.notion_projects_data_source_id:
        try:
            matches = await notion_gateway.query_by_number(
                settings.notion_projects_data_source_id,
                "Kommo ID",
                kommo_id,
            )
            if matches:
                page = matches[0]
                notion_match = {
                    "page_id": page.get("id"),
                    "url": page.get("url")
                    or notion_gateway.notion_page_url(str(page.get("id") or "")),
                }
        except Exception:
            notion_match = None

    existing = await project_link_service.get_by_kommo_lead_id(db, kommo_id)
    warnings: list[str] = list(drive_preview.get("warnings") or [])
    if not settings.notion_projects_data_source_id:
        warnings.append("NOTION_PROJECTS_DATA_SOURCE_ID не задан")
    if not notion_match:
        warnings.append("Страница Notion не найдена — будет создана при подтверждении")

    return {
        "project_key": drive_preview.get("project_key"),
        "internal_lead_number": extract_internal_lead_number(lead),
        "kommo_lead_id": kommo_id,
        "kommo_lead_name": lead.get("name"),
        "kommo_url": lead.get("url"),
        "drive_folder_id": (existing.drive_folder_id if existing else None)
        or drive_preview.get("existing_drive_folder_id"),
        "drive_folder_url": existing.drive_folder_url if existing else None,
        "notion_page_id": (existing.notion_project_page_id if existing else None)
        or (notion_match or {}).get("page_id"),
        "notion_url": (existing.notion_project_url if existing else None)
        or (notion_match or {}).get("url"),
        "create_notion": notion_match is None and bool(settings.notion_projects_data_source_id),
        "warnings": warnings,
    }


def format_link_preview(data: dict[str, Any]) -> str:
    lines = [
        "<b>🔗 Связать Kommo, Notion и Drive?</b>",
        "",
        f"Project key: <code>{html.escape(str(data.get('project_key') or '—'))}</code>",
        f"Сделка: {html.escape(str(data.get('kommo_lead_name') or '—'))}",
    ]
    if data.get("internal_lead_number"):
        lines.append(f"Внутренний номер: <b>№{html.escape(str(data['internal_lead_number']))}</b>")
    lines.extend(
        [
            "",
            f"Notion: {html.escape(str(data.get('notion_url') or 'будет создан'))}",
            f"Drive: {html.escape(str(data.get('drive_folder_url') or data.get('drive_folder_id') or 'не связан'))}",
        ]
    )
    for warning in data.get("warnings") or []:
        lines.append(f"⚠️ {html.escape(str(warning))}")
    lines.extend(["", "Запись выполнится только после подтверждения."])
    return "\n".join(lines)


async def execute_link_systems(
    db: AsyncSession,
    *,
    lead: dict[str, Any],
    payload: dict[str, Any],
) -> dict[str, Any]:
    kommo_id = int(payload["kommo_lead_id"])
    project_key = str(payload["project_key"])
    notion_page_id = payload.get("notion_page_id")
    notion_url = payload.get("notion_url")

    if payload.get("create_notion"):
        notion_result = await notion_gateway.upsert_project_from_kommo(lead)
        notion_page_id = notion_result.get("id") or notion_page_id
        notion_url = notion_result.get("url") or notion_url

    link = await project_link_service.upsert_link(
        db,
        project_key=project_key,
        kommo_lead_id=kommo_id,
        internal_lead_number=payload.get("internal_lead_number"),
        kommo_lead_name=payload.get("kommo_lead_name") or lead.get("name"),
        notion_project_page_id=str(notion_page_id) if notion_page_id else None,
        notion_project_url=str(notion_url) if notion_url else None,
        drive_folder_id=payload.get("drive_folder_id"),
        drive_folder_url=payload.get("drive_folder_url"),
    )
    return {
        "project_key": link.project_key,
        "notion_url": link.notion_project_url,
        "drive_folder_url": link.drive_folder_url,
        "kommo_url": payload.get("kommo_url") or lead.get("url"),
    }
