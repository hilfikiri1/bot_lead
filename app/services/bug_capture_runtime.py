"""One-shot screenshot bug capture and reusable repair prompts.

Workflow:
    /bug -> screenshot with a descriptive caption

The screenshot and description become one PostgreSQL QA issue, the file is stored
under Google Drive / Баги / YYYY / MM / ISSUE-CODE, and the issue is projected to
Notion.  No Kommo or client data is modified.
"""
from __future__ import annotations

import hashlib
import html
import os
import re
from datetime import datetime, timezone
from typing import Any

from app.agent import memory
from app.agent.contracts import AgentReply
from app.services import goals_qa_service, google_drive_service, qa_projection_runtime

_INSTALLED = False


def _safe_caption(value: str | None) -> str:
    return goals_qa_service.redact_sensitive(value).strip()


def _attachment_links(issue: Any) -> list[str]:
    return [
        str(item.drive_url)
        for item in (issue.attachments or [])
        if item.upload_status == "uploaded" and item.drive_url
    ]


def build_bug_fix_prompt(issue: Any) -> str:
    """Return a copy-ready prompt grounded only in the stored QA record."""
    links = _attachment_links(issue)
    lines = [
        "Исправь следующий баг в проекте Buy & Bring Solutions.",
        "Не проводи полный аудит проекта: найди только связанные обработчики и внеси точечное исправление.",
        "",
        f"Код бага: {issue.issue_code or issue.id}",
        f"Название: {issue.title}",
        f"Тип: {issue.issue_type}",
        f"Приоритет: {issue.priority}",
        f"Модуль: {issue.module or 'не определён'}",
        f"Среда: {issue.environment or 'production'}",
        "",
        "Описание и фактический результат:",
        str(issue.description or issue.actual_result or "—"),
    ]
    if issue.expected_result:
        lines.extend(["", "Ожидаемый результат:", str(issue.expected_result)])
    if issue.reproduction_steps:
        lines.extend(["", "Шаги воспроизведения:", str(issue.reproduction_steps)])
    if issue.user_comment:
        lines.extend(["", "Дополнительные комментарии:", str(issue.user_comment)])
    if links:
        lines.extend(["", "Скриншоты и вложения в Google Drive:"])
        lines.extend(f"- {url}" for url in links)
    if issue.notion_url:
        lines.extend(["", f"Карточка Notion: {issue.notion_url}"])
    lines.extend(
        [
            "",
            "После исправления покажи:",
            "1. корневую причину;",
            "2. изменённые файлы;",
            "3. результат тестов;",
            "4. требуется ли миграция или новая переменная окружения.",
        ]
    )
    return "\n".join(lines)[:12000]


async def _ensure_bug_folder(issue: Any) -> dict[str, Any]:
    root_id = os.getenv("GOOGLE_DRIVE_QA_FOLDER_ID", "").strip()
    if not root_id:
        raise RuntimeError("GOOGLE_DRIVE_QA_FOLDER_ID не настроен")
    now = datetime.now(timezone.utc)
    bugs = await qa_projection_runtime._ensure_folder("Баги", root_id)  # noqa: SLF001
    year = await qa_projection_runtime._ensure_folder(str(now.year), str(bugs["id"]))  # noqa: SLF001
    month = await qa_projection_runtime._ensure_folder(f"{now.month:02d}", str(year["id"]))  # noqa: SLF001
    return await qa_projection_runtime._ensure_folder(  # noqa: SLF001
        issue.issue_code or f"BUG-{issue.id}", str(month["id"])
    )


async def _upload_bug_attachment(
    db: Any,
    *,
    issue: Any,
    attachment: Any,
    content: bytes,
) -> Any:
    attachment.upload_status = "uploading"
    await db.commit()
    try:
        folder = await _ensure_bug_folder(issue)
        uploaded = await google_drive_service.upload_file(
            parent_folder_id=str(folder["id"]),
            filename=google_drive_service.sanitize_filename(attachment.original_name),
            content=content,
            mime_type=attachment.mime_type or "application/octet-stream",
        )
        metadata = dict(attachment.metadata_json or {})
        metadata.update(
            {
                "drive_folder_id": str(folder["id"]),
                "drive_folder_url": folder.get("webViewLink")
                or f"https://drive.google.com/drive/folders/{folder['id']}",
            }
        )
        attachment.metadata_json = metadata
        await goals_qa_service.mark_attachment_uploaded(
            db,
            attachment=attachment,
            drive_file_id=str(uploaded["id"]),
            drive_url=str(uploaded.get("webViewLink") or ""),
        )
    except Exception as exc:
        await goals_qa_service.mark_attachment_failed(
            db,
            attachment=attachment,
            error=str(exc),
        )
    return attachment


def _folder_url(issue: Any) -> str | None:
    for item in issue.attachments or []:
        metadata = item.metadata_json or {}
        if metadata.get("drive_folder_url"):
            return str(metadata["drive_folder_url"])
    return None


