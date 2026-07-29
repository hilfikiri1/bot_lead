from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.security import sanitize_text, sanitize_value
from app.models.integration_event import IntegrationEvent

logger = logging.getLogger(__name__)



async def record_event(
    db: AsyncSession,
    *,
    service: str,
    operation: str,
    status: str,
    external_id: str | None = None,
    telegram_user_id: int | None = None,
    duration_ms: int | None = None,
    payload: dict[str, Any] | None = None,
    result: dict[str, Any] | None = None,
    error_message: str | None = None,
) -> IntegrationEvent | None:
    event = IntegrationEvent(
        service=service[:50],
        operation=operation[:100],
        status=status[:20],
        external_id=(external_id or None),
        telegram_user_id=telegram_user_id,
        duration_ms=duration_ms,
        payload=sanitize_value(payload),
        result=sanitize_value(result),
        error_message=sanitize_text(error_message),
    )
    try:
        db.add(event)
        await db.commit()
        await db.refresh(event)
        return event
    except Exception as exc:
        await db.rollback()
        logger.warning("Could not persist integration event: %s", exc)
        return None


async def recent_errors(db: AsyncSession, *, limit: int = 10) -> list[IntegrationEvent]:
    query = (
        select(IntegrationEvent)
        .where(IntegrationEvent.status == "error")
        .order_by(desc(IntegrationEvent.created_at))
        .limit(max(1, min(limit, 50)))
    )
    return list((await db.execute(query)).scalars().all())
