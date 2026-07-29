"""Unified project card with graceful multi-source degradation."""

from __future__ import annotations

import asyncio
import html
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.lead_refs import extract_internal_lead_number
from app.config import get_settings
from app.models.agent_v5 import LeadAssessment, NextActionState, ProjectMemory
from app.models.project_link import ProjectLink
from app.services import (
    contact_resolver,
    drive_diagnostics,
    google_drive_service,
    identity_service,
    kommo_service,
    next_action_service,
    project_link_service,
)
from app.services.contact_resolver import ResolvedContact

settings = get_settings()


@dataclass
class SourceBlock:
    data: dict[str, Any] = field(default_factory=dict)
    source: str = "unknown"
    updated_at: str | None = None
    confidence: float = 1.0
    error: str | None = None
    ok: bool = True


@dataclass
class UnifiedProject:
    internal_number: str | None = None
    kommo_lead_id: int | None = None
    title: str | None = None
    product: str | None = None
    pipeline: str | None = None
    stage: str | None = None
    status: str | None = None
    budget: Any = None
    lead_class: str | None = None
    client: dict[str, Any] = field(default_factory=dict)
    contacts: list[dict[str, Any]] = field(default_factory=list)
    company: str | None = None
    country: str | None = None
    client_language: str | None = None
    responsible_user: dict[str, Any] = field(default_factory=dict)
    source: str | None = None
    last_contact_at: str | None = None
    last_contact_channel: str | None = None
    last_contact_summary: str | None = None
    next_action: str | None = None
    next_action_at: str | None = None
    open_tasks: list[dict[str, Any]] = field(default_factory=list)
    overdue_tasks: list[dict[str, Any]] = field(default_factory=list)
    files: list[dict[str, Any]] = field(default_factory=list)
    recent_events: list[dict[str, Any]] = field(default_factory=list)
    requirements: list[str] = field(default_factory=list)
    decisions: list[str] = field(default_factory=list)
    promises: list[str] = field(default_factory=list)
    missing_information: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    recommended_action: str | None = None
    integration_health: dict[str, str] = field(default_factory=dict)
    freshness: dict[str, str] = field(default_factory=dict)
    blocks: dict[str, SourceBlock] = field(default_factory=dict)
    primary_contact: ResolvedContact | None = None
    whatsapp_url: str | None = None
    generated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _safe(coro, *, source: str) -> SourceBlock:
    try:
        data = await asyncio.wait_for(coro, timeout=float(getattr(settings, "agent_source_timeout_seconds", 8) or 8))
        if isinstance(data, SourceBlock):
            return data
        return SourceBlock(data=data if isinstance(data, dict) else {"value": data}, source=source, updated_at=_now_iso())
    except Exception as exc:
        return SourceBlock(source=source, ok=False, error=exc.__class__.__name__, confidence=0.0, updated_at=_now_iso())