def _saved_reply(issue: Any, *, drive_ok: bool, notion_error: str | None) -> AgentReply:
    lines = [
        f"✅ <b>{html.escape(issue.issue_code or str(issue.id))} сохранён</b>",
        "",
        html.escape(issue.title),
    ]
    folder_url = _folder_url(issue)
    if drive_ok and folder_url:
        lines.append(
            f'📁 <a href="{html.escape(folder_url, quote=True)}">Папка бага в Google Drive</a>'
        )
    elif not drive_ok:
        lines.append("⚠️ Скриншот сохранён локально, но загрузка в Google Drive не подтверждена.")
    if issue.notion_url:
        lines.append(
            f'📓 <a href="{html.escape(issue.notion_url, quote=True)}">Карточка бага в Notion</a>'
        )
    elif notion_error:
        lines.append("⚠️ Notion сейчас недоступен; локальная карточка не потеряна.")
    lines.extend(
        [
            "",
            "Когда решите исправлять баг, отправьте:",
            f"<code>/bug_prompt {html.escape(issue.issue_code or str(issue.id))}</code>",
            "Бот вернёт готовый промпт со ссылками на скриншоты.",
        ]
    )
    return AgentReply(
        "\n".join(lines),
        intent="bug_screenshot_saved",
        metadata={
            "issue_id": int(issue.id),
            "issue_code": issue.issue_code,
            "drive_saved": drive_ok,
            "notion_saved": bool(issue.notion_url),
        },
    )


def install_bug_capture_runtime() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True

    from app.agent import service as agent_service

    original_upload = agent_service.handle_project_file_upload
    original_handle_message = agent_service.handle_message

    async def handle_upload_with_one_shot_bug(
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
        session = await memory.get_or_create_session(
            db, telegram_user_id=int(telegram_user_id)
        )
        context = await memory.build_context(
            db, telegram_user_id=int(telegram_user_id), session=session
        )
        intake = context.get("qa_intake")
        if not isinstance(intake, dict):
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

        description = _safe_caption(caption)
        if not description:
            return AgentReply(
                "📝 Добавьте к скриншоту подпись с описанием: что сделали, что ожидали и что произошло фактически. Режим /bug остаётся активным.",
                intent="bug_caption_required",
            )

        issue, _ = await goals_qa_service.create_local_issue(
            db,
            telegram_user_id=int(telegram_user_id),
            text=description,
            issue_type=str(intake.get("issue_type") or "Bug"),
            active_project_number=context.get("active_internal_lead_number"),
            kommo_lead_id=context.get("active_kommo_lead_id"),
            trace_id=str(context.get("last_trace_id") or "") or None,
            source="telegram_screenshot",
            force_new=True,
            metadata={
                "capture_mode": "one_shot_screenshot",
                "telegram_message_id": int(telegram_message_id or 0),
            },
        )
        issue.status = "New"
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
                "caption": description,
            },
        )
        await _upload_bug_attachment(
            db, issue=issue, attachment=attachment, content=content
        )
        issue = await goals_qa_service.get_issue(
            db,
            telegram_user_id=int(telegram_user_id),
            issue_ref=int(issue.id),
        )

        notion_error = None
        try:
            issue = await qa_projection_runtime.sync_issue_to_notion(db, issue)
        except Exception as exc:
            notion_error = goals_qa_service.redact_sensitive(str(exc))[:300]

        await memory.update_context(
            db,
            session=session,
            values={"qa_intake": None, "active_qa_issue_id": None},
        )
        return _saved_reply(
            issue,
            drive_ok=attachment.upload_status == "uploaded",
            notion_error=notion_error,
        )

    async def handle_message_with_bug_prompt(
        db: Any,
        *,
        chat_id: int,
        telegram_user_id: int,
        text: str,
        source: str = "text",
        allow_conversation_passthrough: bool = False,
        active_kommo_lead_id: int | None = None,
    ) -> AgentReply:
        match = re.fullmatch(
            r"\s*/bug_prompt(?:@\w+)?\s+([A-Za-z]+-\d+|\d+)\s*",
            str(text or ""),
            flags=re.I,
        )
        if not match:
            return await original_handle_message(
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
            issue_ref=match.group(1),
        )
        if issue is None:
            return AgentReply("❓ Баг не найден. Посмотрите список командой /bugs.", intent="bug_prompt_missing")
        prompt = build_bug_fix_prompt(issue)
        return AgentReply(
            "<b>📋 Готовый промпт для исправления</b>\n\n"
            f"<pre>{html.escape(prompt)}</pre>",
            intent="bug_prompt_ready",
            metadata={"issue_id": int(issue.id), "issue_code": issue.issue_code},
        )

    agent_service.handle_project_file_upload = handle_upload_with_one_shot_bug
    agent_service.handle_message = handle_message_with_bug_prompt
