"""Unified project card across Kommo, Notion, Drive and the agent audit."""

from __future__ import annotations

import html
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent import notion_gateway
from app.agent.lead_refs import extract_internal_lead_number
from app.config import get_settings
from app.models.integration_event import IntegrationEvent
from app.models.pending_agent_action import PendingAgentAction
from app.services import (
    client_language_service,
    contact_resolver,
    google_drive_service,
    identity_service,
    kommo_service,
    project_artifact_service,
    project_link_service,
)

settings = get_settings()


@dataclass
class ProjectSnapshot:
    identity: dict[str, Any] = field(default_factory=dict)
    client: dict[str, Any] = field(default_factory=dict)
    kommo: dict[str, Any] = field(default_factory=dict)
    responsible: dict[str, Any] = field(default_factory=dict)
    notion: dict[str, Any] = field(default_factory=dict)
    drive: dict[str, Any] = field(default_factory=dict)
    open_tasks: list[dict[str, Any]] = field(default_factory=list)
    communications: list[dict[str, Any]] = field(default_factory=list)
    documents: list[dict[str, Any]] = field(default_factory=list)
    pending_actions: list[dict[str, Any]] = field(default_factory=list)
    missing_information: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    recommended_next_action: str | None = None
    source_warnings: list[str] = field(default_factory=list)
    generated_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


def _timestamp_label(value: Any) -> str:
    if value in (None, ""):
        return "—"
    try:
        if isinstance(value, (int, float)) or str(value).isdigit():
            moment = datetime.fromtimestamp(int(value), tz=timezone.utc)
        else:
            moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return moment.astimezone().strftime("%d.%m.%Y %H:%M")
    except Exception:
        return str(value)[:40]


def _company_from_contact(contact: dict[str, Any]) -> str | None:
    for custom_field in contact.get("custom_fields") or []:
        marker = (
            f"{custom_field.get('name') or ''} "
            f"{custom_field.get('code') or ''}"
        ).casefold()
        if any(token in marker for token in ("company", "компан", "firma")):
            return str(custom_field.get("value") or "") or None
    return None


def _next_step_from(
    *,
    link: Any,
    notion_project: dict[str, Any] | None,
    kommo_tasks: list[dict[str, Any]],
) -> str | None:
    metadata = dict(getattr(link, "metadata_json", None) or {})
    if metadata.get("next_step"):
        return str(metadata["next_step"])
    if notion_project and notion_project.get("Следующий шаг"):
        return str(notion_project["Следующий шаг"])
    if kommo_tasks:
        return str(kommo_tasks[0].get("text") or "") or None
    return None


async def _pending_for_lead(
    db: AsyncSession,
    *,
    kommo_lead_id: int,
) -> list[dict[str, Any]]:
    result = await db.execute(
        select(PendingAgentAction)
        .where(PendingAgentAction.status.in_(("pending", "approved", "executing")))
        .order_by(desc(PendingAgentAction.created_at))
        .limit(200)
    )
    rows: list[dict[str, Any]] = []
    for action in result.scalars().all():
        payload = dict(action.payload or {})
        payload_lead_id = (
            payload.get("kommo_lead_id")
            or payload.get("lead_id")
            or ((payload.get("lead") or {}).get("id") if isinstance(payload.get("lead"), dict) else None)
        )
        if int(payload_lead_id or 0) != int(kommo_lead_id):
            continue
        rows.append(
            {
                "id": int(action.id),
                "type": action.action_type,
                "status": action.status,
                "created_at": action.created_at,
                "telegram_user_id": action.telegram_user_id,
            }
        )
    return rows[:10]


