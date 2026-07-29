"""Scheduled morning/evening agent digests (read-only, no external writes)."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent import digest
from app.config import get_settings
from app.database import AsyncSessionLocal
from app.models.agent_session import AgentSession
from app.services.telegram_service import send_message

logger = logging.getLogger(__name__)
settings = get_settings()

_SENT_KEYS: set[str] = set()


def _tz() -> ZoneInfo:
    try:
        return ZoneInfo(settings.agent_digest_timezone or settings.manager_timezone)
    except Exception:
        return ZoneInfo("Europe/Warsaw")


def _digest_idempotency_key(kind: str, user_id: int, local_date: str) -> str:
    return f"digest:{kind}:{user_id}:{local_date}"


async def _already_sent(db: AsyncSession, user_id: int, key: str) -> bool:
    if key in _SENT_KEYS:
        return True
    result = await db.execute(
        select(AgentSession).where(AgentSession.telegram_user_id == user_id)
    )
    session = result.scalar_one_or_none()
    if session and (session.context or {}).get("last_scheduled_digest_key") == key:
        return True
    return False


async def send_scheduled_digest(*, kind: str, user_id: int, chat_id: int | None = None) -> None:
    """Send read-only digest to one allowed user."""
    target_chat = chat_id or user_id
    now = datetime.now(_tz())
    key = _digest_idempotency_key(kind, user_id, now.date().isoformat())

    async with AsyncSessionLocal() as db:
        if await _already_sent(db, user_id, key):
            logger.info("Scheduled digest skipped (duplicate): %s", key)
            return
        result = await digest.build_digest()
        title = "🌅 Утренний дайджест" if kind == "morning" else "🌆 Вечерний отчёт"
        text = f"<b>{title}</b>\n\n" + digest.format_digest(result)
        await send_message(target_chat, text, reply_markup=digest.digest_markup(result.get("digest_map") or []))
        session = (
            await db.execute(
                select(AgentSession).where(AgentSession.telegram_user_id == user_id)
            )
        ).scalar_one_or_none()
        if session:
            ctx = dict(session.context or {})
            ctx["last_scheduled_digest_key"] = key
            ctx["last_digest"] = digest.build_last_digest_context(result)
            session.context = ctx
            await db.commit()
        _SENT_KEYS.add(key)


async def periodic_digest_loop() -> None:
    """Asyncio scheduler for morning/evening digests."""
    await asyncio.sleep(max(30, settings.lead_status_sync_initial_delay_seconds))
    while True:
        try:
            now = datetime.now(_tz())
            allowed = settings.get_allowed_user_ids()
            if settings.agent_morning_digest_enabled and now.hour == settings.agent_morning_digest_hour:
                for user_id in allowed:
                    await send_scheduled_digest(kind="morning", user_id=user_id)
            if settings.agent_evening_digest_enabled and now.hour == settings.agent_evening_digest_hour:
                for user_id in allowed:
                    await send_scheduled_digest(kind="evening", user_id=user_id)
        except Exception as exc:
            logger.warning("Scheduled digest loop error: %s", exc)
        await asyncio.sleep(60)


async def start_periodic_digest_loop() -> asyncio.Task:
    return asyncio.create_task(periodic_digest_loop(), name="agent-scheduled-digest")
