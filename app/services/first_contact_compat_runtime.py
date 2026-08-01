"""Backward-compatible presentation wrappers for first-contact drafts.

Some internal tests and legacy call sites pass lightweight record objects without
``metadata_json``.  The real ORM model always has that field, but presentation
helpers should remain tolerant of older objects as well.
"""

from __future__ import annotations

import copy
from typing import Any, Callable

from app.services import client_message_service

FIRST_CONTACT_KIND = "first_contact_message"
_INSTALLED = False


def _closure_value(function: Callable[..., Any], name: str) -> Any | None:
    closure = function.__closure__ or ()
    for freevar, cell in zip(function.__code__.co_freevars, closure):
        if freevar != name:
            continue
        try:
            return cell.cell_contents
        except ValueError:
            return None
    return None


def _draft_kind(record: Any) -> str:
    metadata = getattr(record, "metadata_json", None) or {}
    if not isinstance(metadata, dict):
        return ""
    return str(metadata.get("draft_kind") or "")


def install_first_contact_compat_runtime() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True

    wrapped_format = client_message_service.format_client_message_draft
    base_format = _closure_value(wrapped_format, "original_format_draft")
    if callable(base_format):
        def format_client_message_draft_compat(record: Any) -> str:
            text = base_format(record)
            if _draft_kind(record) != FIRST_CONTACT_KIND:
                return text
            text = text.replace(
                "повторите follow-up",
                "повторите создание первого сообщения",
            )
            return "<b>👋 Первый контакт — черновик</b>\n\n" + text

        client_message_service.format_client_message_draft = (
            format_client_message_draft_compat
        )

    wrapped_markup = client_message_service.message_draft_markup
    base_markup = _closure_value(wrapped_markup, "original_draft_markup")
    if callable(base_markup):
        def message_draft_markup_compat(record: Any) -> dict[str, Any]:
            markup = copy.deepcopy(base_markup(record))
            if _draft_kind(record) != FIRST_CONTACT_KIND:
                return markup
            for row in markup.get("inline_keyboard") or []:
                for button in row:
                    if button.get("url") and "WhatsApp" in str(
                        button.get("text") or ""
                    ):
                        button["text"] = "👋 Открыть WhatsApp"
                        return markup
            return markup

        client_message_service.message_draft_markup = message_draft_markup_compat