async def build_unified_project(
    db: AsyncSession,
    *,
    lead: dict[str, Any],
    include_drive_files: bool = True,
) -> UnifiedProject:
    identity_service.assert_current_user_can_access_lead(lead)
    kommo_id = int(lead["id"])
    internal = extract_internal_lead_number(lead)
    project = UnifiedProject(
        internal_number=internal,
        kommo_lead_id=kommo_id,
        title=str(lead.get("name") or ""),
        product=str(lead.get("name") or ""),
        pipeline=str(lead.get("pipeline_name") or "") or None,
        stage=str(lead.get("status_name") or "") or None,
        status=str(lead.get("status_name") or "") or None,
        budget=lead.get("price"),
        source="kommo",
        integration_health={"kommo": "✅"},
        freshness={"kommo": _now_iso()},
    )
    project.blocks["kommo"] = SourceBlock(data={"id": kommo_id}, source="kommo", updated_at=_now_iso())

    primary = contact_resolver.resolve_contact(lead)
    project.primary_contact = primary
    project.client = {
        "contact_id": primary.contact_id,
        "name": primary.name,
        "phone": primary.phone_display,
        "phone_normalized": primary.phone_normalized,
        "email": primary.email,
        "source": primary.source,
    }
    project.contacts = [
        {
            "contact_id": c.contact_id,
            "name": c.name,
            "phone": c.phone_display,
            "email": c.email,
            "source": c.source,
        }
        for c in contact_resolver.resolve_all_contacts(lead)
    ]
    project.whatsapp_url = contact_resolver.whatsapp_url(primary.phone_normalized)
    if primary.source == "missing" or not primary.phone_normalized:
        if primary.source == "missing":
            project.missing_information.append("Нет связанного контакта с телефоном")
        elif not primary.phone_normalized:
            project.missing_information.append("В связанном контакте нет телефона")
    # Only warn about missing phone when truly missing — not when contact has phone.
    if primary.phone_normalized:
        project.missing_information = [
            item for item in project.missing_information if "телефон" not in item.casefold()
        ]

    try:
        owner = await kommo_service.get_user_summary(lead.get("responsible_user_id"))
        project.responsible_user = owner or {}
    except Exception:
        project.integration_health["kommo_user"] = "⚠️"

    link = await project_link_service.get_by_kommo_lead_id(db, kommo_id)
    if link:
        project.country = link.country_code
        project.company = link.client_name
        project.blocks["link"] = SourceBlock(
            data={"project_key": link.project_key, "drive_folder_id": link.drive_folder_id},
            source="project_link",
            updated_at=_now_iso(),
        )

    # Notion — graceful
    notion_block = await _safe(_load_notion(kommo_id), source="notion")
    project.blocks["notion"] = notion_block
    project.integration_health["notion"] = "✅" if notion_block.ok else "⚠️"
    if notion_block.ok and notion_block.data.get("project"):
        notion_project = notion_block.data["project"]
        if notion_project.get("Следующий шаг"):
            project.next_action = str(notion_project["Следующий шаг"])

    # Drive — graceful
    if include_drive_files and link and link.drive_folder_id and settings.google_drive_enabled:
        drive_block = await _safe(_load_drive_files(str(link.drive_folder_id)), source="drive")
        project.blocks["drive"] = drive_block
        if drive_block.ok:
            project.integration_health["drive"] = "✅"
            project.files = list(drive_block.data.get("files") or [])
        else:
            category = "unknown"
            try:
                # Prefer classified category when available from nested error path.
                category = str(drive_block.error or "unknown")
            except Exception:
                pass
            project.integration_health["drive"] = f"⚠️ {category}"
    elif not settings.google_drive_enabled:
        project.integration_health["drive"] = "⚪ отключён"
    else:
        project.integration_health["drive"] = "⚪ нет папки"

    project.integration_health.setdefault("sheets", "⚪")

    # Next action evaluation
    view = next_action_service.evaluate_lead_next_action(
        lead, action_text=project.next_action
    )
    project.next_action = view.action_text or view.recommended_action
    project.next_action_at = view.due_at.isoformat() if view.due_at else None
    project.recommended_action = view.recommended_action
    if view.status == "overdue":
        project.overdue_tasks.append({"text": view.recommended_action, "due_at": project.next_action_at})
    if view.stale_reason:
        project.risks.append(view.stale_reason)

    # Memory + assessment from DB
    memory = (
        await db.execute(select(ProjectMemory).where(ProjectMemory.kommo_lead_id == kommo_id))
    ).scalar_one_or_none()
    if memory:
        if memory.requirements:
            project.requirements = [memory.requirements]
        if memory.decisions:
            project.decisions = [memory.decisions]
        if memory.promises:
            project.promises = [memory.promises]
        if memory.missing_information:
            project.missing_information.extend(
                [x for x in str(memory.missing_information).split("\n") if x.strip()]
            )
        if memory.risks:
            project.risks.extend([x for x in str(memory.risks).split("\n") if x.strip()])

    assessment = (
        await db.execute(
            select(LeadAssessment)
            .where(LeadAssessment.kommo_lead_id == kommo_id)
            .order_by(desc(LeadAssessment.created_at))
            .limit(1)
        )
    ).scalar_one_or_none()
    if assessment:
        project.lead_class = assessment.grade

    notes = lead.get("notes") or []
    if notes:
        project.last_contact_summary = str(notes[0].get("text") or "")[:400]
        project.last_contact_channel = "kommo_note"
        if notes[0].get("created_at"):
            project.last_contact_at = str(notes[0].get("created_at"))

    return project


