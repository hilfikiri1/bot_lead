"""Persistent client follow-up scheduling, reminders and response reconciliation."""
from __future__ import annotations

import hashlib
import html
import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.agent import generation as agent_generation
from app.config import get_settings
from app.database import AsyncSessionLocal
from app.models.agent_user import AgentUser
from app.models.agent_v5 import NextActionState
from app.models.client_message_draft import ClientMessageDraft
from app.services import client_language_service, client_message_service, kommo_service, telegram_service

logger = logging.getLogger(__name__)
settings = get_settings()

FOLLOWUP_PRESETS: dict[str, int] = {"tomorrow": 1, "3d": 3, "7d": 7}
_ACTIVE_STATUSES = {"scheduled", "reminded"}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _manager_tz() -> ZoneInfo:
    try:
        return ZoneInfo(settings.manager_timezone)
    except Exception:
        return ZoneInfo("Europe/Warsaw")


def enabled() -> bool:
    import os

    return os.getenv("AGENT_FOLLOWUP_ENABLED", "true").strip().casefold() in {"1", "true", "yes", "on"}


def preset_due_at(preset: str, *, now: datetime | None = None) -> datetime:
    if preset not in FOLLOWUP_PRESETS:
        raise ValueError("Неизвестный срок follow-up.")
    local_now = (now or utcnow()).astimezone(_manager_tz())
    target = local_now + timedelta(days=FOLLOWUP_PRESETS[preset])
    target = target.replace(hour=10, minute=0, second=0, microsecond=0)
    if target <= local_now:
        target += timedelta(days=1)
    return target.astimezone(timezone.utc)


def parse_custom_due_at(value: str, *, now: datetime | None = None) -> datetime:
    raw = " ".join(str(value or "").strip().split())
    if not raw:
        raise ValueError("Дата не указана.")
    tz = _manager_tz()
    local_now = (now or utcnow()).astimezone(tz)
    lowered = raw.casefold().replace("ё", "е")
    if lowered in {"завтра", "tomorrow"}:
        return preset_due_at("tomorrow", now=local_now)
    for prefix, days in (("завтра ", 1), ("сегодня ", 0)):
        if lowered.startswith(prefix):
            try:
                parsed_time = datetime.strptime(raw[len(prefix) :], "%H:%M").time()
            except ValueError as exc:
                raise ValueError("Используйте формат: завтра 10:00.") from exc
            target = datetime.combine((local_now + timedelta(days=days)).date(), parsed_time, tzinfo=tz)
            if target <= local_now:
                raise ValueError("Дата должна быть в будущем.")
            return target.astimezone(timezone.utc)
    for fmt in ("%d.%m.%Y %H:%M", "%Y-%m-%d %H:%M", "%d.%m %H:%M"):
        try:
            parsed = datetime.strptime(raw, fmt)
        except ValueError:
            continue
        if fmt == "%d.%m %H:%M":
            parsed = parsed.replace(year=local_now.year)
            candidate = parsed.replace(tzinfo=tz)
            if candidate <= local_now:
                candidate = candidate.replace(year=local_now.year + 1)
            parsed = candidate.replace(tzinfo=None)
        target = parsed.replace(tzinfo=tz)
        if target <= local_now:
            raise ValueError("Дата должна быть в будущем.")
        return target.astimezone(timezone.utc)
    raise ValueError("Не удалось распознать дату. Пример: 31.07.2026 10:00 или завтра 10:00.")


def format_due_at(value: datetime | None) -> str:
    aware = _aware(value)
    return "—" if aware is None else aware.astimezone(_manager_tz()).strftime("%d.%m.%Y %H:%M")


def followup_prompt_markup(draft_id: int) -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {"text": "Завтра", "callback_data": f"followup:set:{draft_id}:tomorrow"},
                {"text": "Через 3 дня", "callback_data": f"followup:set:{draft_id}:3d"},
            ],
            [
                {"text": "Через 7 дней", "callback_data": f"followup:set:{draft_id}:7d"},
                {"text": "Выбрать дату", "callback_data": f"followup:custom:{draft_id}"},
            ],
            [{"text": "Не напоминать", "callback_data": f"followup:none:{draft_id}"}],
        ]
    }


