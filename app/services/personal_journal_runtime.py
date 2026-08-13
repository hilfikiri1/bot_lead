"""Transcript-only personal journal routing.

When the owner explicitly enters the personal journal, every following non-slash
text/voice message is a diary entry, never an agent command. Personal entries are
stored locally first and appended verbatim (post secret-redaction) to one Notion
page. They never feed the company daily/weekly Kaizen analysis.
"""
from __future__ import annotations

import hashlib
import html
import logging
import os
from typing import Any

from sqlalchemy.orm.attributes import flag_modified

from app.agent import memory, notion_gateway
from app.agent.contracts import AgentReply
from app.services import kaizen_journal_service, kaizen_source_guard_runtime

logger = logging.getLogger(__name__)
_INSTALLED = False
_PENDING_KEY = "pending_daily_reflection"
PERSONAL = "personal"
PERSONAL_ENTRY_TYPE = "daily_personal"
# Verified workspace page: "👤 Личный дневник — итоги дня".
# Environment configuration still wins, but this keeps the owner-only bot working
# even before Railway receives the variable.
DEFAULT_PERSONAL_JOURNAL_PAGE_ID = "3ba8a54a-26de-8188-b077-ccafac277a84"


def personal_page_id() -> str:
    return (
        os.getenv("NOTION_PERSONAL_JOURNAL_PAGE_ID", "").strip()
        or DEFAULT_PERSONAL_JOURNAL_PAGE_ID
    )


def _chunks(text: str, size: int = 1800) -> list[str]:
    value = str(text or "").strip()
    return [value[i : i + size] for i in range(0, len(value), size)] if value else []


def _rt(text: str) -> list[dict[str, Any]]:
    return [{"type": "text", "text": {"content": str(text)[:1900]}}]


def _entry_id(user_id: int, local_day: str, raw: str) -> str:
    return hashlib.sha256(f"{int(user_id)}:{local_day}:{raw}".encode("utf-8")).hexdigest()[:20]


async def _append_to_notion(*, marker: str, raw: str, recorded_at: Any) -> tuple[str, str | None]:
    page_id = personal_page_id()
    if not page_id:
        return "not_configured", None

    marker_text = f"BBS-PERSONAL-{marker}"
    cursor = None
    for _ in range(20):
        params: dict[str, Any] = {"page_size": 100}
        if cursor:
            params["start_cursor"] = cursor
        listing = await notion_gateway._request(  # noqa: SLF001
            "GET", f"/blocks/{page_id}/children", params=params
        )
        for block in listing.get("results") or []:
            kind = str(block.get("type") or "")
            plain = "".join(
                str(item.get("plain_text") or "")
                for item in (block.get(kind) or {}).get("rich_text") or []
            )
            if marker_text in plain:
                return "ok", notion_gateway.notion_page_url(page_id)
        if not listing.get("has_more"):
            break
        cursor = listing.get("next_cursor")

    title = recorded_at.strftime("%d.%m.%Y · %H:%M")
    blocks: list[dict[str, Any]] = [
        {
            "object": "block",
            "type": "heading_3",
            "heading_3": {"rich_text": _rt(title)},
        }
    ]
    blocks.extend(
        {
            "object": "block",
            "type": "paragraph",
            "paragraph": {"rich_text": _rt(chunk)},
        }
        for chunk in _chunks(raw)
    )
    blocks.extend(
        [
            {
                "object": "block",
                "type": "paragraph",
                "paragraph": {"rich_text": _rt(marker_text)},
            },
            {"object": "block", "type": "divider", "divider": {}},
        ]
    )
    await notion_gateway._request(  # noqa: SLF001
        "PATCH", f"/blocks/{page_id}/children", json={"children": blocks[:100]}
    )
    return "ok", notion_gateway.notion_page_url(page_id)


