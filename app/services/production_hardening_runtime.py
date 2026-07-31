"""Compatibility and production-safety patches installed after legacy runtimes.

The application still contains several import-time runtime decorators. This module is
kept deliberately small and installs last so older call sites remain compatible while
security-sensitive webhook behavior fails closed in production.
"""
from __future__ import annotations

import hmac
from typing import Any, Callable

from app.config import get_settings

_INSTALLED = False


def _closure_value(function: Callable[..., Any], name: str) -> Any | None:
    """Return a named closure value when a runtime wrapper captured it."""
    closure = function.__closure__ or ()
    for freevar, cell in zip(function.__code__.co_freevars, closure):
        if freevar == name:
            try:
                return cell.cell_contents
            except ValueError:
                return None
    return None


def install_production_hardening_runtime() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True

    from app.agent import service as agent_service
    from app.api import telegram as telegram_api
    from app.services import goals_qa_service

    settings = get_settings()

    # QA projection wraps the project upload function with a newer signature.
    # Preserve older internal/test call sites that legitimately omit the optional
    # Telegram metadata, while retaining QA routing for current Telegram uploads.
    qa_upload = agent_service.handle_project_file_upload
    base_upload = _closure_value(qa_upload, "original_file_upload")

    async def handle_project_file_upload_compat(
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
    ):
        legacy_call = caption is None and kind is None
        if legacy_call and callable(base_upload):
            return await base_upload(
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
        return await qa_upload(
            db,
            chat_id=chat_id,
            telegram_user_id=telegram_user_id,
            telegram_message_id=(
                int(telegram_message_id) if telegram_message_id is not None else 0
            ),
            filename=filename,
            mime_type=mime_type,
            content=content,
            caption=caption,
            kind=kind or "document",
        )

    agent_service.handle_project_file_upload = handle_project_file_upload_compat

    original_infer_priority = goals_qa_service.infer_priority

    def infer_priority_hardened(text: str) -> str:
        normalized = goals_qa_service.normalize(text)
        if any(
            token in normalized
            for token in (
                "не загружает",
                "не грузит",
                "не удается загрузить",
                "не удаётся загрузить",
            )
        ):
            return "High"
        return original_infer_priority(text)

    goals_qa_service.infer_priority = infer_priority_hardened

    def verify_telegram_secret_fail_closed(
        x_telegram_bot_api_secret_token: str | None,
    ) -> bool:
        expected = settings.telegram_webhook_secret.strip()
        if not expected:
            # Local development can use direct webhook fixtures without a secret,
            # but an internet-facing production endpoint must never trust unsigned
            # Telegram update JSON.
            return not settings.is_production
        return hmac.compare_digest(
            x_telegram_bot_api_secret_token or "", expected
        )

    telegram_api._verify_secret = verify_telegram_secret_fail_closed

    # Install after every legacy upload/message wrapper so /bug screenshot capture
    # and /bug_prompt cannot be bypassed by an older runtime layer.
    from app.services.bug_capture_runtime import install_bug_capture_runtime

    install_bug_capture_runtime()
