"""Final compatibility fixes for the composed production runtime stack.

The project intentionally composes several runtime extensions. This module is installed
last and keeps legacy callers compatible with the final QA file handler while preserving
the intended QA severity rules.
"""
from __future__ import annotations

import inspect
from typing import Any

_INSTALLED_HANDLER_ID: int | None = None


def _patch_qa_priority() -> None:
    from app.services import goals_qa_service

    current = goals_qa_service.infer_priority
    if getattr(current, "_bbs_final_compat", False):
        return

    def infer_priority_compatible(text: str) -> str:
        result = current(text)
        if result != "Medium":
            return result
        normalized = goals_qa_service.normalize(text)
        if any(
            token in normalized
            for token in (
                "ошибк",
                "не загружает",
                "не грузит",
                "не открывает",
                "не сохраняет",
                "не отправляет",
            )
        ):
            return "High"
        return result

    infer_priority_compatible._bbs_final_compat = True  # type: ignore[attr-defined]
    goals_qa_service.infer_priority = infer_priority_compatible


def _patch_project_file_handler() -> None:
    global _INSTALLED_HANDLER_ID

    from app.agent import service as agent_service

    current = agent_service.handle_project_file_upload
    if getattr(current, "_bbs_final_compat", False):
        _INSTALLED_HANDLER_ID = id(current)
        return
    if _INSTALLED_HANDLER_ID == id(current):
        return

    accepted = set(inspect.signature(current).parameters)

    async def handle_project_file_upload_compatible(
        db: Any,
        *,
        chat_id: int,
        telegram_user_id: int,
        filename: str,
        mime_type: str,
        content: bytes,
        telegram_message_id: int = 0,
        caption: str | None = None,
        kind: str = "document",
        **extra: Any,
    ):
        values: dict[str, Any] = {
            "chat_id": chat_id,
            "telegram_user_id": telegram_user_id,
            "telegram_message_id": telegram_message_id,
            "filename": filename,
            "mime_type": mime_type,
            "content": content,
            "caption": caption,
            "kind": kind,
            **extra,
        }
        kwargs = {name: value for name, value in values.items() if name in accepted}
        return await current(db, **kwargs)

    handle_project_file_upload_compatible._bbs_final_compat = True  # type: ignore[attr-defined]
    agent_service.handle_project_file_upload = handle_project_file_upload_compatible
    _INSTALLED_HANDLER_ID = id(handle_project_file_upload_compatible)


def install_final_compat_runtime() -> None:
    """Install after all other runtime extensions; safe to call repeatedly."""

    _patch_qa_priority()
    _patch_project_file_handler()
