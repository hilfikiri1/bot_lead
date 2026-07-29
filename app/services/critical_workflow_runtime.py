"""Critical runtime fixes found during the production smoke test.

The module intentionally patches two existing workflows without changing their
business permissions:

* lead registry confirmation uses a deterministic digest and ignores the
  recalculated task due timestamp;
* project file upload is explicitly bound to the project selected by the
  manager instead of relying only on implicit conversation context.
"""
from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

from app.agent import memory
from app.agent import service as agent_service
from app.config import get_settings
from app.services import lead_status_sync_service, telegram_state_service

logger = logging.getLogger(__name__)
settings = get_settings()
_INSTALLED = False


def stable_registry_digest(report: dict[str, Any]) -> str:
    """Return a digest containing only manager-visible, deterministic changes.

    ``task_due_at`` is deliberately excluded. It is calculated from the current
    clock every time a preview is rebuilt, so including it makes an unchanged
    preview look stale a few seconds later.
    """

    stable = {
        "sheet_updates": [
            {
                "row_number": item.get("row_number"),
                "new_lead_number": item.get("new_lead_number"),
                "kommo_lead_id": item.get("kommo_lead_id"),
            }
            for item in report.get("sheet_updates") or []
        ],
        "onboarding_actions": [
            {
                "row_number": item.get("row_number"),
                "kommo_lead_id": item.get("kommo_lead_id"),
                "lead_number": item.get("lead_number"),
                "old_name": item.get("old_name"),
                "new_name": item.get("new_name"),
                "target_status_id": item.get("target_status_id"),
                "task_text": item.get("task_text"),
                "number_conflict": item.get("number_conflict"),
            }
            for item in report.get("onboarding_actions") or []
        ],
    }
    payload = json.dumps(stable, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20]


def _project_upload_lead_id(state: dict[str, Any] | None) -> int | None:
    if not state or state.get("mode") != "awaiting_project_file":
        return None
    try:
        lead_id = int(state.get("kommo_lead_id") or 0)
    except (TypeError, ValueError):
        return None
    return lead_id if lead_id > 0 else None


def install_critical_workflow_runtime() -> None:
    """Install production-safe wrappers exactly once."""

    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True

    # Both the preview and the confirmation rebuild call this function through
    # lead_status_sync_service, including the row-number runtime extension.
    lead_status_sync_service._updates_digest = stable_registry_digest

    original_callback = agent_service.handle_callback

    async def handle_callback_with_project_upload_state(
        db: Any,
        *,
        callback_data: str,
        telegram_user_id: int,
        chat_id: int | None = None,
    ) -> Any:
        reply = await original_callback(
            db,
            callback_data=callback_data,
            telegram_user_id=telegram_user_id,
            chat_id=chat_id,
        )
        if reply is None or reply.intent != "project_upload_prompt":
            return reply

        try:
            lead_id = int((reply.metadata or {}).get("lead_id") or 0)
        except (TypeError, ValueError):
            lead_id = 0
        if lead_id <= 0:
            return reply

        await telegram_state_service.set_state(
            telegram_user_id,
            {
                "mode": "awaiting_project_file",
                "chat_id": int(chat_id or 0),
                "kommo_lead_id": lead_id,
            },
            ttl_seconds=settings.telegram_state_ttl_minutes * 60,
        )
        reply.text = (
            reply.text.rstrip()
            + "\n\n<b>Выбранный проект зафиксирован.</b> "
            + "Отправь файл до 20 МБ. PDF/Excel отправляй через «Файл/Документ», "
            + "а изображение — как фото."
        )
        return reply

    agent_service.handle_callback = handle_callback_with_project_upload_state

    original_project_upload = agent_service.handle_project_file_upload

    async def handle_project_file_upload_with_bound_project(
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
    ) -> Any:
        state = await telegram_state_service.get_state(telegram_user_id)
        bound_lead_id = _project_upload_lead_id(state)
        if bound_lead_id:
            session = await memory.get_or_create_session(
                db, telegram_user_id=telegram_user_id
            )
            await memory.set_active_lead(
                db,
                session=session,
                kommo_lead_id=bound_lead_id,
            )

        reply = await original_project_upload(
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

        if bound_lead_id and reply.intent in {
            "save_file_to_drive_project",
            "file_upload_duplicate_pending",
            "file_upload_duplicate",
        }:
            await telegram_state_service.clear_state(telegram_user_id)
        return reply

    agent_service.handle_project_file_upload = (
        handle_project_file_upload_with_bound_project
    )

    logger.info("Critical registry confirmation and project upload fixes installed")
