"""Unified read-only project snapshot across Kommo, Notion, Drive and agent memory."""

from __future__ import annotations

import html
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.lead_refs import extract_internal_lead_number
from app.config import get_settings
from app.services import google_drive_service, kommo_service, project_link_service

settings = get_settings()


@dataclass
class ProjectSnapshot:
    identity: dict[str, Any] = field(default_factory=dict)
    client: dict[str, Any] = field(default_factory=dict)
    kommo: dict[str, Any] = field(default_factory=dict)
    notion: dict[str, Any] = field(default_factory=dict)
    drive: dict[str, Any] = field(default_factory=dict)
    open_tasks: list[dict[str, Any]] = field(default_factory=list)
    documents: list[dict[str, Any]] = field(default_factory=list)
    missing_information: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    recommended_next_action: str | None = None
    source_warnings: list[str] = field(default_factory=list)
    generated_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


async def build_snapshot(
    db: AsyncSession,
    *,
    lead: dict[str, Any],
    context: dict[str, Any] | None = None,
) -> ProjectSnapshot:
    context = context or {}
    kommo_id = int(lead["id"])
    internal = extract_internal_lead_number(lead)
    link = await project_link_service.get_by_kommo_lead_id(db, kommo_id)
    snapshot = ProjectSnapshot(
        identity={
            "project_key": link.project_key if link else None,
            "internal_lead_number": internal,
            "kommo_lead_id": kommo_id,
            "name": lead.get("name"),
            "source": "kommo",
        },
        kommo={
            "name": lead.get("name"),
            "status": lead.get("status_name"),
            "pipeline": lead.get("pipeline_name"),
            "price": lead.get("price"),
            "url": lead.get("url"),
            "updated_at": lead.get("updated_at"),
            "closest_task_at": lead.get("closest_task_at"),
            "notes": (lead.get("notes") or [])[:3],
            "source": "kommo",
        },
    )
    contacts = lead.get("contacts") or []
    if contacts:
        snapshot.client = {
            "name": contacts[0].get("name"),
            "phones": contacts[0].get("phones") or [],
            "emails": contacts[0].get("emails") or [],
            "source": "kommo",
        }

    if link:
        snapshot.notion = {
            "page_id": link.notion_project_page_id,
            "url": link.notion_project_url,
            "source": "project_link",
        }
        snapshot.drive = {
            "folder_id": link.drive_folder_id,
            "url": link.drive_folder_url,
            "name": link.drive_folder_name,
            "source": "project_link",
        }
    else:
        snapshot.missing_information.append("ProjectLink не создан")
        snapshot.recommended_next_action = "Создать проект в Drive и связать системы"

    if link and link.drive_folder_id and settings.google_drive_enabled:
        try:
            files = await google_drive_service.list_project_files(
                link.drive_folder_id, limit=10
            )
            snapshot.documents = [
                {
                    "name": item.get("name"),
                    "url": item.get("webViewLink"),
                    "modified": item.get("modifiedTime"),
                    "source": "drive",
                }
                for item in files
            ]
        except Exception as exc:
            snapshot.source_warnings.append(f"Drive: {exc.__class__.__name__}")

    pending = context.get("pending_clarification")
    if pending:
        snapshot.blockers.append("Есть незавершённое уточнение агента")

    if context.get("last_draft"):
        snapshot.open_tasks.append(
            {"kind": "draft", "title": "Последний черновик", "source": "agent_memory"}
        )

    return snapshot


def format_snapshot(snapshot: ProjectSnapshot) -> str:
    identity = snapshot.identity
    lines = [
        f"<b>📂 Проект {html.escape(str(identity.get('project_key') or identity.get('name') or '—'))}</b>",
        "",
    ]
    if identity.get("internal_lead_number"):
        lines.append(f"Внутренний номер: №{html.escape(str(identity['internal_lead_number']))}")
    lines.extend(
        [
            f"Клиент: {html.escape(str(snapshot.client.get('name') or '—'))}",
            f"Товар/сделка: {html.escape(str(snapshot.kommo.get('name') or '—'))}",
            f"Этап: {html.escape(str(snapshot.kommo.get('status') or '—'))}",
            f"Бюджет: {html.escape(str(snapshot.kommo.get('price') or '—'))}",
            "",
            "<b>Документы</b>",
        ]
    )
    if snapshot.documents:
        for doc in snapshot.documents[:8]:
            lines.append(f"• {html.escape(str(doc.get('name') or '—'))}")
    else:
        lines.append("—")
    if snapshot.missing_information:
        lines.extend(["", "<b>Нерешённые вопросы</b>"])
        lines.extend(f"• {html.escape(x)}" for x in snapshot.missing_information[:8])
    if snapshot.recommended_next_action:
        lines.extend(
            [
                "",
                "<b>Рекомендуемый следующий шаг</b>",
                html.escape(snapshot.recommended_next_action),
            ]
        )
    links: list[str] = []
    if snapshot.kommo.get("url"):
        links.append(f'<a href="{html.escape(str(snapshot.kommo["url"]), quote=True)}">Kommo</a>')
    if snapshot.notion.get("url"):
        links.append(
            f'<a href="{html.escape(str(snapshot.notion["url"]), quote=True)}">Notion</a>'
        )
    if snapshot.drive.get("url"):
        links.append(
            f'<a href="{html.escape(str(snapshot.drive["url"]), quote=True)}">Drive</a>'
        )
    if links:
        lines.extend(["", "Ссылки: " + " · ".join(links)])
    for warning in snapshot.source_warnings:
        lines.append(f"⚠️ {html.escape(warning)}")
    return "\n".join(lines)
