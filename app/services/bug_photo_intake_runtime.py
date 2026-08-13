"""Allow /bug -> screenshot -> text/voice description as a two-step flow.

The older one-shot bug capture required the screenshot caption itself to contain the
whole description. Mobile users naturally send the image first and explain it in the
next message. This runtime persists the screenshot immediately, keeps QA intake open,
and enriches the same issue when the next non-command text/voice transcript arrives.
"""
from __future__ import annotations

import hashlib
import html
from datetime import datetime, timezone
from typing import Any

from app.agent import memory
from app.agent.contracts import AgentReply
from app.services import bug_capture_runtime, goals_qa_service, qa_projection_runtime

_INSTALLED = False
_PENDING_ISSUE_KEY = "pending_screenshot_issue_id"
_PLACEHOLDER = "Скриншот бага получен. Описание пользователя ожидается следующим сообщением."


def _is_image(*, mime_type: str | None, kind: str | None) -> bool:
    return str(kind or "").casefold() == "photo" or str(mime_type or "").casefold().startswith("image/")


def _drive_ok(issue: Any) -> bool:
    return any(str(item.upload_status or "") == "uploaded" for item in (issue.attachments or []))


def _waiting_description_reply(issue: Any, attachment: Any) -> AgentReply:
    lines = [
        f"📸 <b>{html.escape(issue.issue_code or str(issue.id))}: скриншот сохранён</b>",
        "",
        "Теперь напиши или надиктуй отдельным сообщением, что произошло, что ожидалось и что нужно исправить.",
        "Я добавлю описание к <b>этой же</b> QA-карточке — второй баг не создам.",
    ]
    folder_url = bug_capture_runtime._folder_url(issue)  # noqa: SLF001
    if attachment.upload_status == "uploaded" and folder_url:
        lines.extend(["", f'📁 <a href="{html.escape(folder_url, quote=True)}">Скриншот в Google Drive</a>'])
    elif attachment.upload_status != "uploaded":
        error = goals_qa_service.redact_sensitive(str(attachment.error_message or "неизвестная ошибка"))[:250]
        lines.extend([
            "",
            "⚠️ QA-карточка создана, но загрузка изображения в Google Drive не подтвердилась.",
            f"Ошибка: {html.escape(error)}",
            "Режим /bug остаётся активным; после исправления Drive можно отправить фото повторно.",
        ])
    return AgentReply(
        "\n".join(lines)[:3900],
        intent="bug_screenshot_waiting_description",
        metadata={
            "issue_id": int(issue.id),
            "issue_code": issue.issue_code,
            "drive_saved": attachment.upload_status == "uploaded",
            "awaiting_description": True,
        },
    )


async def _create_screenshot_first_issue(
    db: Any,
    *,
    session: Any,
    context: dict[str, Any],
    telegram_user_id: int,
    telegram_message_id: int | None,
    filename: str,
    mime_type: str,
    content: bytes,
    kind: str | None,
    intake: dict[str, Any],
) -> AgentReply:
    issue, _ = await goals_qa_service.create_local_issue(
        db,
        telegram_user_id=int(telegram_user_id),
        text=_PLACEHOLDER,
        issue_type=str(intake.get("issue_type") or "Bug"),
        active_project_number=context.get("active_internal_lead_number"),
        kommo_lead_id=context.get("active_kommo_lead_id"),
        trace_id=str(context.get("last_trace_id") or "") or None,
        source="telegram_screenshot",
        force_new=True,
        metadata={
            "capture_mode": "screenshot_then_description",
            "telegram_message_id": int(telegram_message_id or 0),
            "description_pending": True,
        },
    )
    issue.status = "Need details"
    await db.commit()

    attachment = await goals_qa_service.add_attachment_record(
        db,
        issue=issue,
        original_name=filename,
        mime_type=mime_type,
        size_bytes=len(content),
        telegram_file_id=None,
        checksum=hashlib.sha256(content).hexdigest(),
        metadata={
            "telegram_message_id": int(telegram_message_id or 0),
            "kind": kind or "photo",
            "description_pending": True,
        },
    )
    await bug_capture_runtime._upload_bug_attachment(  # noqa: SLF001
        db, issue=issue, attachment=attachment, content=content
    )
    issue = await goals_qa_service.get_issue(
        db,
        telegram_user_id=int(telegram_user_id),
        issue_ref=int(issue.id),
    )

    pending = dict(intake)
    pending[_PENDING_ISSUE_KEY] = int(issue.id)
    pending["screenshot_received_at"] = datetime.now(timezone.utc).isoformat()
    await memory.update_context(
        db,
        session=session,
        values={"qa_intake": pending, "active_qa_issue_id": int(issue.id)},
    )
    attachment = next((item for item in (issue.attachments or []) if int(item.id) == int(attachment.id)), attachment)
    return _waiting_description_reply(issue, attachment)


