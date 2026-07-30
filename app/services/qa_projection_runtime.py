"""Durable QA attachments and Notion projections.

Installed after goals_qa_runtime. It keeps PostgreSQL as the source of truth, updates
existing Notion pages instead of duplicating them, and marks an attachment uploaded
only after Google Drive confirms the file creation.
"""
from __future__ import annotations

import hashlib
import html
import os
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from app.agent import executor, memory, notion_gateway
from app.agent.contracts import AgentReply
from app.models.goal_qa import BusinessGoal
from app.services import goals_qa_service, google_drive_service

_INSTALLED = False


def _payload(meta: dict[str, Any], value: Any) -> dict[str, Any] | None:
    kind = str(meta.get("type") or "")
    if value is None or value == "":
        return None
    if kind == "title":
        return notion_gateway._title(str(value))  # noqa: SLF001
    if kind in {"rich_text", "text"}:
        return notion_gateway._rich_text(str(value))  # noqa: SLF001
    if kind == "select":
        return {"select": {"name": str(value)}}
    if kind == "status":
        return {"status": {"name": str(value)}}
    if kind == "number":
        return {"number": float(value)}
    if kind == "date":
        return {"date": {"start": str(value)}}
    if kind == "url":
        return {"url": str(value)}
    if kind == "files" and isinstance(value, list):
        return {
            "files": [
                {
                    "name": str(item.get("name") or "attachment")[:255],
                    "type": "external",
                    "external": {"url": str(item["url"])},
                }
                for item in value[:20]
                if isinstance(item, dict) and item.get("url")
            ]
        }
    return None


def _find(meta: dict[str, Any], *names: str, kind: str | None = None) -> str | None:
    for name in names:
        prop = meta.get(name)
        if prop and (kind is None or str(prop.get("type")) == kind):
            return name
    if kind:
        return next((name for name, prop in meta.items() if str(prop.get("type")) == kind), None)
    return None


async def sync_issue_to_notion(db: Any, issue: Any) -> Any:
    source_id = os.getenv("NOTION_QA_DATA_SOURCE_ID", "").strip()
    if not source_id:
        raise notion_gateway.OperationalNotionError("NOTION_QA_DATA_SOURCE_ID не настроен.")
    source = await notion_gateway.retrieve_data_source(source_id)
    meta = source.get("properties") or {}
    title_name = _find(meta, "Название", "Name", kind="title")
    if not title_name:
        raise notion_gateway.OperationalNotionError("В QA-базе нет title-свойства.")
    values = {
        title_name: f"{issue.issue_code} — {issue.title}",
        "Тип": issue.issue_type,
        "Статус": issue.status,
        "Приоритет": issue.priority,
        "Модуль": issue.module,
        "Среда": issue.environment,
        "Описание": issue.description,
        "Ожидаемый результат": issue.expected_result,
        "Фактический результат": issue.actual_result,
        "Шаги воспроизведения": issue.reproduction_steps,
        "Trace ID": issue.trace_id,
        "Telegram user": str(issue.telegram_user_id),
        "Связанный проект": issue.active_project_number,
        "Kommo ID": issue.kommo_lead_id,
        "Версия приложения": issue.app_version,
        "Railway deployment": issue.railway_deployment,
        "GitHub PR": issue.github_pr,
        "Корневая причина": issue.root_cause,
        "Решение": issue.resolution,
        "Комментарий пользователя": issue.user_comment,
        "Результат проверки": issue.retest_result,
    }
    properties: dict[str, Any] = {}
    for name, value in values.items():
        if name not in meta:
            continue
        prop = _payload(meta[name], value)
        if prop is not None:
            properties[name] = prop
    files_name = _find(meta, "Вложения", "Скриншоты", "Видео", kind="files")
    if files_name:
        links = [
            {"name": item.original_name, "url": item.drive_url}
            for item in (issue.attachments or [])
            if item.upload_status == "uploaded" and item.drive_url
        ]
        prop = _payload(meta[files_name], links)
        if prop is not None:
            properties[files_name] = prop
    if issue.notion_page_id:
        data = await notion_gateway._request(  # noqa: SLF001
            "PATCH",
            f"/pages/{notion_gateway._page_id(issue.notion_page_id)}",  # noqa: SLF001
            json={"properties": properties},
        )
    else:
        data = await notion_gateway._request(  # noqa: SLF001
            "POST",
            "/pages",
            json={
                "parent": {
                    "type": "data_source_id",
                    "data_source_id": notion_gateway._data_source_id(source_id),  # noqa: SLF001
                },
                "properties": properties,
                "children": [
                    {
                        "object": "block",
                        "type": "paragraph",
                        "paragraph": {
                            "rich_text": [
                                {
                                    "type": "text",
                                    "text": {
                                        "content": str(issue.description or "—")[:1900]
                                    },
                                }
                            ]
                        },
                    }
                ],
            },
        )
    issue.notion_page_id = str(data.get("id") or issue.notion_page_id or "") or None
    issue.notion_url = data.get("url") or issue.notion_url or (
        notion_gateway.notion_page_url(issue.notion_page_id)
        if issue.notion_page_id
        else None
    )
    await db.commit()
    return issue


