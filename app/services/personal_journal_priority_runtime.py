"""Give an explicitly selected personal journal priority over stale QA intake.

The mobile /bug flow intentionally waits for a later text/voice description, but the
owner can switch modes before providing that description.  When the owner taps the
personal-journal button, that explicit destination wins: stale QA intake is cleared
and the next natural-language message is saved as a personal transcript.
"""
from __future__ import annotations

import html
from typing import Any

from app.agent import memory
from app.agent.contracts import AgentReply
from app.services import personal_journal_runtime

_INSTALLED = False
_PENDING_KEY = "pending_daily_reflection"


def _saved_reply(entry: Any, saved: dict[str, Any]) -> AgentReply:
    status = str(saved.get("notion_status") or "")
    if status == "ok":
        text = "✅ <b>Личная запись сохранена в Notion.</b>"
    elif status == "not_configured":
        text = (
            "✅ <b>Личная запись сохранена.</b>\n\n"
            "⚠️ Страница Notion не настроена."
        )
    else:
        text = (
            "✅ <b>Личная запись сохранена локально.</b>\n\n"
            "⚠️ Notion сейчас не обновился; запись не потеряна."
        )
    return AgentReply(
        text,
        intent="personal_journal_saved",
        metadata={
            "entry_id": int(entry.id),
            "personal_entry_id": saved.get("id"),
            "notion_status": status,
            "notion_url": saved.get("notion_url"),
        },
    )


async def _clear_stale_qa(db: Any, *, session: Any) -> None:
    context = session.context or {}
    if not isinstance(context, dict):
        return
    if context.get("qa_intake") is None and context.get("active_qa_issue_id") is None:
        return
    await memory.update_context(
        db,
        session=session,
        values={"qa_intake": None, "active_qa_issue_id": None},
    )


def install_personal_journal_priority_runtime() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True

    from app.agent import service as agent_service

    original_message = agent_service.handle_message
    original_callback = agent_service.handle_callback

    async def message(
        db: Any,
        *,
        chat_id: int,
        telegram_user_id: int,
        text: str,
        source: str = "text",
        allow_conversation_passthrough: bool = False,
        active_kommo_lead_id: int | None = None,
    ) -> AgentReply:
        raw = str(text or "").strip()
        if raw.startswith("/"):
            return await original_message(
                db,
                chat_id=chat_id,
                telegram_user_id=telegram_user_id,
                text=text,
                source=source,
                allow_conversation_passthrough=allow_conversation_passthrough,
                active_kommo_lead_id=active_kommo_lead_id,
            )

        session = await memory.get_or_create_session(
            db, telegram_user_id=int(telegram_user_id)
        )
        pending = dict((session.context or {}).get(_PENDING_KEY) or {})
        if str(pending.get("scope") or "") != personal_journal_runtime.PERSONAL:
            return await original_message(
                db,
                chat_id=chat_id,
                telegram_user_id=telegram_user_id,
                text=text,
                source=source,
                allow_conversation_passthrough=allow_conversation_passthrough,
                active_kommo_lead_id=active_kommo_lead_id,
            )

        # The owner explicitly selected Personal Journal.  Do not let an older
        # screenshot/bug intake consume this voice transcript.
        await _clear_stale_qa(db, session=session)
        try:
            entry, saved = await personal_journal_runtime.save_personal_transcript(
                db,
                telegram_user_id=telegram_user_id,
                session=session,
                text=raw,
                source=source,
            )
        except ValueError as exc:
            return AgentReply(
                f"❓ {html.escape(str(exc))}",
                intent="personal_journal_followup",
            )
        return _saved_reply(entry, saved)

    async def callback(
        db: Any,
        *,
        callback_data: str,
        telegram_user_id: int,
        chat_id: int | None = None,
    ) -> AgentReply:
        reply = await original_callback(
            db,
            callback_data=callback_data,
            telegram_user_id=telegram_user_id,
            chat_id=chat_id,
        )
        if str(callback_data or "").startswith("agent:reflection:personal:"):
            session = await memory.get_or_create_session(
                db, telegram_user_id=int(telegram_user_id)
            )
            await _clear_stale_qa(db, session=session)
        return reply

    agent_service.handle_message = message
    agent_service.handle_callback = callback
