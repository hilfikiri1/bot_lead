"""Normalize journal sources and redact credentials before local persistence."""
from __future__ import annotations

import re
from typing import Any

from app.agent.security import sanitize_text
from app.services import kaizen_journal_service

_INSTALLED = False
_ALLOWED = {"text", "voice", "scheduled", "system"}
_SECRET_MARKER = "[СКРЫТО: СЕКРЕТ]"

_PEM_RE = re.compile(
    r"-----BEGIN\s+[^-]*PRIVATE KEY-----.*?-----END\s+[^-]*PRIVATE KEY-----",
    flags=re.I | re.S,
)
_JSON_SECRET_RE = re.compile(
    r'(?i)("(?:private_key|client_secret|refresh_token|access_token|id_token|password|api[_-]?key|authorization|cookie|credentials?)"\s*:\s*)"(?:\\.|[^"\\])*"'
)
_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(private[_-]?key|client[_-]?secret|refresh[_-]?token|access[_-]?token|id[_-]?token|credentials?)\b\s*[:=]\s*[^\s,;]+"
)
_KNOWN_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:"
    r"sk-[A-Za-z0-9_-]{20,}|"
    r"github_pat_[A-Za-z0-9_]{20,}|"
    r"gh[pousr]_[A-Za-z0-9]{20,}|"
    r"ya29\.[A-Za-z0-9._-]{20,}|"
    r"EAA[A-Za-z0-9]{40,}|"
    r"\d{6,12}:[A-Za-z0-9_-]{30,}|"
    r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"
    r")(?![A-Za-z0-9])"
)


def storage_source(value: Any) -> str:
    source = str(value or "system").strip().casefold()
    return source if source in _ALLOWED else "system"


def redact_journal_text(value: Any) -> str:
    """Remove credential material while preserving ordinary business facts."""
    text = str(value or "")
    text = _PEM_RE.sub(_SECRET_MARKER, text)
    text = _JSON_SECRET_RE.sub(
        lambda match: f'{match.group(1)}"{_SECRET_MARKER}"', text
    )
    text = _ASSIGNMENT_RE.sub(
        lambda match: f"{match.group(1)}={_SECRET_MARKER}", text
    )
    text = _KNOWN_TOKEN_RE.sub(_SECRET_MARKER, text)
    # Reuse the common Bearer/token/password sanitizer as the final pass.
    return str(sanitize_text(text, limit=50_000) or "")


def install_kaizen_source_guard_runtime() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True

    original_get_or_create = kaizen_journal_service.get_or_create_entry
    original_save = kaizen_journal_service.save_daily_reflection

    async def get_or_create_entry_with_source_guard(*args, **kwargs):
        kwargs["source"] = storage_source(kwargs.get("source"))
        return await original_get_or_create(*args, **kwargs)

    async def save_daily_reflection_with_privacy_guard(*args, **kwargs):
        kwargs["source"] = storage_source(kwargs.get("source"))
        kwargs["text"] = redact_journal_text(kwargs.get("text"))
        return await original_save(*args, **kwargs)

    kaizen_journal_service.get_or_create_entry = get_or_create_entry_with_source_guard
    kaizen_journal_service.save_daily_reflection = save_daily_reflection_with_privacy_guard
