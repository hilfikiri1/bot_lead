"""Expose kaizen persistence requirements to the existing read-only /diag flow."""
from __future__ import annotations

from app.services import system_diagnostics

_INSTALLED = False


def install_kaizen_diagnostics_runtime() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True
    required = list(system_diagnostics._REQUIRED_TABLES)
    if "kaizen_journal_entries" not in required:
        required.append("kaizen_journal_entries")
    system_diagnostics._REQUIRED_TABLES = tuple(required)
