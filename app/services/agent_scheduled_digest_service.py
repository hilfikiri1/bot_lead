"""One scheduler for morning digest, evening reflection and weekly kaizen review."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent import digest, memory
from app.config import get_settings
from app.database import AsyncSessionLocal
from app.models.agent_session import AgentSession
from app.services import identity_service, kaizen_journal_service
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
    """Send the existing read-only digest to one allowed user."""
    target_chat = chat_id or user_id
    now = datetime.now(_tz())
    key = _digest_idempotency_key(kind, user_id, now.date().isoformat())

    async with AsyncSessionLocal() as db:
        if await _already_sent(db, user_id, key):
            logger.info("Scheduled digest skipped (duplicate): %s", key)
            return
        identity_service.set_current_user(
            await identity_service.get_user_by_telegram_id(db, user_id)
        )
        result = await digest.build_digest(db=db, telegram_user_id=user_id)
        title = "🌅 Утренний дайджест" if kind == "morning" else "🌆 Вечерний отчёт"
        text = f"<b>{title}</b>\n\n" + digest.format_digest(result)
        await send_message(
            target_chat,
            text,
            reply_markup=digest.digest_markup(result.get("digest_map") or []),
        )
        session = await memory.get_or_create_session(
            db, telegram_user_id=user_id
        )
        ctx = dict(session.context or {})
        ctx["last_scheduled_digest_key"] = key
        ctx["last_digest"] = digest.build_last_digest_context(result)
        session.context = ctx
        await db.commit()
        _SENT_KEYS.add(key)


async def send_evening_reflection(
    *, user_id: int, chat_id: int | None = None
) -> None:
    target_chat = chat_id or user_id
    async with AsyncSessionLocal() as db:
        identity_service.set_current_user(
            await identity_service.get_user_by_telegram_id(db, user_id)
        )
        entry = await kaizen_journal_service.claim_evening_invitation(
            db, telegram_user_id=user_id
        )
        if entry is None:
            return
        try:
            session = await memory.get_or_create_session(
                db, telegram_user_id=user_id
            )
            await kaizen_journal_service.set_pending_reflection(
                db,
                session=session,
                day=entry.period_start,
                source="scheduled",
            )
            await send_message(
                target_chat,
                kaizen_journal_service.reflection_invitation_text(),
                reply_markup=kaizen_journal_service.reflection_invitation_markup(
                    entry.period_start
                ),
            )
            await kaizen_journal_service.mark_evening_invitation_sent(
                db, entry=entry
            )
        except Exception:
            await db.rollback()
            try:
                await kaizen_journal_service.release_evening_invitation_claim(
                    db, entry=entry
                )
            except Exception:
                await db.rollback()
            raise


async def send_due_reflection_reminders() -> None:
    async with AsyncSessionLocal() as db:
        entries = await kaizen_journal_service.claim_due_reminders(db)
        for entry in entries:
            try:
                session = await memory.get_or_create_session(
                    db, telegram_user_id=int(entry.telegram_user_id)
                )
                await kaizen_journal_service.set_pending_reflection(
                    db,
                    session=session,
                    day=entry.period_start,
                    source="scheduled",
                )
                await send_message(
                    int(entry.telegram_user_id),
                    "⏰ <b>Напоминаю про итоги дня</b>\n\n"
                    "Расскажи одним текстовым или голосовым сообщением, что сегодня получилось, "
                    "что мешало и что важно завтра.",
                    reply_markup=kaizen_journal_service.reflection_invitation_markup(
                        entry.period_start
                    ),
                )
            except Exception as exc:
                logger.warning(
                    "Kaizen reminder failed for user %s: %s",
                    entry.telegram_user_id,
                    exc.__class__.__name__,
                )


async def send_weekly_review(*, user_id: int, chat_id: int | None = None) -> None:
    target_chat = chat_id or user_id
    async with AsyncSessionLocal() as db:
        identity_service.set_current_user(
            await identity_service.get_user_by_telegram_id(db, user_id)
        )
        period = await kaizen_journal_service.claim_weekly_review(
            db, telegram_user_id=user_id
        )
        if period is None:
            return
        entry, analysis_ok = await kaizen_journal_service.build_weekly_review(
            db,
            telegram_user_id=user_id,
            week_start=period[0],
            force_rebuild=True,
        )
        text = kaizen_journal_service.format_weekly_review(
            entry, analysis_ok=analysis_ok
        )
        if (
            (entry.analysis or {}).get("improvement_candidates")
            and not kaizen_journal_service.notion_improvements_available()
        ):
            text += (
                "\n\nℹ️ Создание карточек скрыто: проверь базу Tasks командой "
                "<code>/notion_test</code>."
            )
        await send_message(
            target_chat,
            text,
            reply_markup=kaizen_journal_service.weekly_review_markup(entry),
        )


async def periodic_digest_loop() -> None:
    """Async scheduler shared by all digest/reflection deliveries."""
    await asyncio.sleep(max(30, settings.lead_status_sync_initial_delay_seconds))
    while True:
        try:
            now = datetime.now(_tz())
            allowed = settings.get_allowed_user_ids()

            # Reminders are due-time based, not hour based.
            if settings.agent_evening_reflection_enabled:
                await send_due_reflection_reminders()

            if (
                settings.agent_morning_digest_enabled
                and now.hour == settings.agent_morning_digest_hour
            ):
                for user_id in allowed:
                    await send_scheduled_digest(kind="morning", user_id=user_id)

            if settings.agent_evening_reflection_enabled:
                if now.hour == settings.agent_evening_reflection_hour:
                    for user_id in allowed:
                        await send_evening_reflection(user_id=user_id)
            elif (
                settings.agent_evening_digest_enabled
                and now.hour == settings.agent_evening_digest_hour
            ):
                for user_id in allowed:
                    await send_scheduled_digest(kind="evening", user_id=user_id)

            if (
                settings.agent_weekly_review_enabled
                and now.weekday() == settings.agent_weekly_review_weekday
                and now.hour == settings.agent_weekly_review_hour
            ):
                for user_id in allowed:
                    await send_weekly_review(user_id=user_id)
        except Exception as exc:
            logger.warning("Scheduled agent loop error: %s", exc)
        await asyncio.sleep(60)


async def start_periodic_digest_loop() -> asyncio.Task:
    return asyncio.create_task(periodic_digest_loop(), name="agent-scheduled-digest")
