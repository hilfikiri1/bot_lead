"""Compatibility fixes for QA behaviors introduced by the goals/QA rollout.

The QA projection wrapper must preserve the public file-upload call signature used by
existing project workflows.  Priority inference also uses Russian word stems so
normal inflections such as ``ошибку`` and ``не загружает`` remain High priority.
"""
from __future__ import annotations

import logging
from typing import Any

from app.services import goals_qa_service

logger = logging.getLogger(__name__)
_INSTALLED = False


def install_qa_regression_compat_runtime() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True

    from app.agent import service as agent_service

    current_file_upload = agent_service.handle_project_file_upload

    async def compatible_file_upload(
        db: Any,
        *,
        chat_id: int,
        telegram_user_id: int,
        filename: str,
        mime_type: str,
        content: bytes,
        telegram_message_id: int | None = None,
        caption: str | None = None,
        kind: str | None = None,
    ):
        return await current_file_upload(
            db,
            chat_id=chat_id,
            telegram_user_id=telegram_user_id,
            telegram_message_id=telegram_message_id,
            filename=filename,
            mime_type=mime_type,
            content=content,
            caption=caption,
            kind=kind or "document",
        )

    original_infer_priority = goals_qa_service.infer_priority

    def infer_priority_with_inflections(text: str) -> str:
        result = original_infer_priority(text)
        if result != "Medium":
            return result
        normalized = goals_qa_service.normalize(text)
        if any(
            token in normalized
            for token in (
                "ошиб",
                "не загруж",
                "не груз",
                "сбой",
                "упал",
                "падает",
            )
        ):
            return "High"
        return result

    agent_service.handle_project_file_upload = compatible_file_upload
    goals_qa_service.infer_priority = infer_priority_with_inflections
    logger.info("QA regression compatibility runtime installed")
