"""One-time end-of-day check-in after a new lead is assigned a call action."""
from __future__ import annotations

import html
import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm.attributes import flag_modified

from app.agent import memory
from app.database import AsyncSessionLocal
from app.models.agent_session import AgentSession
from app.services import followup_service, telegram_service, telegram_state_service

logger = logging.getLogger(__name__)
_INSTALLED_CONFIRM_ID: int | None = None
_INSTALLED_CALLBACK_ID: int | None = None
_INSTALLED_REMINDER_ID: int | None = None


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _checkin_due(timezone_name: str = "Europe/Warsaw") -> datetime:
    try:
        zone = ZoneInfo(timezone_name)
    except Exception:
        zone = ZoneInfo("Europe/Warsaw")
    now = datetime.now(zone)
    due = now.replace(hour=18, minute=0, second=0, microsecond=0)
    if due <= now:
        due = now + timedelta(hours=1)
    return due.astimezone(timezone.utc)


async def schedule_call_checkin(
    *,
    telegram_user_id: int,
    preview: dict[str, Any],
) -> None:
    due_at = _checkin_due(
        str(getattr(__import__("app.config", fromlist=["get_settings"]).get_settings(), "manager_timezone", "Europe/Warsaw"))
    )
    item = {
        "lead_id": int(preview["lead_id"]),
        "lead_number": str(preview.get("lead_number") or ""),
        "client_name": str(preview.get("client_name") or "Клиент")[:500],
        "phone": str(preview.get("phone") or "")[:100],
        "new_name": str(preview.get("new_name") or "")[:500],
        "kommo_url": str(preview.get("kommo_url") or "")[:2000],
        "due_at": due_at.isoformat(),
        "status": "scheduled",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    async with AsyncSessionLocal() as db:
        session = await memory.get_or_create_session(
            db, telegram_user_id=int(telegram_user_id)
        )
        context = dict(session.context or {})
        items = [
            dict(existing)
            for existing in (context.get("onboarding_call_checkins") or [])
            if isinstance(existing, dict)
            and int(existing.get("lead_id") or 0) != int(item["lead_id"])
            and existing.get("status") == "scheduled"
        ]
        items.append(item)
        context["onboarding_call_checkins"] = items[-20:]
        session.context = context
        flag_modified(session, "context")
        await db.commit()


def _checkin_markup(item: dict[str, Any]) -> dict[str, Any]:
    rows = [
        [
            {
                "text": "📝 Записать результат звонка",
                "callback_data": f"onboard:call:{int(item['lead_id'])}",
            }
        ]
    ]
    if item.get("kommo_url"):
        rows.append([{"text": "🔗 Открыть Kommo", "url": str(item["kommo_url"])}])
    return {"inline_keyboard": rows}


async def send_due_call_checkins(*, now: datetime | None = None, limit: int = 50) -> int:
    current = _aware(now or datetime.now(timezone.utc))
    sent = 0
    async with AsyncSessionLocal() as db:
        sessions = list(
            (
                await db.execute(
                    select(AgentSession)
                    .where(AgentSession.context.is_not(None))
                    .order_by(AgentSession.updated_at.asc())
                    .limit(max(1, min(int(limit), 200)))
                )
            ).scalars().all()
        )
        for session in sessions:
            context = dict(session.context or {})
            items = [
                dict(item)
                for item in (context.get("onboarding_call_checkins") or [])
                if isinstance(item, dict)
            ]
            changed = False
            for item in items:
                if item.get("status") != "scheduled" or not item.get("due_at"):
                    continue
                try:
                    due_at = _aware(datetime.fromisoformat(str(item["due_at"])))
                except ValueError:
                    item["status"] = "invalid"
                    changed = True
                    continue
                if due_at > current:
                    continue
                number = html.escape(str(item.get("lead_number") or "—"))
                client = html.escape(str(item.get("client_name") or "Клиент"))
                phone = html.escape(str(item.get("phone") or "—"))
                try:
                    await telegram_service.send_message(
                        int(session.telegram_user_id),
                        "📞 <b>КАК ПРОШЁЛ ЗВОНОК?</b>\n\n"
                        f"Лид: <b>№{number} · {client}</b>\n"
                        f"Телефон: <code>{phone}</code>\n\n"
                        "Зафиксируй голосом или текстом:\n"
                        "• что подтвердил клиент;\n"
                        "• что мы пообещали;\n"
                        "• какие данные получили;\n"
                        "• какой следующий шаг и срок.",
                        reply_markup=_checkin_markup(item),
                    )
                    item["status"] = "reminded"
                    item["reminded_at"] = current.isoformat()
                    sent += 1
                    changed = True
                except Exception as exc:
                    logger.warning("Could not send onboarding call check-in: %s", exc)
            if changed:
                context["onboarding_call_checkins"] = items
                session.context = context
                flag_modified(session, "context")
                await db.commit()
    return sent


async def activate_call_result_context(
    db: Any,
    *,
    telegram_user_id: int,
    lead_id: int,
) -> None:
    session = await memory.get_or_create_session(
        db, telegram_user_id=int(telegram_user_id)
    )
    session.active_kommo_lead_id = int(lead_id)
    context = dict(session.context or {})
    items = [
        dict(item)
        for item in (context.get("onboarding_call_checkins") or [])
        if isinstance(item, dict)
    ]
    selected = next(
        (item for item in items if int(item.get("lead_id") or 0) == int(lead_id)),
        None,
    )
    if selected:
        selected["status"] = "awaiting_result"
        context["active_internal_lead_number"] = selected.get("lead_number")
        context["active_lead_name"] = selected.get("new_name") or selected.get("client_name")
    context["pending_call_result"] = {
        "lead_id": int(lead_id),
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    context["onboarding_call_checkins"] = items
    session.context = context
    flag_modified(session, "context")
    await db.commit()


def install_onboarding_call_checkin_runtime() -> None:
    global _INSTALLED_CONFIRM_ID, _INSTALLED_CALLBACK_ID, _INSTALLED_REMINDER_ID

    from app.api import telegram as telegram_api
    from app.services import facebook_lead_onboarding_runtime as onboarding

    current_confirm = onboarding._confirm
    if not getattr(current_confirm, "_bbs_call_checkin", False):

        async def confirm_with_call_checkin(
            chat_id: int,
            user_id: int,
            state: dict[str, Any],
        ) -> None:
            preview = dict(state.get("current_preview") or {})
            before = len(state.get("results") or [])
            await current_confirm(chat_id, user_id, state)
            after_results = list(state.get("results") or [])
            succeeded = len(after_results) > before and not after_results[-1].get("stale")
            if succeeded and preview.get("recommended_channel") == "call":
                try:
                    await schedule_call_checkin(
                        telegram_user_id=int(user_id), preview=preview
                    )
                    await telegram_service.send_message(
                        chat_id,
                        "⏰ После звонка я спрошу результат в конце рабочего дня.",
                    )
                except Exception as exc:
                    logger.warning("Could not schedule onboarding call check-in: %s", exc)

        confirm_with_call_checkin._bbs_call_checkin = True  # type: ignore[attr-defined]
        onboarding._confirm = confirm_with_call_checkin
        _INSTALLED_CONFIRM_ID = id(confirm_with_call_checkin)

    current_callback = telegram_api._handle_manager_callback
    if not getattr(current_callback, "_bbs_call_checkin", False):

        async def callback_with_call_result(
            *,
            callback_data: str,
            chat_id: int,
            user_id: int,
            db: Any,
        ) -> bool:
            if callback_data.startswith("onboard:call:"):
                try:
                    lead_id = int(callback_data.rsplit(":", 1)[-1])
                except ValueError:
                    await telegram_service.send_message(chat_id, "Некорректный лид.")
                    return True
                await activate_call_result_context(
                    db,
                    telegram_user_id=int(user_id),
                    lead_id=lead_id,
                )
                await telegram_state_service.clear_state(user_id)
                await telegram_service.send_message(
                    chat_id,
                    "🎙 <b>ЗАПИШИ РЕЗУЛЬТАТ ЗВОНКА</b>\n\n"
                    "Отправь голос или текст обычными словами. Например:\n"
                    "«Поговорил с клиентом. Ему нужны… Мы договорились… Следующий шаг…»\n\n"
                    "Сообщение будет привязано к выбранной сделке; новый лид не создаётся.",
                )
                return True
            return await current_callback(
                callback_data=callback_data,
                chat_id=chat_id,
                user_id=user_id,
                db=db,
            )

        callback_with_call_result._bbs_call_checkin = True  # type: ignore[attr-defined]
        telegram_api._handle_manager_callback = callback_with_call_result
        _INSTALLED_CALLBACK_ID = id(callback_with_call_result)

    current_reminders = followup_service.send_due_reminders
    if not getattr(current_reminders, "_bbs_call_checkin", False):

        async def reminders_with_call_checkins(
            *,
            now: datetime | None = None,
            limit: int = 50,
        ) -> int:
            regular = await current_reminders(now=now, limit=limit)
            checkins = await send_due_call_checkins(now=now, limit=limit)
            return int(regular) + int(checkins)

        reminders_with_call_checkins._bbs_call_checkin = True  # type: ignore[attr-defined]
        followup_service.send_due_reminders = reminders_with_call_checkins
        _INSTALLED_REMINDER_ID = id(reminders_with_call_checkins)