async def build_snapshot(
    db: AsyncSession,
    *,
    lead: dict[str, Any],
    context: dict[str, Any] | None = None,
) -> ProjectSnapshot:
    context = context or {}
    kommo_id = int(lead["id"])
    identity_service.assert_current_user_can_access_lead(lead)
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
            "status_id": lead.get("status_id"),
            "pipeline": lead.get("pipeline_name"),
            "pipeline_id": lead.get("pipeline_id"),
            "price": lead.get("price"),
            "url": lead.get("url"),
            "updated_at": lead.get("updated_at"),
            "closest_task_at": lead.get("closest_task_at"),
            "notes": (lead.get("notes") or [])[:5],
            "source": "kommo",
        },
    )
    contacts = lead.get("contacts") or []
    resolved = contact_resolver.resolve_contact(lead)
    contact = contacts[0] if contacts else {}
    language = await client_language_service.read_communication_language(db, lead=lead)
    snapshot.client = {
        "id": resolved.contact_id or contact.get("id"),
        "name": resolved.name or contact.get("name"),
        "company": lead.get("company_name") or _company_from_contact(contact),
        "phones": [resolved.phone_display] if resolved.phone_display else (contact.get("phones") or []),
        "phone_normalized": resolved.phone_normalized,
        "emails": [resolved.email] if resolved.email else (contact.get("emails") or []),
        "language": language.language,
        "language_source": language.source,
        "source": resolved.source,
        "whatsapp_url": contact_resolver.whatsapp_url(resolved.phone_normalized),
    }

    try:
        owner = await kommo_service.get_user_summary(lead.get("responsible_user_id"))
        snapshot.responsible = owner or {}
    except Exception as exc:
        snapshot.source_warnings.append(f"Kommo user: {exc.__class__.__name__}")

    notion_workspace: dict[str, Any] = {
        "project": None,
        "tasks": [],
        "communications": [],
        "warnings": [],
    }
    if settings.notion_api_token.strip():
        notion_workspace = await notion_gateway.read_project_workspace(kommo_id)
        snapshot.source_warnings.extend(notion_workspace.get("warnings") or [])

    if link:
        notion_project = notion_workspace.get("project") or {}
        snapshot.notion = {
            "page_id": link.notion_project_page_id or notion_project.get("id"),
            "url": link.notion_project_url or notion_project.get("url"),
            "project": notion_project,
            "source": "notion",
        }
        snapshot.drive = {
            "folder_id": link.drive_folder_id,
            "url": link.drive_folder_url,
            "name": link.drive_folder_name,
            "source": "project_link",
        }
    else:
        notion_project = notion_workspace.get("project") or {}
        snapshot.notion = {
            "page_id": notion_project.get("id"),
            "url": notion_project.get("url"),
            "project": notion_project,
            "source": "notion",
        }
        snapshot.missing_information.append("ProjectLink не создан")

    kommo_tasks: list[dict[str, Any]] = []
    try:
        kommo_tasks = await kommo_service.get_open_lead_tasks(kommo_id, limit=20)
    except Exception as exc:
        snapshot.source_warnings.append(f"Kommo tasks: {exc.__class__.__name__}")
    snapshot.open_tasks = kommo_tasks + list(notion_workspace.get("tasks") or [])
    snapshot.communications = list(notion_workspace.get("communications") or [])

    artifacts = await project_artifact_service.recent_for_project(db, kommo_id, limit=10)
    seen_drive_ids: set[str] = set()
    for artifact in artifacts:
        if artifact.drive_file_id:
            seen_drive_ids.add(str(artifact.drive_file_id))
        snapshot.documents.append(
            {
                "name": artifact.final_filename or artifact.suggested_filename,
                "url": artifact.drive_file_url,
                "modified": artifact.uploaded_at or artifact.created_at,
                "type": artifact.artifact_type_label,
                "status": artifact.status,
                "source": "artifact_audit",
            }
        )

    if link and link.drive_folder_id and settings.google_drive_enabled:
        try:
            files = await google_drive_service.list_project_files(
                link.drive_folder_id, limit=20
            )
            for item in files:
                if item.get("mimeType") == "application/vnd.google-apps.folder":
                    continue
                if str(item.get("id") or "") in seen_drive_ids:
                    continue
                snapshot.documents.append(
                    {
                        "name": item.get("name"),
                        "url": item.get("webViewLink"),
                        "modified": item.get("modifiedTime"),
                        "type": "Файл Drive",
                        "status": "external",
                        "source": "drive",
                    }
                )
        except Exception as exc:
            snapshot.source_warnings.append(f"Drive: {exc.__class__.__name__}")

    snapshot.documents.sort(
        key=lambda item: str(item.get("modified") or ""), reverse=True
    )
    snapshot.pending_actions = await _pending_for_lead(db, kommo_lead_id=kommo_id)

    if context.get("pending_clarification"):
        snapshot.blockers.append("Есть незавершённое уточнение агента")
    if snapshot.pending_actions:
        snapshot.blockers.append(
            f"Ожидают подтверждения действия: {len(snapshot.pending_actions)}"
        )
    if (
        not snapshot.client.get("phone_normalized")
        and not snapshot.client.get("phones")
        and not snapshot.client.get("emails")
    ):
        snapshot.missing_information.append("Нет телефона и email клиента")
    if link and not link.notion_project_page_id and not snapshot.notion.get("page_id"):
        snapshot.missing_information.append("Нет связанной карточки Notion")
    if link and not link.drive_folder_id:
        snapshot.missing_information.append("Нет связанной папки Drive")

    next_step = _next_step_from(
        link=link,
        notion_project=notion_project,
        kommo_tasks=kommo_tasks,
    )
    if next_step:
        snapshot.recommended_next_action = next_step
    elif not link:
        snapshot.recommended_next_action = "Создать проект в Drive и связать системы"
    elif not snapshot.open_tasks:
        snapshot.recommended_next_action = (
            "Определить следующий шаг и поставить задачу со сроком"
        )
    else:
        snapshot.recommended_next_action = "Выполнить ближайшую активную задачу"
    return snapshot


