"""Next-action engine, stale detection and operational inbox for Agent v5."""

from __future__ import annotations

import html
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.agent_v5 import NextActionState
from app.services import kommo_service

settings = get_settings()


@dataclass
class NextActionView:
    kommo_lead_id: int
    internal_number: str | None
    name: str
    status: str
    waiting_on: str | None
    action_text: str | None
    due_at: datetime | None
    stale_reason: str | None
    recommended_action: str | None
    age_days: int = 0
    category: str = "without_next"


@dataclass
class InboxResult:
    overdue: list[NextActionView] = field(default_factory=list)
    waiting_us: list[NextActionView] = field(default_factory=list)
    waiting_client: list[NextActionView] = field(default_factory=list)
    without_next: list[NextActionView] = field(default_factory=list)
    stale: list[NextActionView] = field(default_factory=list)
    document_review: list[NextActionView] = field(default_factory=list)
    integration_errors: list[NextActionView] = field(default_factory=list)
    ready: list[NextActionView] = field(default_factory=list)


def stale_threshold_days(*, pipeline: str | None = None, grade: str | None = None) -> int:
    configured = int(getattr(settings, "agent_stale_days_default", 7) or 7)
    if grade == "A":
        return max(2, configured // 2)
    if grade == "C":
        return configured * 2
    _ = pipeline
    return configured


def evaluate_lead_next_action(
    lead: dict[str, Any],
    *,
    now: datetime | None = None,
    grade: str | None = None,
    waiting_on: str | None = None,
    action_text: str | None = None,
) -> NextActionView:
    now = now or datetime.now(timezone.utc)
    now_ts = int(now.timestamp())
    closest = lead.get("closest_task_at")
    updated_at = int(lead.get("updated_at") or lead.get("created_at") or 0)
    age_days = max(0, (now_ts - updated_at) // 86_400) if updated_at else 999
    internal = None
    from app.agent.lead_refs import extract_internal_lead_number

    try:
        internal = extract_internal_lead_number(lead)
    except Exception:
        internal = None

    due_at = None
    status = "ok"
    stale_reason = None
    category = "ready"
    recommended = action_text

    if waiting_on == "us":
        status = "waiting_us"
        category = "waiting_us"
        recommended = recommended or "Ответить клиенту"
    elif waiting_on == "client":
        status = "waiting_client"
        category = "waiting_client"
        recommended = recommended or "Дождаться ответа клиента"

    if closest is None and not action_text:
        status = "missing"
        category = "without_next"
        stale_reason = "нет следующей задачи"
        recommended = "Создать следующее действие"
    elif isinstance(closest, (int, float)) and int(closest) < now_ts:
        status = "overdue"
        category = "overdue"
        due_at = datetime.fromtimestamp(int(closest), tz=timezone.utc)
        stale_reason = "задача просрочена"
        recommended = recommended or "Закрыть или перенести просроченную задачу"
    elif isinstance(closest, (int, float)):
        due_at = datetime.fromtimestamp(int(closest), tz=timezone.utc)
        if not action_text:
            recommended = "Выполнить задачу в срок"

    threshold = stale_threshold_days(pipeline=str(lead.get("pipeline_name") or ""), grade=grade)
    if age_days >= threshold and status not in {"overdue"}:
        if status == "ok":
            status = "stale"
            category = "stale"
        stale_reason = stale_reason or f"нет контакта {age_days} дн."
        recommended = recommended or "Связаться с клиентом"

    return NextActionView(
        kommo_lead_id=int(lead.get("id") or 0),
        internal_number=internal,
        name=str(lead.get("name") or lead.get("id") or "—"),
        status=status,
        waiting_on=waiting_on,
        action_text=action_text,
        due_at=due_at,
        stale_reason=stale_reason,
        recommended_action=recommended,
        age_days=age_days,
        category=category,
    )


async def upsert_next_action_state(
    db: AsyncSession,
    view: NextActionView,
    *,
    responsible_user_id: int | None = None,
) -> NextActionState:
    result = await db.execute(
        select(NextActionState).where(NextActionState.kommo_lead_id == view.kommo_lead_id)
    )
    row = result.scalar_one_or_none()
    if row is None:
        row = NextActionState(kommo_lead_id=view.kommo_lead_id)
        db.add(row)
    row.status = view.status
    row.waiting_on = view.waiting_on
    row.action_text = view.action_text or view.recommended_action
    row.due_at = view.due_at
    row.responsible_user_id = responsible_user_id
    row.stale_reason = view.stale_reason
    row.metadata_json = {"category": view.category, "age_days": view.age_days}
    row.updated_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(row)
    return row


async def build_inbox(
    db: AsyncSession,
    *,
    limit_per_section: int = 8,
    responsible_user_id: int | None = None,
) -> InboxResult:
    open_result = await kommo_service.get_all_open_leads()
    leads = open_result.get("leads") or []
    inbox = InboxResult()
    for lead in leads:
        if responsible_user_id and int(lead.get("responsible_user_id") or 0) != int(responsible_user_id):
            continue
        view = evaluate_lead_next_action(lead)
        try:
            await upsert_next_action_state(
                db,
                view,
                responsible_user_id=lead.get("responsible_user_id"),
            )
        except Exception:
            await db.rollback()
        bucket = {
            "overdue": inbox.overdue,
            "waiting_us": inbox.waiting_us,
            "waiting_client": inbox.waiting_client,
            "without_next": inbox.without_next,
            "stale": inbox.stale,
            "ready": inbox.ready,
        }.get(view.category, inbox.without_next)
        if len(bucket) < limit_per_section:
            bucket.append(view)
    return inbox


def _item_lines(items: list[NextActionView], *, emoji: str, title: str) -> list[str]:
    if not items:
        return []
    lines = [f"{emoji} <b>{title}: {len(items)}</b>", ""]
    for index, item in enumerate(items[:8], 1):
        label = f"№{item.internal_number}" if item.internal_number else str(item.kommo_lead_id)
        lines.append(f"{index}. {html.escape(label)} — {html.escape(item.name[:60])}")
        reason = item.stale_reason or item.recommended_action or item.status
        lines.append(html.escape(str(reason)[:120]))
        lines.append("")
    return lines


def format_inbox(inbox: InboxResult) -> str:
    lines = ["<b>📥 Операционный inbox</b>", ""]
    lines.extend(_item_lines(inbox.overdue, emoji="🔴", title="Просрочено"))
    lines.extend(_item_lines(inbox.waiting_us, emoji="🟠", title="Клиент ждёт нас"))
    lines.extend(_item_lines(inbox.waiting_client, emoji="🟡", title="Мы ждём клиента"))
    lines.extend(_item_lines(inbox.without_next, emoji="🔵", title="Нет следующего действия"))
    lines.extend(_item_lines(inbox.stale, emoji="⚪", title="Давно без контакта"))
    if len(lines) == 2:
        lines.append("Сейчас нет проблемных проектов.")
    return "\n".join(lines)


def format_plan(inbox: InboxResult) -> str:
    lines = [
        "<b>📅 ПЛАН НА СЕГОДНЯ</b>",
        "",
        f"🔴 Просрочено: <b>{len(inbox.overdue)}</b>",
        f"🔵 Без следующего шага: <b>{len(inbox.without_next)}</b>",
        f"🟠 Клиент ждёт нас: <b>{len(inbox.waiting_us)}</b>",
        f"🟡 Ждём клиента: <b>{len(inbox.waiting_client)}</b>",
        f"⚪ Stale: <b>{len(inbox.stale)}</b>",
        "",
    ]
    combined = (inbox.overdue + inbox.waiting_us + inbox.without_next + inbox.stale)[:10]
    if not combined:
        lines.append("На сегодня критичных пунктов нет.")
        return "\n".join(lines)
    for index, item in enumerate(combined, 1):
        label = f"№{item.internal_number}" if item.internal_number else str(item.kommo_lead_id)
        lines.append(f"<b>{index}. {html.escape(label)} — {html.escape(item.name[:50])}</b>")
        lines.append(html.escape(str(item.recommended_action or item.stale_reason or "—")[:160]))
        lines.append("")
    return "\n".join(lines)


def inbox_markup(inbox: InboxResult) -> dict[str, Any] | None:
    rows: list[list[dict[str, str]]] = []
    for item in (inbox.overdue + inbox.waiting_us + inbox.without_next)[:6]:
        label = f"№{item.internal_number}" if item.internal_number else str(item.kommo_lead_id)
        rows.append(
            [
                {
                    "text": f"Открыть {label}"[:64],
                    "callback_data": f"agent:lead:{item.kommo_lead_id}",
                }
            ]
        )
    return {"inline_keyboard": rows} if rows else None


NEXT_ACTION_OPTIONS = [
    ("write", "Написать клиенту"),
    ("call", "Позвонить"),
    ("factory", "Запросить фабрику"),
    ("offer", "Подготовить предложение"),
    ("wait", "Ждём ответ"),
    ("close", "Закрыть как нецелевой"),
    ("manual", "Указать вручную"),
]


def next_action_prompt_markup(*, kommo_lead_id: int) -> dict[str, Any]:
    rows = []
    for key, label in NEXT_ACTION_OPTIONS:
        rows.append(
            [{"text": label, "callback_data": f"agent:next:{kommo_lead_id}:{key}"}]
        )
    return {"inline_keyboard": rows}