async def sync_goal_to_notion(db: Any, goal: BusinessGoal) -> BusinessGoal:
    source_id = os.getenv("NOTION_GOALS_DATA_SOURCE_ID", "").strip()
    if not source_id:
        return goal
    source = await notion_gateway.retrieve_data_source(source_id)
    meta = source.get("properties") or {}
    title_name = _find(meta, "Цель", "Название", "Name", kind="title")
    if not title_name:
        raise notion_gateway.OperationalNotionError("В базе целей нет title-свойства.")
    values = {
        title_name: goal.title,
        "Период": goal.goal_type,
        "Тип": goal.goal_type,
        "Статус": goal.status,
        "Измеримый результат": goal.metric_name,
        "Текущее значение": goal.current_value,
        "Целевое значение": goal.target_value,
        "Процент выполнения": goal.progress_percent,
        "Препятствия": goal.obstacles,
        "Следующий шаг": goal.next_step,
        "Итог периода": goal.result_summary,
        "External ID": goal.external_id,
    }
    properties: dict[str, Any] = {}
    for name, value in values.items():
        if name not in meta:
            continue
        prop = _payload(meta[name], value)
        if prop is not None:
            properties[name] = prop
    period_name = _find(meta, "Период цели", "Даты", kind="date")
    if period_name:
        properties[period_name] = {
            "date": {
                "start": goal.period_start.isoformat(),
                "end": goal.period_end.isoformat(),
            }
        }
    if goal.notion_page_id:
        data = await notion_gateway._request(  # noqa: SLF001
            "PATCH",
            f"/pages/{notion_gateway._page_id(goal.notion_page_id)}",  # noqa: SLF001
            json={"properties": properties},
        )
    else:
        data = await notion_gateway._request(  # noqa: SLF001
            "POST",
            "/pages",
            json={
                "parent": {
                    "type": "data_source_id",
                    "data_source_id": notion_gateway._data_source_id(source_id),  # noqa: SLF001
                },
                "properties": properties,
            },
        )
    goal.notion_page_id = str(data.get("id") or goal.notion_page_id or "") or None
    goal.notion_url = data.get("url") or goal.notion_url
    await db.commit()
    return goal


async def _ensure_folder(name: str, parent_id: str) -> dict[str, Any]:
    existing = await google_drive_service.list_project_files(parent_id, limit=100)
    match = next(
        (
            item
            for item in existing
            if item.get("name") == name
            and item.get("mimeType") == "application/vnd.google-apps.folder"
        ),
        None,
    )
    if match:
        return match
    return await google_drive_service._create_folder(  # noqa: SLF001
        name=name, parent_id=parent_id
    )


async def upload_qa_attachment(
    db: Any,
    *,
    issue: Any,
    attachment: Any,
    content: bytes,
) -> Any:
    root_id = os.getenv("GOOGLE_DRIVE_QA_FOLDER_ID", "").strip()
    if not root_id:
        await goals_qa_service.mark_attachment_failed(
            db,
            attachment=attachment,
            error="GOOGLE_DRIVE_QA_FOLDER_ID не настроен.",
        )
        return attachment
    attachment.upload_status = "uploading"
    await db.commit()
    now = datetime.now(timezone.utc)
    try:
        year = await _ensure_folder(str(now.year), root_id)
        month = await _ensure_folder(f"{now.month:02d}", str(year["id"]))
        issue_folder = await _ensure_folder(
            issue.issue_code or f"QA-{issue.id}", str(month["id"])
        )
        uploaded = await google_drive_service.upload_file(
            parent_folder_id=str(issue_folder["id"]),
            filename=google_drive_service.sanitize_filename(attachment.original_name),
            content=content,
            mime_type=attachment.mime_type or "application/octet-stream",
        )
        await goals_qa_service.mark_attachment_uploaded(
            db,
            attachment=attachment,
            drive_file_id=str(uploaded["id"]),
            drive_url=str(uploaded.get("webViewLink") or ""),
        )
        return attachment
    except Exception as exc:
        await goals_qa_service.mark_attachment_failed(
            db, attachment=attachment, error=str(exc)
        )
        return attachment