def format_snapshot(snapshot: ProjectSnapshot) -> str:
    identity = snapshot.identity
    project_label = identity.get("project_key") or identity.get("name") or "—"
    contacts = snapshot.client
    phones = ", ".join(str(x) for x in contacts.get("phones") or []) or "—"
    emails = ", ".join(str(x) for x in contacts.get("emails") or []) or "—"
    lines = [
        f"<b>📂 Проект {html.escape(str(project_label))}</b>",
        "",
    ]
    if identity.get("internal_lead_number"):
        lines.append(
            f"Номер: <b>№{html.escape(str(identity['internal_lead_number']))}</b>"
        )
    lines.extend(
        [
            f"Клиент: <b>{html.escape(str(contacts.get('name') or '—'))}</b>",
            f"Компания: {html.escape(str(contacts.get('company') or '—'))}",
            f"Телефон: {html.escape(phones)}",
            f"Email: {html.escape(emails)}",
            f"Язык общения: <b>{html.escape(str(contacts.get('language') or '—').upper())}</b>",
            "",
            f"Товар/сделка: {html.escape(str(snapshot.kommo.get('name') or '—'))}",
            f"Статус Kommo: <b>{html.escape(str(snapshot.kommo.get('status') or '—'))}</b>",
            f"Ответственный: {html.escape(str(snapshot.responsible.get('name') or '—'))}",
            f"Последнее обновление: {_timestamp_label(snapshot.kommo.get('updated_at'))}",
        ]
    )

    notes = snapshot.kommo.get("notes") or []
    lines.extend(["", "<b>Последние переговоры</b>"])
    if notes:
        for note in notes[:3]:
            text = " ".join(str(note.get("text") or "").split())
            lines.append(f"• {html.escape(text[:300])}")
    elif snapshot.communications:
        for item in snapshot.communications[:3]:
            text = item.get("summary") or item.get("title") or "Касание"
            lines.append(f"• {html.escape(str(text)[:300])}")
    else:
        lines.append("—")

    lines.extend(["", "<b>Активные задачи</b>"])
    if snapshot.open_tasks:
        for task in snapshot.open_tasks[:5]:
            title = task.get("text") or task.get("title") or "Задача"
            due = task.get("complete_till") or task.get("due_at")
            lines.append(
                f"• {html.escape(str(title)[:180])} · {_timestamp_label(due)}"
            )
    else:
        lines.append("—")

    lines.extend(["", "<b>Последние файлы</b>"])
    if snapshot.documents:
        for doc in snapshot.documents[:5]:
            label = f"{doc.get('type') or 'Файл'}: {doc.get('name') or '—'}"
            if doc.get("url"):
                lines.append(
                    f'• <a href="{html.escape(str(doc["url"]), quote=True)}">'
                    f"{html.escape(label[:220])}</a>"
                )
            else:
                lines.append(f"• {html.escape(label[:220])}")
    else:
        lines.append("—")

    if snapshot.blockers:
        lines.extend(["", "<b>Требует внимания</b>"])
        lines.extend(f"• {html.escape(x)}" for x in snapshot.blockers[:5])
    if snapshot.missing_information:
        lines.extend(["", "<b>Не хватает данных</b>"])
        lines.extend(
            f"• {html.escape(x)}" for x in snapshot.missing_information[:5]
        )
    if snapshot.recommended_next_action:
        lines.extend(
            [
                "",
                "<b>Рекомендуемый следующий шаг</b>",
                html.escape(snapshot.recommended_next_action[:500]),
            ]
        )
    if snapshot.source_warnings:
        lines.extend(
            [
                "",
                "⚠️ Частично недоступно: "
                + html.escape(", ".join(snapshot.source_warnings[:4])),
            ]
        )
    links: list[str] = []
    for label, source in (
        ("Kommo", snapshot.kommo),
        ("Notion", snapshot.notion),
        ("Drive", snapshot.drive),
    ):
        if source.get("url"):
            links.append(
                f'<a href="{html.escape(str(source["url"]), quote=True)}">{label}</a>'
            )
    if links:
        lines.extend(["", "Ссылки: " + " · ".join(links)])
    return "\n".join(lines)[:4000]


