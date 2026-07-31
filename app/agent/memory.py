from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import delete, desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent_message import AgentMessage
from app.models.agent_session import AgentSession
from app.services import crm_service


async def get_or_create_session(
    db: AsyncSession, *, telegram_user_id: int
) -> AgentSession:
    result = await db.execute(
        select(AgentSession).where(AgentSession.telegram_user_id == telegram_user_id)
    )
    session = result.scalar_one_or_none()
    if session:
        return session
    session = AgentSession(telegram_user_id=telegram_user_id, context={})
    db.add(session)
    await db.commit()
    await db.refresh(session)
    return session


async def remember_message(
    db: AsyncSession,
    *,
    session: AgentSession,
    role: str,
    content: str,
    source: str,
    intent: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> AgentMessage:
    record = AgentMessage(
        session_id=session.id,
        telegram_user_id=session.telegram_user_id,
        role=role[:20],
        source=source[:20],
        content=content[:50_000],
        intent=(intent[:100] if intent else None),
        metadata_json=metadata,
    )
    db.add(record)
    if role == "user":
        session.last_user_message = content[:10_000]
    elif role == "assistant":
        session.last_assistant_message = content[:10_000]
    if intent:
        session.last_intent = intent[:100]
    await db.commit()
    await db.refresh(record)
    return record


async def recent_messages(
    db: AsyncSession,
    *,
    telegram_user_id: int,
    limit: int = 12,
) -> list[dict[str, Any]]:
    query = (
        select(AgentMessage)
        .where(AgentMessage.telegram_user_id == telegram_user_id)
        .order_by(desc(AgentMessage.created_at))
        .limit(max(1, min(limit, 30)))
    )
    rows = list((await db.execute(query)).scalars().all())
    rows.reverse()
    return [
        {
            "role": row.role,
            "source": row.source,
            "content": row.content,
            "intent": row.intent,
            "created_at": row.created_at.isoformat() if row.created_at else None,
        }
        for row in rows
    ]


async def set_active_lead(
    db: AsyncSession,
    *,
    session: AgentSession,
    kommo_lead_id: int | None = None,
    local_lead_id: int | None = None,
    lead_name: str | None = None,
) -> None:
    if kommo_lead_id is not None:
        session.active_kommo_lead_id = int(kommo_lead_id)
    if local_lead_id is not None:
        session.active_local_lead_id = int(local_lead_id)
    context = dict(session.context or {})
    if lead_name:
        context["active_lead_name"] = lead_name[:500]
    context["active_lead_updated_at"] = datetime.now(timezone.utc).isoformat()
    session.context = context
    await db.commit()


async def update_context(
    db: AsyncSession,
    *,
    session: AgentSession,
    values: dict[str, Any],
) -> None:
    context = dict(session.context or {})
    context.update(values)
    session.context = context
    await db.commit()


async def build_context(
    db: AsyncSession,
    *,
    telegram_user_id: int,
    session: AgentSession | None = None,
) -> dict[str, Any]:
    session = session or await get_or_create_session(
        db, telegram_user_id=telegram_user_id
    )
    local_context = await crm_service.get_user_command_context(
        db, telegram_user_id=telegram_user_id
    )
    recent = await recent_messages(db, telegram_user_id=telegram_user_id, limit=10)
    active_kommo = session.active_kommo_lead_id or local_context.get("kommo_lead_id")
    active_local = session.active_local_lead_id or local_context.get("local_lead_id")
    session_context = dict(session.context or {})
    return {
        **local_context,
        "active_kommo_lead_id": active_kommo,
        "active_local_lead_id": active_local,
        "active_lead_name": session_context.get("active_lead_name")
        or local_context.get("lead_name"),
        "memory_summary": session.memory_summary,
        "last_intent": session.last_intent,
        "recent_messages": recent,
        "last_draft": session_context.get("last_draft"),
        "last_draft_lead": session_context.get("last_draft_lead"),
        "last_draft_created_at": session_context.get("last_draft_created_at"),
        "last_digest": session_context.get("last_digest"),
        "pending_clarification": session_context.get("pending_clarification"),
        # QA modes are written to AgentSession.context by goals_qa_runtime and
        # must survive the next Telegram update. Without these keys, text after
        # /bug is incorrectly routed to the normal CRM agent, and /bug_test
        # cannot receive the manager's verification result.
        "qa_intake": session_context.get("qa_intake"),
        "qa_retest_issue_id": session_context.get("qa_retest_issue_id"),
    }


async def reset_session(
    db: AsyncSession,
    *,
    telegram_user_id: int,
) -> None:
    session = await get_or_create_session(db, telegram_user_id=telegram_user_id)
    session.active_kommo_lead_id = None
    session.active_local_lead_id = None
    session.memory_summary = None
    session.last_intent = None
    session.last_user_message = None
    session.last_assistant_message = None
    session.context = {}
    await db.execute(
        delete(AgentMessage).where(AgentMessage.telegram_user_id == telegram_user_id)
    )
    await db.commit()


async def message_count(db: AsyncSession, *, telegram_user_id: int) -> int:
    query = select(func.count(AgentMessage.id)).where(
        AgentMessage.telegram_user_id == telegram_user_id
    )
    return int((await db.execute(query)).scalar_one() or 0)