async def _load_notion(kommo_id: int) -> dict[str, Any]:
    if not settings.notion_api_token.strip():
        return {"project": None, "skipped": True}
    from app.agent import notion_gateway

    return await notion_gateway.read_project_workspace(kommo_id)


async def _load_drive_files(folder_id: str) -> dict[str, Any]:
    try:
        files = await google_drive_service.list_project_files(folder_id, limit=10)
        return {
            "files": [
                {
                    "name": item.get("name"),
                    "url": item.get("webViewLink"),
                    "modified": item.get("modifiedTime"),
                }
                for item in files
            ]
        }
    except Exception as exc:
        info = drive_diagnostics.classify_drive_exception(exc)
        raise RuntimeError(info.category) from exc


def format_unified_project(project: UnifiedProject) -> str:
    title = project.title or "Проект"
    number = f"№{project.internal_number}" if project.internal_number else f"ID {project.kommo_lead_id}"
    lines = [
        f"<b>📂 {html.escape(number)} — {html.escape(title[:80])}</b>",
        "",
    ]
    contact = project.primary_contact
    if contact and contact.name:
        lines.append(f"Клиент: <b>{html.escape(contact.name)}</b>")
    if contact and contact.phone_display:
        lines.append(f"Телефон: {html.escape(contact.phone_display)}")
        if project.whatsapp_url:
            lines.append(
                f'<a href="{html.escape(project.whatsapp_url, quote=True)}">Открыть WhatsApp</a>'
            )
    elif contact and contact.source != "missing":
        # Contact exists but phone missing — soft note, not a hard blocker banner.
        lines.append("Телефон: не указан в контакте")
    lines.extend(
        [
            f"Этап: {html.escape(str(project.stage or '—'))}",
            f"Бюджет: {html.escape(str(project.budget if project.budget is not None else '—'))}",
        ]
    )
    if project.lead_class:
        badge = {"A": "🟢 A", "B": "🟡 B", "C": "🔴 C"}.get(project.lead_class, project.lead_class)
        lines.append(f"Класс: {badge}")
    lines.extend(["", "<b>Следующий шаг</b>", html.escape(str(project.recommended_action or project.next_action or "не задан"))])
    if project.missing_information:
        lines.extend(["", "<b>Нужно уточнить</b>"])
        lines.extend(f"• {html.escape(item)}" for item in project.missing_information[:6])
    if project.risks:
        lines.extend(["", "<b>Риски</b>"])
        lines.extend(f"• {html.escape(item)}" for item in project.risks[:6])
    lines.extend(["", "<b>Интеграции</b>"])
    for name in ("kommo", "notion", "drive", "sheets"):
        mark = project.integration_health.get(name, "⚪")
        lines.append(f"{name.capitalize()}: {html.escape(str(mark))}")
    return "\n".join(lines)


def project_actions_markup(project: UnifiedProject) -> dict[str, Any]:
    lead_id = int(project.kommo_lead_id or 0)
    rows = [
        [
            {"text": "💬 Follow-up", "callback_data": f"agent:prep:draft:{lead_id}"},
            {"text": "📞 Звонок", "callback_data": f"agent:prep:task:{lead_id}"},
        ],
        [
            {"text": "✅ Задача", "callback_data": f"agent:prep:task:{lead_id}"},
            {"text": "📝 Заметка", "callback_data": f"agent:prep:note:{lead_id}"},
        ],
        [
            {"text": "🕒 История", "callback_data": f"agent:hist:{lead_id}:all:0"},
            {"text": "📂 Файлы", "callback_data": f"agent:files:{lead_id}"},
        ],
        [
            {"text": "⚠️ Что отсутствует", "callback_data": f"agent:missing:{lead_id}"},
            {"text": "🔄 Обновить", "callback_data": f"agent:lead:{lead_id}"},
        ],
    ]
    return {"inline_keyboard": rows}