def project_actions_markup(snapshot: ProjectSnapshot) -> dict[str, Any]:
    lead_id = int(snapshot.identity.get("kommo_lead_id") or 0)
    rows: list[list[dict[str, str]]] = [
        [
            {
                "text": "🔄 Обновить статус",
                "callback_data": f"agent:project:status:{lead_id}",
            },
            {
                "text": "✅ Добавить задачу",
                "callback_data": f"agent:prep:task:{lead_id}",
            },
        ],
        [
            {
                "text": "🎙 Добавить разговор",
                "callback_data": f"lead:audio:{lead_id}:0",
            },
            {
                "text": "📎 Загрузить файл",
                "callback_data": f"agent:project:upload:{lead_id}",
            },
        ],
        [
            {
                "text": "✍️ Follow-up",
                "callback_data": f"agent:prep:draft:{lead_id}",
            },
            {
                "text": "💬 Переписка",
                "callback_data": f"agent:comms:{lead_id}:0",
            },
        ],
        [
            {
                "text": "🕘 История",
                "callback_data": f"agent:project:history:{lead_id}",
            }
        ],
    ]
    links: list[dict[str, str]] = []
    if snapshot.drive.get("url"):
        links.append({"text": "📁 Drive", "url": str(snapshot.drive["url"])})
    if snapshot.kommo.get("url"):
        links.append({"text": "🔗 Kommo", "url": str(snapshot.kommo["url"])})
    if snapshot.notion.get("url"):
        links.append({"text": "📝 Notion", "url": str(snapshot.notion["url"])})
    if links:
        rows.append(links[:3])
    return {"inline_keyboard": rows}


def _humanize_history_note(text: str) -> str:
    clean = " ".join(str(text or "").split())
    lowered = clean.casefold()
    if "первичн" in lowered and "анализ" in lowered:
        return "Первичный анализ нового лида"
    if "bbs-missed-call" in lowered or "недозвон" in lowered:
        attempt = ""
        for token in clean.split():
            if token.isdigit():
                attempt = f" · попытка {token}"
                break
        return f"Недозвон{attempt}"
    if "результат звонка" in lowered:
        return clean[:180]
    if "auto_lead_analysis" in lowered or "auto_lead_task" in lowered:
        return clean.split("[")[0].strip()[:180] or "Системное событие лида"
    if "update_kommo_lead" in lowered:
        return "Обновление сделки в Kommo"
    return clean[:220]


def _format_task_due(value: Any) -> str:
    if value in (None, ""):
        return ""
    try:
        if isinstance(value, (int, float)):
            dt = datetime.fromtimestamp(int(value), tz=timezone.utc).astimezone()
        else:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone()
        return dt.strftime("%d.%m.%Y %H:%M")
    except Exception:
        return ""