async def _finish_screenshot_issue(
    db: Any,
    *,
    session: Any,
    issue: Any,
    description: str,
    source: str,
) -> AgentReply:
    safe = goals_qa_service.redact_sensitive(description).strip()
    if len(safe) < 5:
        return AgentReply(
            "📝 Описание слишком короткое. Напиши или надиктуй, что сделал, что ожидал и что произошло фактически.",
            intent="bug_description_required",
        )

    issue.issue_type = goals_qa_service.classify_issue(safe, "Bug")
    issue.module = goals_qa_service.infer_module(safe)
    issue.priority = goals_qa_service.infer_priority(safe)
    issue.title = goals_qa_service.issue_title(safe, issue.issue_type)
    issue.description = safe
    issue.actual_result = safe
    issue.status = "New" if len(safe) >= 12 else "Need details"
    issue.dedupe_key = (
        goals_qa_service.issue_dedupe_key(
            issue_type=issue.issue_type,
            module=issue.module,
            title=issue.title,
            description=safe,
        )
        + f":{int(issue.id)}"
    )[:128]
    metadata = dict(issue.metadata_json or {})
    metadata.update(
        {
            "description_pending": False,
            "description_source": source,
            "description_received_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    issue.metadata_json = metadata
    await db.commit()
    try:
        await db.refresh(issue)
    except Exception:
        pass

    notion_error = None
    try:
        issue = await qa_projection_runtime.sync_issue_to_notion(db, issue)
    except Exception as exc:
        notion_error = goals_qa_service.redact_sensitive(str(exc))[:300]

    await memory.update_context(
        db,
        session=session,
        values={"qa_intake": None, "active_qa_issue_id": int(issue.id)},
    )
    return bug_capture_runtime._saved_reply(  # noqa: SLF001
        issue,
        drive_ok=_drive_ok(issue),
        notion_error=notion_error,
    )


def install_bug_photo_intake_runtime() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True

    from app.agent import service as agent_service

    original_upload = agent_service.handle_project_file_upload
    original_message = agent_service.handle_message

    async def upload(
        db: Any,
        *,
        chat_id: int,
        telegram_user_id: int,
        telegram_message_id: int | None = None,
        filename: str,
        mime_type: str,
        content: bytes,
        caption: str | None = None,
        kind: str | None = None,
    ) -> AgentReply:
        if str(caption or "").strip() or not _is_image(mime_type=mime_type, kind=kind):
            return await original_upload(
                db,
                chat_id=chat_id,
                telegram_user_id=telegram_user_id,
                telegram_message_id=telegram_message_id,
                filename=filename,
                mime_type=mime_type,
                content=content,
                caption=caption,
                kind=kind,
            )
        session = await memory.get_or_create_session(db, telegram_user_id=int(telegram_user_id))
        context = await memory.build_context(db, telegram_user_id=int(telegram_user_id), session=session)
        intake = context.get("qa_intake")
        if not isinstance(intake, dict) or str(intake.get("issue_type") or "Bug") != "Bug":
            return await original_upload(
                db,
                chat_id=chat_id,
                telegram_user_id=telegram_user_id,
                telegram_message_id=telegram_message_id,
                filename=filename,
                mime_type=mime_type,
                content=content,
                caption=caption,
                kind=kind,
            )
        return await _create_screenshot_first_issue(
            db,
            session=session,
            context=context,
            telegram_user_id=telegram_user_id,
            telegram_message_id=telegram_message_id,
            filename=filename,
            mime_type=mime_type,
            content=content,
            kind=kind,
            intake=intake,
        )

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
        session = await memory.get_or_create_session(db, telegram_user_id=int(telegram_user_id))
        context = await memory.build_context(db, telegram_user_id=int(telegram_user_id), session=session)
        intake = context.get("qa_intake")
        pending_id = int(intake.get(_PENDING_ISSUE_KEY) or 0) if isinstance(intake, dict) else 0
        if not pending_id:
            return await original_message(
                db,
                chat_id=chat_id,
                telegram_user_id=telegram_user_id,
                text=text,
                source=source,
                allow_conversation_passthrough=allow_conversation_passthrough,
                active_kommo_lead_id=active_kommo_lead_id,
            )
        issue = await goals_qa_service.get_issue(
            db,
            telegram_user_id=int(telegram_user_id),
            issue_ref=pending_id,
        )
        if issue is None:
            await memory.update_context(db, session=session, values={"qa_intake": None, "active_qa_issue_id": None})
            return AgentReply(
                "⚠️ Не удалось найти QA-карточку для этого скриншота. Запусти /bug ещё раз.",
                intent="bug_pending_issue_missing",
            )
        return await _finish_screenshot_issue(
            db,
            session=session,
            issue=issue,
            description=raw,
            source=source,
        )

    agent_service.handle_project_file_upload = upload
    agent_service.handle_message = message
