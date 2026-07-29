"""Orchestration for confirmed Google Drive project creation."""

from __future__ import annotations

import html
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.lead_refs import extract_internal_lead_number
from app.config import get_settings
from app.services import google_drive_service, kommo_service, project_link_service
from app.services.google_drive_service import COUNTRY_FOLDER_NAMES, PROJECT_SUBFOLDERS

settings = get_settings()


def _country_parent_folder_id(country_code: str) -> str:
    """Resolve country subfolder under projects root — uses projects folder as parent for MVP."""
    _ = COUNTRY_FOLDER_NAMES.get(country_code.upper(), COUNTRY_FOLDER_NAMES["OTHER"])
    return settings.google_drive_projects_folder_id.strip()


async def build_drive_project_preview(
    db: AsyncSession,
    *,
    lead: dict[str, Any],
    country_code: str | None = None,
) -> dict[str, Any]:
    kommo_id = int(lead["id"])
    internal = extract_internal_lead_number(lead)
    country = project_link_service.infer_country_code(
        lead=lead, explicit=country_code
    )
    existing = await project_link_service.get_by_kommo_lead_id(db, kommo_id)
    if existing and existing.project_key:
        project_key = existing.project_key
    else:
        project_key = project_link_service.build_project_key(
            country_code=country,
            internal_lead_number=internal,
            kommo_lead_id=kommo_id,
        )
    contacts = lead.get("contacts") or []
    client_name = (contacts[0].get("name") if contacts else None) or lead.get("name")
    folder_name = f"{project_key} — {lead.get('name') or client_name}"
    parent_id = _country_parent_folder_id(country)
    warnings: list[str] = []
    if not settings.google_drive_enabled:
        warnings.append("GOOGLE_DRIVE_ENABLED=false")
    if not parent_id:
        warnings.append("GOOGLE_DRIVE_PROJECTS_FOLDER_ID не задан")
    if existing and existing.drive_folder_id:
        warnings.append("Папка уже связана с проектом — будет использована существующая")
    return {
        "project_key": project_key,
        "internal_lead_number": internal,
        "kommo_lead_id": kommo_id,
        "kommo_lead_name": lead.get("name"),
        "kommo_url": lead.get("url"),
        "country_code": country,
        "client_name": client_name,
        "project_name": lead.get("name"),
        "folder_name": folder_name[:255],
        "parent_folder_id": parent_id,
        "subfolders": list(PROJECT_SUBFOLDERS),
        "existing_drive_folder_id": existing.drive_folder_id if existing else None,
        "warnings": warnings,
    }


def format_drive_project_preview(data: dict[str, Any]) -> str:
    internal = data.get("internal_lead_number")
    lines = [
        "<b>📁 Создать проект в Google Drive?</b>",
        "",
        f"Project key: <code>{html.escape(str(data.get('project_key') or '—'))}</code>",
    ]
    if internal:
        lines.append(f"Внутренний номер: <b>№{html.escape(str(internal))}</b>")
    lines.extend(
        [
            f"Сделка: {html.escape(str(data.get('kommo_lead_name') or '—'))}",
            f"Kommo ID: <code>{html.escape(str(data.get('kommo_lead_id') or '—'))}</code>",
            f"Клиент: {html.escape(str(data.get('client_name') or '—'))}",
            f"Страна: {html.escape(str(data.get('country_code') or '—'))}",
            f"Папка: {html.escape(str(data.get('folder_name') or '—'))}",
            "",
            "<b>Подпапки</b>",
        ]
    )
    for name in data.get("subfolders") or []:
        lines.append(f"• {html.escape(str(name))}")
    for warning in data.get("warnings") or []:
        lines.append(f"⚠️ {html.escape(str(warning))}")
    lines.append("")
    lines.append("Запись выполнится только после подтверждения.")
    return "\n".join(lines)


async def execute_drive_project(
    db: AsyncSession,
    *,
    payload: dict[str, Any],
) -> dict[str, Any]:
    kommo_id = int(payload["kommo_lead_id"])
    project_key = str(payload["project_key"])
    parent_id = str(payload.get("parent_folder_id") or settings.google_drive_projects_folder_id)
    folder_name = str(payload.get("folder_name") or project_key)

    existing = await project_link_service.get_by_kommo_lead_id(db, kommo_id)
    if existing and existing.drive_folder_id:
        folder = {
            "id": existing.drive_folder_id,
            "webViewLink": existing.drive_folder_url,
            "name": existing.drive_folder_name or folder_name,
        }
    elif payload.get("existing_drive_folder_id"):
        folder = {
            "id": payload["existing_drive_folder_id"],
            "webViewLink": payload.get("existing_drive_folder_url"),
            "name": folder_name,
        }
    else:
        found = await google_drive_service.find_project_folder(
            parent_id=parent_id, project_key=project_key
        )
        if found:
            folder = found
        else:
            folder = await google_drive_service.create_project_folder(
                project_key=project_key,
                parent_id=parent_id,
                display_name=folder_name,
            )

    subfolders = await google_drive_service.ensure_project_subfolders(str(folder["id"]))
    link = await project_link_service.upsert_link(
        db,
        project_key=project_key,
        kommo_lead_id=kommo_id,
        internal_lead_number=payload.get("internal_lead_number"),
        kommo_lead_name=payload.get("kommo_lead_name"),
        country_code=payload.get("country_code"),
        client_name=payload.get("client_name"),
        project_name=payload.get("project_name"),
        drive_folder_id=str(folder.get("id") or ""),
        drive_folder_url=str(folder.get("webViewLink") or ""),
        drive_folder_name=str(folder.get("name") or folder_name),
        metadata={"subfolder_count": len(subfolders)},
    )
    return {
        "project_key": link.project_key,
        "drive_folder_id": link.drive_folder_id,
        "drive_folder_url": link.drive_folder_url,
        "subfolder_count": len(subfolders),
        "kommo_url": payload.get("kommo_url"),
    }
