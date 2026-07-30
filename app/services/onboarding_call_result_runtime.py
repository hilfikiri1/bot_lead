"""Safely route the next text/voice after an onboarding call check-in.

The wrapper is installed after every other agent runtime. A pending call result is
converted into an explicit project update, so the voice pipeline cannot fall through
to legacy new-lead/call-analysis creation.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.orm.attributes import flag_modified

_INSTALLED = False
_PENDING_TTL = timedelta(hours=12)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _pending_is_fresh(pending: dict[str, Any]) -> bool:
    try:
        started = _aware(datetime.fromisoformat(str(pending.get("started_at") or "")))
    except (TypeError, ValueError):
        return False
    return datetime.now(timezone.utc) - started <= _PENDING_TTL


def _selected_checkin(context: dict[str, Any], lead_id: int) -> dict[str, Any] | None:
    return next(
        (
            dict(item)
            for item in (context.get("onboarding_call_checkins") or [])
            if isinstance(item, dict) and int(item.get("lead_id") or 0) == int(lead_id)
        ),
        None,
    )


async def _clear_pending(db: Any, session: Any, *, lead_id: int, status: str) -> None:
    context = dict(session.context or {})
    items = [
        dict(item)
        for item in (context.get("onboarding_call_checkins") or [])
        if isinstance(item, dict)
    ]
    for item in items:
        if int(item.get("lead_id") or 0) == int(lead_id):
            item["status"] = status
            item[f"{status}_at"] = datetime.now(timezone.utc).isoformat()
    context["onboarding_call_checkins"] = items
    context["pending_call_result"] = None
    session.context = context
    flag_modified(session, "context")
    await db.commit()


def install_onboarding_call_result_runtime() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True

    from app.agent import service as agent_service

    original_handle = agent_service.handle_message

    async def handle_with_pending_call_result(
        db: Any,
        *,
        chat_id: int,
        telegram_user_id: int,
        text: str,
        source: str = "text",
        allow_conversation_passthrough: bool = False,
        active_kommo_lead_id: int | None = None,
    ):
        session = await agent_service.memory.get_or_create_session(
            db, telegram_user_id=int(telegram_user_id)
        )
        context = dict(session.context or {})
        pending = (
            dict(context.get("pending_call_result") or {})
            if isinstance(context.get("pending_call_result"), dict)
            else None
        )
        raw = str(text or "").strip()

        # Commands remain commands. The call-result state stays available until
        # the manager sends a normal text or voice response, or until it expires.
        if not pending or not raw or raw.startswith("/"):
            return await original_handle(
                db,
                chat_id=chat_id,
                telegram_user_id=telegram_user_id,
                text=text,
                source=source,
                allow_conversation_passthrough=allow_conversation_passthrough,
                active_kommo_lead_id=active_kommo_lead_id,
            )

        lead_id = int(pending.get("lead_id") or 0)
        if not lead_id or not _pending_is_fresh(pending):
            if lead_id:
                await _clear_pending(db, session, lead_id=lead_id, status="expired")
            else:
                context["pending_call_result"] = None
                session.context = context
                flag_modified(session, "context")
                await db.commit()
            return await original_handle(
                db,
                chat_id=chat_id,
                telegram_user_id=telegram_user_id,
                text=text,
                source=source,
                allow_conversation_passthrough=allow_conversation_passthrough,
                active_kommo_lead_id=active_kommo_lead_id,
            )

        selected = _selected_checkin(context, lead_id) or {}
        internal = str(
            selected.get("lead_number")
            or context.get("active_internal_lead_number")
            or ""
        ).strip()
        target_label = f"проекту {internal}" if internal else f"сделке #{lead_id}"
        routed_text = (
            f"По {target_label} поговорил с клиентом. "
            f"Результат разговора: {raw}"
        )

        reply = await original_handle(
            db,
            chat_id=chat_id,
            telegram_user_id=telegram_user_id,
            text=routed_text,
            source=source,
            # Never permit the voice pipeline to interpret this as a new client
            # conversation after the target was explicitly selected.
            allow_conversation_passthrough=False,
            active_kommo_lead_id=lead_id,
        )
        await _clear_pending(db, session, lead_id=lead_id, status="captured")
        return reply

    handle_with_pending_call_result._bbs_onboarding_call_result = True  # type: ignore[attr-defined]
    agent_service.handle_message = handle_with_pending_call_result