def reminder_markup(*, lead_id: int, lead_url: str | None = None) -> dict[str, Any]:
    rows: list[list[dict[str, str]]] = [
        [{"text": "✍️ Подготовить follow-up", "callback_data": f"followup:prepare:{lead_id}"}],
        [
            {"text": "⏰ Отложить на 3 дня", "callback_data": f"followup:snooze:{lead_id}:3d"},
            {"text": "💬 Клиент ответил", "callback_data": f"followup:replied:{lead_id}"},
        ],
        [{"text": "✅ Закрыть ожидание", "callback_data": f"followup:close:{lead_id}"}],
    ]
    if lead_url:
        rows.append([{"text": "🔗 Открыть Kommo", "url": str(lead_url)}])
    return {"inline_keyboard": rows}


async def _state_for_update(db: AsyncSession, lead_id: int) -> NextActionState:
    row = (
        await db.execute(
            select(NextActionState)
            .where(NextActionState.kommo_lead_id == int(lead_id))
            .with_for_update()
        )
    ).scalar_one_or_none()
    if row is None:
        row = NextActionState(kommo_lead_id=int(lead_id))
        db.add(row)
        await db.flush()
    return row


def _followup_meta(row: NextActionState) -> dict[str, Any]:
    return dict((row.metadata_json or {}).get("followup") or {})


def _save_followup_meta(row: NextActionState, followup: dict[str, Any]) -> None:
    metadata = dict(row.metadata_json or {})
    metadata["followup"] = followup
    row.metadata_json = metadata
    try:
        flag_modified(row, "metadata_json")
    except Exception:
        pass


async def _complete_kommo_task(task_id: int | None, *, result_text: str) -> None:
    if not task_id:
        return
    try:
        await kommo_service._request(
            "PATCH",
            "/api/v4/tasks",
            json_body=[{"id": int(task_id), "is_completed": True, "result": {"text": result_text[:1000]}}],
        )
    except Exception as exc:
        logger.warning("Could not complete Kommo follow-up task %s: %s", task_id, exc)


async def schedule_from_draft(
    db: AsyncSession,
    *,
    draft_id: int,
    telegram_user_id: int,
    due_at: datetime,
    preset: str | None = None,
) -> dict[str, Any]:
    draft = await client_message_service.get_draft(db, draft_id, lock=True)
    if draft is None:
        raise ValueError("Черновик не найден.")
    if draft.status != "sent":
        raise ValueError("Follow-up можно поставить только после подтверждённой отправки.")
    lead = await kommo_service.get_lead_details(int(draft.kommo_lead_id))
    user = (
        await db.execute(select(AgentUser).where(AgentUser.telegram_user_id == int(telegram_user_id)))
    ).scalar_one_or_none()
    if user is None:
        raise PermissionError("Пользователь Telegram не найден.")

    due_at = _aware(due_at)
    if due_at is None or due_at <= utcnow():
        raise ValueError("Срок follow-up должен быть в будущем.")
    sent_at = _aware(draft.sent_at or draft.sent_confirmed_at or draft.updated_at) or utcnow()
    row = await _state_for_update(db, int(draft.kommo_lead_id))
    marker = f"[BBS-FOLLOWUP-{draft.id}]"
    action_text = f"Проверить ответ клиента · {marker}"
    row.status = "waiting_client"
    row.waiting_on = "client"
    row.action_text = action_text
    row.due_at = due_at
    row.last_contact_at = sent_at
    row.responsible_user_id = lead.get("responsible_user_id")
    row.stale_reason = None
    followup = {
        "status": "scheduled",
        "draft_id": int(draft.id),
        "delivery_marker": draft.delivery_marker,
        "sent_at": sent_at.isoformat(),
        "due_at": due_at.isoformat(),
        "preset": preset,
        "channel": draft.channel,
        "recipient": draft.recipient,
        "client_name": draft.client_name,
        "message_preview": str(draft.body or "")[:1000],
        "scheduled_by_telegram_user_id": int(telegram_user_id),
        "scheduled_at": utcnow().isoformat(),
        "reminder_count": 0,
        "last_reminded_at": None,
        "closed_at": None,
        "closed_reason": None,
        "kommo_task_id": None,
    }
    _save_followup_meta(row, followup)
    await db.commit()

    task_id = None
    try:
        open_tasks = await kommo_service.get_open_lead_tasks(int(draft.kommo_lead_id), limit=50)
        duplicate = next((item for item in open_tasks if marker in str(item.get("text") or "")), None)
        if duplicate:
            task_id = duplicate.get("id")
        else:
            task = await kommo_service.create_lead_task(
                lead_id=int(draft.kommo_lead_id),
                text=action_text,
                complete_till=int(due_at.timestamp()),
                responsible_user_id=lead.get("responsible_user_id"),
            )
            task_id = task.get("task_id")
    except Exception as exc:
        logger.warning("Could not create Kommo follow-up task: %s", exc)

    row = await _state_for_update(db, int(draft.kommo_lead_id))
    followup = _followup_meta(row)
    followup["kommo_task_id"] = task_id
    _save_followup_meta(row, followup)
    await db.commit()
    return {
        "lead_id": int(draft.kommo_lead_id),
        "lead_name": lead.get("name"),
        "lead_url": lead.get("url"),
        "draft_id": int(draft.id),
        "due_at": due_at,
        "kommo_task_id": task_id,
    }


