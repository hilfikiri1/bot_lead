"""Failure-tolerant audit logger for external integrations."""
from __future__ import annotations

import logging
import re
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.integration_event import IntegrationEvent

logger = logging.getLogger(__name__)


def _safe_error_message(value: str | None, limit: int = 4000) -> str | None:
    if value is None:
        return None
    text = str(value)[:limit]
    text = re.sub(
        r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+",
        r"\1***",
        text,
    )
    text = re.sub(
        r"(?i)\b(token|secret|password|api[_-]?key|authorization)\b\s*[:=]\s*[^\s,;]+",
        lambda match: f"{match.group(1)}=***",
        text,
    )
    return text


def _safe_dict(value: dict[str, Any] | None, limit: int = 20_000) -> dict[str, Any] | None:
    if not value:
        return value
    clean: dict[str, Any] = {}
    for key, item in value.items():
        lowered = key.lower()
        if any(secret in lowered for secret in ("token", "secret", "password", "authorization", "api_key")):
            clean[key] = "***"
        else:
            text = str(item)
            clean[key] = item if len(text) <= limit else f"{text[:limit]}…"
    return clean


async def record(
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
        external_id=external_id or None,
        telegram_user_id=telegram_user_id,
        duration_ms=duration_ms,
        payload=_safe_dict(payload),
        result=_safe_dict(result),
        error_message=_safe_error_message(error_message),
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


async def recent_errors(db: AsyncSession, limit: int = 10) -> list[IntegrationEvent]:
    query = (
        select(IntegrationEvent)
        .where(IntegrationEvent.status == "error")
        .order_by(desc(IntegrationEvent.created_at))
        .limit(max(1, min(limit, 50)))
    )
    return list((await db.execute(query)).scalars().all())