async def build_history(
    db: AsyncSession,
    *,
    lead: dict[str, Any],
    limit: int = 20,
) -> str:
    """Build an operator-facing timeline of what happened to the lead."""
    kommo_id = int(lead["id"])
    lines = [
        f"<b>🕘 История — {html.escape(str(lead.get('name') or kommo_id))}</b>",
        "",
        f"Текущий статус: <b>{html.escape(str(lead.get('status_name') or '—'))}</b>",
        "",
    ]

    try:
        notes = await kommo_service.get_recent_common_notes(kommo_id, limit=max(limit, 20))
    except Exception:
        notes = list(lead.get("notes") or [])

    try:
        tasks = await kommo_service.get_open_lead_tasks(kommo_id, limit=20)
    except Exception:
        tasks = []

    artifacts = await project_artifact_service.recent_for_project(
        db, kommo_id, limit=min(limit, 10)
    )

    timeline: list[tuple[int, str]] = []
    for note in notes:
        stamp = int(note.get("created_at") or note.get("updated_at") or 0)
        label = _humanize_history_note(str(note.get("text") or ""))
        if not label:
            continue
        timeline.append(
            (
                stamp,
                f"• {_timestamp_label(stamp)} · Заметка · {html.escape(label)}",
            )
        )
    for task in tasks:
        stamp = int(task.get("complete_till") or 0)
        title = " ".join(str(task.get("text") or "Задача").split())[:180]
        due = _format_task_due(task.get("complete_till"))
        suffix = f" · срок {due}" if due else ""
        timeline.append(
            (
                stamp or int(datetime.now(timezone.utc).timestamp()),
                f"• {_timestamp_label(stamp) if stamp else 'сейчас'} · Задача{suffix} · "
                f"{html.escape(title)}",
            )
        )
    for artifact in artifacts:
        created = artifact.created_at
        stamp = int(created.timestamp()) if created else 0
        timeline.append(
            (
                stamp,
                f"• {_timestamp_label(created)} · Файл · "
                f"{html.escape(str(artifact.final_filename or artifact.suggested_filename))}",
            )
        )

    # Keep technical IntegrationEvent noise out of the operator history unless
    # there is nothing else to show — then surface a short human label.
    if not timeline:
        events = (
            await db.execute(
                select(IntegrationEvent)
                .order_by(desc(IntegrationEvent.created_at))
                .limit(50)
            )
        ).scalars().all()
        for event in events:
            payload = dict(event.payload or {})
            result = dict(event.result or {})
            lead_id = (
                payload.get("kommo_lead_id")
                or payload.get("lead_id")
                or result.get("kommo_lead_id")
                or result.get("lead_id")
            )
            if int(lead_id or 0) != kommo_id:
                continue
            op = str(event.operation or "")
            human = {
                "update_kommo_lead": "Обновление сделки",
                "create_lead_task": "Создание задачи",
                "add_common_note": "Добавление заметки",
            }.get(op, op.replace("_", " ") or "Событие")
            stamp = int(event.created_at.timestamp()) if event.created_at else 0
            timeline.append(
                (
                    stamp,
                    f"• {_timestamp_label(event.created_at)} · Система · "
                    f"{html.escape(human)} · {html.escape(str(event.status))}",
                )
            )

    timeline.sort(key=lambda item: item[0], reverse=True)
    if not timeline:
        lines.append("Пока нет заметок, задач или звонков по этой сделке.")
    else:
        lines.append("<b>Что происходило</b>")
        lines.append("")
        lines.extend(item[1] for item in timeline[:limit])

    missed = sum(
        1
        for note in notes
        if "недозвон" in str(note.get("text") or "").casefold()
        or "bbs-missed-call" in str(note.get("text") or "").casefold()
        or "не ответил" in str(note.get("text") or "").casefold()
    )
    if missed:
        lines.extend(
            [
                "",
                f"Недозвонов в заметках: <b>{missed}</b>",
                "1-й за день → задача на сегодня; 2-й+ → задача на завтра.",
            ]
        )
    return "\n".join(lines)[:4000]