def install_qa_projection_runtime() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True

    from app.agent import service as agent_service

    goals_qa_service.sync_issue_to_notion = sync_issue_to_notion
    original_execute = executor._execute
    original_file_upload = agent_service.handle_project_file_upload

    async def execute_with_projections(db: Any, action: Any) -> dict[str, Any]:
        if action.action_type == "create_business_goal":
            payload = dict(action.payload or {})
            if int(payload.get("telegram_user_id") or 0) != int(
                action.telegram_user_id
            ):
                raise PermissionError("Goal action owner mismatch.")
            goal = await goals_qa_service.create_goal(
                db,
                telegram_user_id=int(action.telegram_user_id),
                title=str(payload.get("title") or "Цель"),
                goal_type=str(payload.get("goal_type") or "month"),
                target_value=(
                    Decimal(str(payload["target_value"]))
                    if payload.get("target_value") is not None
                    else None
                ),
                metric_name=str(payload.get("metric_name") or "") or None,
            )
            notion_warning = None
            try:
                await sync_goal_to_notion(db, goal)
            except Exception as exc:
                notion_warning = goals_qa_service.redact_sensitive(str(exc))[:500]
            text = f"✅ Цель сохранена локально: {html.escape(goal.title)}"
            if goal.notion_url:
                text += f'\n<a href="{html.escape(goal.notion_url, quote=True)}">Открыть в Notion</a>'
            elif notion_warning:
                text += "\n⚠️ Notion временно недоступен; локальная цель не потеряна."
            return {
                "text": text,
                "data": {
                    "goal_id": int(goal.id),
                    "external_id": goal.external_id,
                    "notion_url": goal.notion_url,
                    "notion_warning": notion_warning,
                },
            }
        return await original_execute(db, action)

    async def handle_file_with_qa(
        db: Any,
        *,
        chat_id: int,
        telegram_user_id: int,
        telegram_message_id: int,
        filename: str,
        mime_type: str,
        content: bytes,
        caption: str | None,
        kind: str,
    ) -> AgentReply:
        session = await memory.get_or_create_session(
            db, telegram_user_id=int(telegram_user_id)
        )
        context = await memory.build_context(
            db, telegram_user_id=int(telegram_user_id), session=session
        )
        intake = context.get("qa_intake")
        issue_id = context.get("active_qa_issue_id") or context.get(
            "qa_retest_issue_id"
        )
        if not intake and not issue_id:
            return await original_file_upload(
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
        issue = None
        if issue_id:
            issue = await goals_qa_service.get_issue(
                db,
                telegram_user_id=int(telegram_user_id),
                issue_ref=int(issue_id),
            )
        if issue is None:
            issue, _ = await goals_qa_service.create_local_issue(
                db,
                telegram_user_id=int(telegram_user_id),
                text=(caption or "Вложение к новой проблеме; описание ожидается"),
                issue_type=(intake or {}).get("issue_type") if isinstance(intake, dict) else None,
                active_project_number=context.get("active_internal_lead_number"),
                kommo_lead_id=context.get("active_kommo_lead_id"),
                source="attachment",
                force_new=True,
            )
            issue.status = "Need details"
            await db.commit()
            await memory.update_context(
                db,
                session=session,
                values={"active_qa_issue_id": int(issue.id)},
            )
        checksum = hashlib.sha256(content).hexdigest()
        attachment = await goals_qa_service.add_attachment_record(
            db,
            issue=issue,
            original_name=filename,
            mime_type=mime_type,
            size_bytes=len(content),
            telegram_file_id=None,
            checksum=checksum,
            metadata={
                "telegram_message_id": int(telegram_message_id),
                "kind": kind,
                "caption": goals_qa_service.redact_sensitive(caption),
            },
        )
        await upload_qa_attachment(
            db, issue=issue, attachment=attachment, content=content
        )
        issue = await goals_qa_service.get_issue(
            db,
            telegram_user_id=int(telegram_user_id),
            issue_ref=int(issue.id),
        )
        if issue and issue.notion_page_id:
            try:
                await sync_issue_to_notion(db, issue)
            except Exception:
                pass
        if attachment.upload_status == "uploaded":
            status = "✅ Вложение сохранено в Drive"
        else:
            status = (
                "⚠️ Вложение записано локально, но не загружено в Drive. "
                "Оно не помечено как успешно сохранённое."
            )
        return AgentReply(
            status
            + "\n\n"
            + goals_qa_service.format_issue(issue),
            intent="qa_attachment_saved"
            if attachment.upload_status == "uploaded"
            else "qa_attachment_failed",
            metadata={
                "issue_id": int(issue.id),
                "attachment_id": int(attachment.id),
                "upload_status": attachment.upload_status,
            },
        )

    executor._execute = execute_with_projections
    agent_service.handle_project_file_upload = handle_file_with_qa
