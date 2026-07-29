"""Durable outbox / retry queue for confirmed external writes."""

from __future__ import annotations

import html
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.security import sanitize_text
from app.models.agent_v5 import IntegrationOperation

STATUSES = (
    "pending",
    "processing",
    "succeeded",
    "failed",
    "retry_scheduled",
    "dead_letter",
    "cancelled",
)


def _backoff_seconds(attempt: int) -> int:
    return min(3600, 15 * (2 ** max(0, attempt - 1)))


async def enqueue(
    db: AsyncSession,
    *,
    operation_type: str,
    service: str,
    payload: dict[str, Any],
    idempotency_key: str,
    telegram_user_id: int | None = None,
    kommo_lead_id: int | None = None,
    correlation_id: str | None = None,
    max_attempts: int = 5,
) -> IntegrationOperation:
    existing = await db.execute(
        select(IntegrationOperation).where(IntegrationOperation.idempotency_key == idempotency_key)
    )
    row = existing.scalar_one_or_none()
    if row:
        return row
    row = IntegrationOperation(
        operation_type=operation_type[:64],
        service=service[:64],
        status="pending",
        kommo_lead_id=kommo_lead_id,
        telegram_user_id=telegram_user_id,
        idempotency_key=idempotency_key[:128],
        correlation_id=correlation_id or uuid4().hex[:16],
        payload_json=payload,
        max_attempts=max_attempts,
        next_attempt_at=datetime.now(timezone.utc),
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


async def mark_succeeded(
    db: AsyncSession,
    op: IntegrationOperation,
    *,
    result: dict[str, Any] | None = None,
    external_id: str | None = None,
) -> IntegrationOperation:
    op.status = "succeeded"
    op.result_json = result or {}
    op.external_id = external_id
    op.last_error = None
    op.updated_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(op)
    return op


async def mark_failed(
    db: AsyncSession,
    op: IntegrationOperation,
    *,
    error: str,
    retryable: bool = True,
) -> IntegrationOperation:
    op.attempt_count = int(op.attempt_count or 0) + 1
    op.last_error = sanitize_text(error, limit=2000)
    op.updated_at = datetime.now(timezone.utc)
    if retryable and op.attempt_count < int(op.max_attempts or 5):
        op.status = "retry_scheduled"
        op.next_attempt_at = datetime.now(timezone.utc) + timedelta(
            seconds=_backoff_seconds(op.attempt_count)
        )
    else:
        op.status = "dead_letter" if retryable else "failed"
        op.next_attempt_at = None
    await db.commit()
    await db.refresh(op)
    return op


async def list_failed(db: AsyncSession, *, limit: int = 20) -> list[IntegrationOperation]:
    result = await db.execute(
        select(IntegrationOperation)
        .where(IntegrationOperation.status.in_(("failed", "dead_letter", "retry_scheduled")))
        .order_by(IntegrationOperation.updated_at.desc())
        .limit(max(1, min(limit, 50)))
    )
    return list(result.scalars().all())


async def get_operation(db: AsyncSession, operation_id: int) -> IntegrationOperation | None:
    result = await db.execute(
        select(IntegrationOperation).where(IntegrationOperation.id == int(operation_id))
    )
    return result.scalar_one_or_none()


async def schedule_retry(db: AsyncSession, op: IntegrationOperation) -> IntegrationOperation:
    if op.status == "succeeded":
        return op
    op.status = "retry_scheduled"
    op.next_attempt_at = datetime.now(timezone.utc)
    op.updated_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(op)
    return op


async def cancel_operation(db: AsyncSession, op: IntegrationOperation) -> IntegrationOperation:
    if op.status == "succeeded":
        return op
    op.status = "cancelled"
    op.next_attempt_at = None
    op.updated_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(op)
    return op


def format_integration_status(ops: list[IntegrationOperation]) -> str:
    lines = ["<b>🔌 Статус интеграций (outbox)</b>", ""]
    if not ops:
        lines.append("Ошибок и отложенных операций нет.")
        return "\n".join(lines)
    for op in ops:
        lines.append(
            f"#{op.id} <code>{html.escape(op.service)}</code> / "
            f"{html.escape(op.operation_type)} — <b>{html.escape(op.status)}</b>"
        )
        if op.last_error:
            lines.append(html.escape(str(op.last_error)[:240]))
        lines.append("")
    return "\n".join(lines)
