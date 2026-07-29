from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.action_utils import action_key, approval_markup  # noqa: F401
from app.config import get_settings
from app.models.pending_agent_action import PendingAgentAction

settings = get_settings()


async def stage_action(
    db: AsyncSession,
    *,
    telegram_user_id: int,
    chat_id: int,
    action_type: str,
    payload: dict[str, Any],
    preview_text: str,
    batch_group_id: str | None = None,
) -> PendingAgentAction:
    key = action_key(
        telegram_user_id=telegram_user_id,
        action_type=action_type,
        payload=(
            {**payload, "_batch_group_id": batch_group_id}
            if batch_group_id
            else payload
        ),
    )
    existing = (
        await db.execute(
            select(PendingAgentAction).where(
                PendingAgentAction.idempotency_key == key
            )
        )
    ).scalar_one_or_none()
    now = datetime.now(timezone.utc)
    if existing:
        expires_at = existing.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if existing.status == "executing":
            started_at = existing.approved_at or existing.updated_at
            if started_at is not None:
                started = started_at if started_at.tzinfo else started_at.replace(tzinfo=timezone.utc)
                age = (now - started).total_seconds()
                if age < 120:
                    return existing
                existing.status = "failed"
                existing.error_message = (
                    "Исполнение прервано (таймаут executing). Проверь внешний сервис "
                    "перед повторной командой."
                )
                await db.commit()
            else:
                return existing
        if existing.status in {"pending", "approved"} and expires_at >= now:
            return existing
        if existing.status in {"pending", "approved"} and expires_at < now:
            existing.status = "expired"
            await db.commit()
        # The same command may legitimately be requested again after it was
        # executed, rejected, failed or expired. Keep idempotency for the live
        # confirmation only and create a fresh unique action afterwards.
        key = f"{key}:{uuid4().hex}"
    ttl = max(5, min(settings.agent_action_ttl_minutes, 24 * 60))
    action = PendingAgentAction(
        telegram_user_id=telegram_user_id,
        chat_id=chat_id,
        action_type=action_type[:100],
        status="pending",
        payload=payload,
        batch_group_id=(batch_group_id or None),
        preview_text=preview_text[:20_000],
        idempotency_key=key,
        expires_at=now + timedelta(minutes=ttl),
    )
    db.add(action)
    try:
        await db.commit()
    except IntegrityError:
        # A duplicate Telegram delivery may stage the same action concurrently.
        # Return the row committed by the winning request instead of surfacing a
        # database error to the manager.
        await db.rollback()
        concurrent = (
            await db.execute(
                select(PendingAgentAction).where(
                    PendingAgentAction.idempotency_key == key
                )
            )
        ).scalar_one_or_none()
        if concurrent is not None:
            return concurrent
        raise
    await db.refresh(action)
    return action


async def get_action(db: AsyncSession, action_id: int) -> PendingAgentAction | None:
    return await db.get(PendingAgentAction, int(action_id))


async def get_batch_actions(
    db: AsyncSession,
    *,
    batch_group_id: str,
    telegram_user_id: int,
) -> list[PendingAgentAction]:
    result = await db.execute(
        select(PendingAgentAction)
        .where(
            PendingAgentAction.batch_group_id == str(batch_group_id),
            PendingAgentAction.telegram_user_id == int(telegram_user_id),
        )
        .order_by(PendingAgentAction.id.asc())
    )
    return list(result.scalars().all())


async def reject_action(
    db: AsyncSession,
    *,
    action: PendingAgentAction,
    telegram_user_id: int,
) -> PendingAgentAction:
    locked = (
        await db.execute(
            select(PendingAgentAction)
            .where(PendingAgentAction.id == int(action.id))
            .with_for_update()
        )
    ).scalar_one_or_none()
    if locked is None:
        return action
    action = locked
    if int(action.telegram_user_id) != int(telegram_user_id):
        raise PermissionError("Это действие принадлежит другому пользователю.")
    if action.status not in {"pending", "approved"}:
        return action
    action.status = "rejected"
    await db.commit()
    await db.refresh(action)
    return action