async def skip_for_draft(db: AsyncSession, *, draft_id: int, telegram_user_id: int) -> dict[str, Any]:
    draft = await client_message_service.get_draft(db, draft_id, lock=True)
    if draft is None:
        raise ValueError("Черновик не найден.")
    metadata = dict(draft.metadata_json or {})
    metadata["followup_skipped"] = {"telegram_user_id": int(telegram_user_id), "at": utcnow().isoformat()}
    draft.metadata_json = metadata
    try:
        flag_modified(draft, "metadata_json")
    except Exception:
        pass
    await db.commit()
    return {"draft_id": int(draft.id), "lead_id": int(draft.kommo_lead_id)}


async def close_followup(
    db: AsyncSession,
    *,
    lead_id: int,
    reason: str,
    waiting_on: str | None = None,
    action_text: str | None = None,
    incoming_at: datetime | None = None,
    incoming_message_id: str | None = None,
) -> NextActionState | None:
    row = (
        await db.execute(
            select(NextActionState)
            .where(NextActionState.kommo_lead_id == int(lead_id))
            .with_for_update()
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    followup = _followup_meta(row)
    if followup.get("status") not in _ACTIVE_STATUSES:
        return row
    sent_at = None
    if followup.get("sent_at"):
        try:
            sent_at = _aware(datetime.fromisoformat(str(followup["sent_at"])))
        except ValueError:
            sent_at = None
    incoming_at = _aware(incoming_at)
    if incoming_at and sent_at and incoming_at <= sent_at:
        return row

    task_id = followup.get("kommo_task_id")
    followup["status"] = "closed"
    followup["closed_at"] = utcnow().isoformat()
    followup["closed_reason"] = reason
    followup["incoming_message_id"] = incoming_message_id
    _save_followup_meta(row, followup)
    row.waiting_on = waiting_on
    row.status = "waiting_us" if waiting_on == "us" else "ok"
    row.action_text = action_text
    row.due_at = None
    row.stale_reason = None
    if incoming_at:
        row.last_contact_at = incoming_at
    await db.commit()
    await _complete_kommo_task(task_id, result_text=f"Follow-up closed: {reason}")
    return row


async def snooze_followup(db: AsyncSession, *, lead_id: int, due_at: datetime) -> NextActionState:
    row = await _state_for_update(db, lead_id)
    followup = _followup_meta(row)
    if followup.get("status") not in _ACTIVE_STATUSES:
        raise ValueError("Активное ожидание для этой сделки не найдено.")
    due_at = _aware(due_at)
    if due_at is None or due_at <= utcnow():
        raise ValueError("Новый срок должен быть в будущем.")
    followup["status"] = "scheduled"
    followup["due_at"] = due_at.isoformat()
    followup["last_reminded_at"] = None
    followup["snoozed_at"] = utcnow().isoformat()
    _save_followup_meta(row, followup)
    row.status = "waiting_client"
    row.waiting_on = "client"
    row.action_text = "Проверить ответ клиента"
    row.due_at = due_at
    row.stale_reason = None
    await db.commit()
    await db.refresh(row)
    return row


async def prepare_followup_draft(
    db: AsyncSession, *, lead_id: int, telegram_user_id: int
) -> ClientMessageDraft:
    lead = await kommo_service.get_lead_details(int(lead_id))
    resolution = await client_language_service.resolve_communication_language(
        db, lead=lead, explicit_language="auto"
    )
    generated = await agent_generation.generate_draft(
        kind="followup_message",
        lead=lead,
        language=resolution.language,
        manager_request=(
            "Клиент не ответил к запланированному сроку. Подготовь короткий естественный follow-up, "
            "продолжая реальную переписку и не повторяя уже заданные вопросы."
        ),
    )
    return await client_message_service.create_client_message_draft(
        db,
        telegram_user_id=telegram_user_id,
        lead=lead,
        draft=generated,
        language_source=resolution.source,
        client_id=resolution.client_id,
        channel="whatsapp",
    )


async def _notification_targets(db: AsyncSession, row: NextActionState) -> list[int]:
    targets: list[int] = []
    if row.responsible_user_id:
        result = await db.execute(
            select(AgentUser).where(
                AgentUser.kommo_user_id == int(row.responsible_user_id),
                AgentUser.status == "active",
            )
        )
        targets.extend(int(user.telegram_user_id) for user in result.scalars().all())
    if not targets and settings.telegram_owner_user_id:
        targets.append(int(settings.telegram_owner_user_id))
    if not targets:
        targets.extend(int(value) for value in settings.get_allowed_user_ids())
    return list(dict.fromkeys(targets))


async def send_due_reminders(*, now: datetime | None = None, limit: int = 50) -> int:
    now = _aware(now) or utcnow()
    sent = 0
    async with AsyncSessionLocal() as db:
        rows = list(
            (
                await db.execute(
                    select(NextActionState)
                    .where(
                        NextActionState.waiting_on == "client",
                        NextActionState.due_at.is_not(None),
                        NextActionState.due_at <= now,
                    )
                    .order_by(NextActionState.due_at.asc())
                    .limit(max(1, min(int(limit), 200)))
                )
            ).scalars().all()
        )
        for row in rows:
            followup = _followup_meta(row)
            if followup.get("status") not in _ACTIVE_STATUSES:
                continue
            last_reminded = None
            if followup.get("last_reminded_at"):
                try:
                    last_reminded = _aware(datetime.fromisoformat(str(followup["last_reminded_at"])))
                except ValueError:
                    last_reminded = None
            if last_reminded and now - last_reminded < timedelta(hours=20):
                continue
            try:
                lead = await kommo_service.get_lead_details(int(row.kommo_lead_id))
            except Exception as exc:
                logger.warning("Could not load lead for follow-up reminder: %s", exc)
                continue
            due = _aware(row.due_at)
            elapsed_days = max(0, int((now - due).total_seconds() // 86400)) if due else 0
            client_name = followup.get("client_name") or lead.get("name") or "Клиент"
            body = (
                f"⏰ <b>FOLLOW-UP</b>\n\n"
                f"Сделка: <b>{html.escape(str(lead.get('name') or row.kommo_lead_id))}</b>\n"
                f"Клиент: {html.escape(str(client_name))}\n"
                f"Сообщение отправлено: {html.escape(format_due_at(row.last_contact_at))}\n"
                f"Срок проверки: {html.escape(format_due_at(row.due_at))}\n"
                + (f"Просрочено на: {elapsed_days} дн.\n" if elapsed_days else "")
                + "\nОтвет клиента не зафиксирован."
            )
            for target in await _notification_targets(db, row):
                try:
                    await telegram_service.send_message(
                        target,
                        body,
                        reply_markup=reminder_markup(
                            lead_id=int(row.kommo_lead_id), lead_url=lead.get("url")
                        ),
                    )
                    sent += 1
                except Exception as exc:
                    logger.warning("Could not send follow-up reminder to %s: %s", target, exc)
            followup["status"] = "reminded"
            followup["last_reminded_at"] = now.isoformat()
            followup["reminder_count"] = int(followup.get("reminder_count") or 0) + 1
            _save_followup_meta(row, followup)
            row.status = "overdue"
            row.stale_reason = "клиент не ответил к сроку follow-up"
            await db.commit()
    return sent


async def periodic_followup_loop() -> None:
    import asyncio

    await asyncio.sleep(45)
    while True:
        try:
            await send_due_reminders()
        except Exception as exc:
            logger.warning("Follow-up reminder loop error: %s", exc)
        await asyncio.sleep(300)


async def start_periodic_followup_loop() -> Any:
    import asyncio

    return asyncio.create_task(periodic_followup_loop(), name="followup-reminders")


def incoming_fingerprint(*, lead_id: int, message_id: str | None, text: str) -> str:
    payload = f"{lead_id}:{message_id or ''}:{text[:500]}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]
