"""Normalize journal storage sources without changing pending-context provenance."""
from __future__ import annotations

from typing import Any

from app.services import kaizen_journal_service

_INSTALLED = False
_ALLOWED = {"text", "voice", "scheduled", "system"}


def storage_source(value: Any) -> str:
    source = str(value or "system").strip().casefold()
    return source if source in _ALLOWED else "system"


def install_kaizen_source_guard_runtime() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True

    original = kaizen_journal_service.get_or_create_entry

    async def get_or_create_entry_with_source_guard(*args, **kwargs):
        kwargs["source"] = storage_source(kwargs.get("source"))
        return await original(*args, **kwargs)

    kaizen_journal_service.get_or_create_entry = get_or_create_entry_with_source_guard
