"""One scheduler for morning digest, evening reflection and weekly kaizen review."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.agent import digest, memory
from app.agent.contracts import AgentReply
from app.config import get_settings
from app.database import AsyncSessionLocal
from app.models.agent_session import AgentSession
from app.services import identity_service, kaizen_journal_service
from app.services.kaizen_notion_guard_runtime import guard_weekly_reply
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
        session = None
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
            # A failed Telegram delivery must not leave a hidden reflection state
            # that could capture the manager's next unrelated work message.
            if session is not None:
                try:
                    await kaizen_journal_service.clear_pending_reflection(
                        db, session=session
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


async def _rearm_failed_reminder(db: AsyncSession, entry) -> None:
    """Retry transient Telegram delivery at most three times without duplicates."""
    meta = dict(entry.analysis or {})
    scheduler = dict(meta.get("scheduler") or {})
    failures = int(scheduler.get("reminder_delivery_failures") or 0) + 1
    scheduler["reminder_delivery_failures"] = failures
    scheduler.pop("reminder_sent_at", None)
    if failures < 3:
        entry.remind_at = datetime.now(timezone.utc) + timedelta(minutes=10)
    else:
        entry.remind_at = None
        scheduler["reminder_abandoned_at"] = datetime.now(timezone.utc).isoformat()
    meta["scheduler"] = scheduler
    entry.analysis = meta
    flag_modified(entry, "analysis")
    await db.commit()


async def send_due_reflection_reminders() -> None:
    async with AsyncSessionLocal() as db:
        entries = await kaizen_journal_service.claim_due_reminders(db)
        for entry in entries:
            session = None
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
                if session is not None:
                    try:
                        await kaizen_journal_service.clear_pending_reflection(
                            db, session=session
                        )
                    except Exception:
                        await db.rollback()
                try:
                    await _rearm_failed_reminder(db, entry)
                except Exception as retry_exc:
                    await db.rollback()
                    logger.warning(
                        "Could not re-arm Kaizen reminder for user %s: %s",
                        entry.telegram_user_id,
                        retry_exc.__class__.__name__,
                    )


async def _claim_weekly_delivery(db: AsyncSession, user_id: int):
    """Claim an automatic weekly delivery while leaving manual /week untouched."""
    start, end = kaizen_journal_service.week_period()
    entry = await kaizen_journal_service.get_entry(
        db,
        telegram_user_id=user_id,
        entry_type=kaizen_journal_service.WEEKLY_ENTRY_TYPE,
        period_start=start,
        period_end=end,
    )
    if entry is None:
        daily = await kaizen_journal_service.daily_entries_for_week(
            db,
            telegram_user_id=user_id,
            start=start,
            end=end,
        )
        minimum = max(1, int(settings.agent_weekly_review_min_daily_entries or 2))
        if len(daily) < minimum:
            return None
        entry = await kaizen_journal_service.get_or_create_entry(
            db,
            telegram_user_id=user_id,
            entry_type=kaizen_journal_service.WEEKLY_ENTRY_TYPE,
            period_start=start,
            period_end=end,
            source="scheduled",
        )

    entry = await kaizen_journal_service.get_entry(
        db,
        telegram_user_id=user_id,
        entry_type=kaizen_journal_service.WEEKLY_ENTRY_TYPE,
        period_start=start,
        period_end=end,
        lock=True,
    )
    if entry is None:
        return None
    meta = dict(entry.analysis or {})
    scheduler = dict(meta.get("scheduler") or {})
    if scheduler.get("weekly_sent_at"):
        return None
    pending = bool(scheduler.get("automatic_delivery_pending"))
    if not pending:
        # Existing completed entries without this marker were requested manually.
        if entry.status == "completed":
            return None
        scheduler["automatic_delivery_pending"] = True

    claimed = scheduler.get("weekly_claimed_at")
    if claimed:
        try:
            claimed_at = datetime.fromisoformat(str(claimed).replace("Z", "+00:00"))
            if claimed_at.astimezone(timezone.utc) > datetime.now(timezone.utc) - timedelta(minutes=10):
                return None
        except (TypeError, ValueError):
            pass
    scheduler["weekly_claimed_at"] = datetime.now(timezone.utc).isoformat()
    meta["scheduler"] = scheduler
    entry.analysis = meta
    flag_modified(entry, "analysis")
    await db.commit()
    return entry


async def _set_weekly_delivery_pending(db: AsyncSession, entry) -> None:
    meta = dict(entry.analysis or {})
    scheduler = dict(meta.get("scheduler") or {})
    scheduler["automatic_delivery_pending"] = True
    scheduler.setdefault("weekly_claimed_at", datetime.now(timezone.utc).isoformat())
    meta["scheduler"] = scheduler
    entry.analysis = meta
    flag_modified(entry, "analysis")
    await db.commit()


async def _mark_weekly_delivery_sent(db: AsyncSession, entry) -> None:
    meta = dict(entry.analysis or {})
    scheduler = dict(meta.get("scheduler") or {})
    scheduler["automatic_delivery_pending"] = False
    scheduler["weekly_sent_at"] = datetime.now(timezone.utc).isoformat()
    scheduler.pop("weekly_claimed_at", None)
    meta["scheduler"] = scheduler
    entry.analysis = meta
    flag_modified(entry, "analysis")
    await db.commit()


async def _release_weekly_delivery_claim(db: AsyncSession, entry) -> None:
    meta = dict(entry.analysis or {})
    scheduler = dict(meta.get("scheduler") or {})
    scheduler["automatic_delivery_pending"] = True
    scheduler.pop("weekly_claimed_at", None)
    meta["scheduler"] = scheduler
    entry.analysis = meta
    flag_modified(entry, "analysis")
    await db.commit()


async def send_weekly_review(*, user_id: int, chat_id: int | None = None) -> None:
    target_chat = chat_id or user_id
    async with AsyncSessionLocal() as db:
        identity_service.set_current_user(
            await identity_service.get_user_by_telegram_id(db, user_id)
        )
        claimed = await _claim_weekly_delivery(db, user_id)
        if claimed is None:
            return
        entry = claimed
        try:
            entry, analysis_ok = await kaizen_journal_service.build_weekly_review(
                db,
                telegram_user_id=user_id,
                week_start=claimed.period_start,
                force_rebuild=False,
            )
            # build_weekly_review replaces the analysis object, so restore delivery
            # metadata before the external Telegram call.
            await _set_weekly_delivery_pending(db, entry)
            reply = AgentReply(
                kaizen_journal_service.format_weekly_review(
                    entry, analysis_ok=analysis_ok
                ),
                reply_markup=kaizen_journal_service.weekly_review_markup(entry),
                intent="weekly_review",
                metadata={"entry_id": int(entry.id), "scheduled": True},
            )
            guarded = await guard_weekly_reply(reply)
            await send_message(
                target_chat,
                guarded.text,
                reply_markup=guarded.reply_markup,
            )
            await _mark_weekly_delivery_sent(db, entry)
        except Exception:
            try:
                await _release_weekly_delivery_claim(db, entry)
            except Exception:
                await db.rollback()
            raise


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
