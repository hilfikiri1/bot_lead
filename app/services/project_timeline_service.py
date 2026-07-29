"""Project timeline / event log for Agent v5."""

from __future__ import annotations

import hashlib
import html
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.project_event import ProjectEvent

EVENT_FILTERS = {
    "all": None,
    "negotiations": {"note", "call", "whatsapp", "email", "voice", "followup", "conversation"},
    "tasks": {"task_created", "task_completed", "next_action", "calendar"},
    "files": {"file_uploaded", "file_classified", "document"},
    "decisions": {"decision", "promise", "requirement", "assessment"},
}


def _idempotency_key(*, event_type: str, kommo_lead_id: int, external_id: str | None, title: str) -> str:
    raw = f"{event_type}:{kommo_lead_id}:{external_id or ''}:{title}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:64]


async def record_event(
    db: AsyncSession,
    *,
    kommo_lead_id: int,
    event_type: str,
    title: str,
    summary: str | None = None,
    actor: str | None = None,
    source: str = "agent",
    project_key: str | None = None,
    internal_lead_number: str | None = None,
    payload: dict[str, Any] | None = None,
    external_id: str | None = None,
    occurred_at: datetime | None = None,
    idempotency_key: str | None = None,
) -> ProjectEvent | None:
    key = idempotency_key or _idempotency_key(
        event_type=event_type,
        kommo_lead_id=int(kommo_lead_id),
        external_id=external_id,
        title=title,
    )
    existing = await db.execute(select(ProjectEvent).where(ProjectEvent.idempotency_key == key))
    if existing.scalar_one_or_none():
        return None
    event = ProjectEvent(
        project_key=project_key,
        kommo_lead_id=int(kommo_lead_id),
        internal_lead_number=internal_lead_number,
        event_type=event_type[:64],
        occurred_at=occurred_at or datetime.now(timezone.utc),
        actor=(actor or "")[:255] or None,
        source=source[:64],
        title=title[:255],
        summary=summary,
        payload_json=payload or {},
        external_id=(external_id or "")[:128] or None,
        idempotency_key=key,
    )
    db.add(event)
    try:
        await db.commit()
        await db.refresh(event)
        return event
    except Exception:
        await db.rollback()
        return None


async def list_events(
    db: AsyncSession,
    *,
    kommo_lead_id: int,
    event_filter: str = "all",
    limit: int = 5,
    offset: int = 0,
    query: str | None = None,
) -> list[ProjectEvent]:
    stmt = select(ProjectEvent).where(ProjectEvent.kommo_lead_id == int(kommo_lead_id))
    allowed = EVENT_FILTERS.get(event_filter)
    if allowed:
        stmt = stmt.where(ProjectEvent.event_type.in_(tuple(allowed)))
    if query:
        like = f"%{query.strip()}%"
        stmt = stmt.where(
            (ProjectEvent.title.ilike(like)) | (ProjectEvent.summary.ilike(like))
        )
    stmt = stmt.order_by(desc(ProjectEvent.occurred_at)).offset(max(0, offset)).limit(max(1, min(limit, 50)))
    result = await db.execute(stmt)
    return list(result.scalars().all())


def format_history(
    events: list[ProjectEvent],
    *,
    kommo_lead_id: int,
    internal_number: str | None = None,
    event_filter: str = "all",
    offset: int = 0,
) -> str:
    label = f"№{internal_number}" if internal_number else f"Kommo {kommo_lead_id}"
    lines = [f"<b>🕒 История проекта {html.escape(label)}</b>", ""]
    if not events:
        lines.append("Событий пока нет.")
        return "\n".join(lines)
    for event in events:
        when = event.occurred_at.astimezone().strftime("%d.%m %H:%M") if event.occurred_at else "—"
        lines.append(
            f"<b>{html.escape(when)}</b> · {html.escape(event.event_type)}\n"
            f"{html.escape(event.title)}"
        )
        if event.summary:
            lines.append(html.escape(str(event.summary)[:300]))
        lines.append("")
    lines.append(f"Фильтр: <code>{html.escape(event_filter)}</code> · offset {offset}")
    return "\n".join(lines)


def history_markup(*, kommo_lead_id: int, offset: int = 0) -> dict[str, Any]:
    lead = int(kommo_lead_id)
    rows = [
        [
            {"text": "Последние 5", "callback_data": f"agent:hist:{lead}:all:0"},
            {"text": "Переговоры", "callback_data": f"agent:hist:{lead}:negotiations:0"},
        ],
        [
            {"text": "Задачи", "callback_data": f"agent:hist:{lead}:tasks:0"},
            {"text": "Файлы", "callback_data": f"agent:hist:{lead}:files:0"},
        ],
        [
            {"text": "Решения", "callback_data": f"agent:hist:{lead}:decisions:0"},
            {"text": "Показать ещё", "callback_data": f"agent:hist:{lead}:all:{offset + 5}"},
        ],
    ]
    return {"inline_keyboard": rows}