async def save_personal_transcript(
    db: Any,
    *,
    telegram_user_id: int,
    session: Any,
    text: str,
    source: str,
) -> tuple[Any, dict[str, Any]]:
    raw = kaizen_source_guard_runtime.redact_journal_text(text).strip()
    if not raw:
        raise ValueError("Личная запись пустая.")

    now = kaizen_journal_service.local_now()
    day = now.date()
    marker = _entry_id(telegram_user_id, day.isoformat(), raw)
    entry = await kaizen_journal_service.get_or_create_entry(
        db,
        telegram_user_id=int(telegram_user_id),
        entry_type=PERSONAL_ENTRY_TYPE,
        period_start=day,
        period_end=day,
        source=kaizen_source_guard_runtime.storage_source(source),
    )

    analysis = dict(entry.analysis or {})
    journal = dict(analysis.get("personal_transcript_v1") or {})
    items = [dict(item) for item in journal.get("entries") or [] if isinstance(item, dict)]
    existing = next((item for item in items if str(item.get("id") or "") == marker), None)
    if existing is None:
        item = {
            "id": marker,
            "recorded_at": now.isoformat(),
            "text": raw,
            "source": kaizen_source_guard_runtime.storage_source(source),
            "notion_status": "pending",
        }
        items.append(item)
        prefix = f"[{now.strftime('%H:%M')}] "
        entry.raw_text = (
            f"{entry.raw_text}\n\n{prefix}{raw}" if entry.raw_text else f"{prefix}{raw}"
        )[:50_000]
    else:
        item = existing

    analysis["personal_transcript_v1"] = {"version": 1, "entries": items[-500:]}
    entry.analysis = analysis
    flag_modified(entry, "analysis")
    entry.status = "completed"
    entry.remind_at = None
    entry.source = kaizen_source_guard_runtime.storage_source(source)
    await kaizen_journal_service.clear_pending_reflection(db, session=session)
    await db.commit()  # durable local transcript before Notion

    notion_status = "failed"
    notion_url = None
    notion_error = None
    try:
        notion_status, notion_url = await _append_to_notion(
            marker=marker, raw=raw, recorded_at=now
        )
    except Exception as exc:
        notion_error = exc.__class__.__name__
        logger.warning("Personal journal Notion append unavailable: %s", notion_error)

    analysis = dict(entry.analysis or {})
    journal = dict(analysis.get("personal_transcript_v1") or {})
    updated_items = [dict(x) for x in journal.get("entries") or [] if isinstance(x, dict)]
    for current in updated_items:
        if str(current.get("id") or "") == marker:
            current["notion_status"] = notion_status
            current["notion_url"] = notion_url
            if notion_error:
                current["notion_error_type"] = notion_error
    analysis["personal_transcript_v1"] = {"version": 1, "entries": updated_items[-500:]}
    entry.analysis = analysis
    flag_modified(entry, "analysis")
    await db.commit()

    return entry, {
        "id": marker,
        "recorded_at": now.isoformat(),
        "notion_status": notion_status,
        "notion_url": notion_url,
        "notion_error_type": notion_error,
    }


def _rename_personal_buttons(markup: Any) -> Any:
    if not isinstance(markup, dict):
        return markup
    keyboard = markup.get("inline_keyboard")
    if not isinstance(keyboard, list):
        return markup
    for row in keyboard:
        if not isinstance(row, list):
            continue
        for button in row:
            if not isinstance(button, dict):
                continue
            callback = str(button.get("callback_data") or "")
            if callback.startswith("agent:reflection:personal:"):
                button["text"] = "👤 Добавить личную запись"
    return markup


def install_personal_journal_runtime() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True

    from app.agent import service as agent_service

    original_message = agent_service.handle_message

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
        # Explicit slash commands never need journal state and must preserve the
        # lightweight command/test path used by the rest of the agent.
        if raw.startswith("/"):
            reply = await original_message(
                db,
                chat_id=chat_id,
                telegram_user_id=telegram_user_id,
                text=text,
                source=source,
                allow_conversation_passthrough=allow_conversation_passthrough,
                active_kommo_lead_id=active_kommo_lead_id,
            )
            reply.reply_markup = _rename_personal_buttons(reply.reply_markup)
            return reply

        session = await memory.get_or_create_session(
            db, telegram_user_id=int(telegram_user_id)
        )
        pending = dict((session.context or {}).get(_PENDING_KEY) or {})
        scope = str(pending.get("scope") or "")

        # Inside personal journal, natural language can never become a weekly-review
        # command. Only an explicit slash command is allowed to escape the diary.
        if scope == PERSONAL and raw:
            try:
                entry, saved = await save_personal_transcript(
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
            status = str(saved.get("notion_status") or "")
            if status == "ok":
                text_reply = "✅ <b>Личная запись сохранена в Notion.</b>"
            elif status == "not_configured":
                text_reply = "✅ <b>Личная запись сохранена.</b>\n\n⚠️ Страница Notion не настроена."
            else:
                text_reply = (
                    "✅ <b>Личная запись сохранена локально.</b>\n\n"
                    "⚠️ Notion сейчас не обновился; запись не потеряна."
                )
            return AgentReply(
                text_reply,
                intent="personal_journal_saved",
                metadata={
                    "entry_id": int(entry.id),
                    "personal_entry_id": saved.get("id"),
                    "notion_status": status,
                    "notion_url": saved.get("notion_url"),
                },
            )

        reply = await original_message(
            db,
            chat_id=chat_id,
            telegram_user_id=telegram_user_id,
            text=text,
            source=source,
            allow_conversation_passthrough=allow_conversation_passthrough,
            active_kommo_lead_id=active_kommo_lead_id,
        )
        reply.reply_markup = _rename_personal_buttons(reply.reply_markup)
        return reply

    agent_service.handle_message = message

    # Also rename the button on the initial evening invitation.
    original_markup = kaizen_journal_service.reflection_invitation_markup

    def invitation_markup(day: Any = None) -> dict[str, Any]:
        return _rename_personal_buttons(original_markup(day))

    kaizen_journal_service.reflection_invitation_markup = invitation_markup
