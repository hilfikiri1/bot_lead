"""Polish local-number compatibility and final operator workflow patches."""
from __future__ import annotations

import re
from typing import Any

from app.services import operator_experience_runtime
from app.services.contact_bundle_runtime import install_contact_bundle_runtime
from app.services.critical_workflow_runtime import install_critical_workflow_runtime
from app.services.status_sync_all_runtime import install_status_sync_all_runtime
from app.services.status_sync_create_runtime import install_status_sync_create_runtime

_INSTALLED = False


def _digits(value: Any) -> str:
    return re.sub(r"\D", "", str(value or ""))


def phones_equivalent(left: Any, right: Any) -> bool:
    a = _digits(left)
    b = _digits(right)
    if not a or not b:
        return False
    if a == b:
        return True
    # Polish forms commonly store the same number as 9 local digits or +48 + 9 digits.
    if len(a) == 9 and len(b) == 11 and b.startswith("48"):
        return b[2:] == a
    if len(b) == 9 and len(a) == 11 and a.startswith("48"):
        return a[2:] == b
    return False


def _all_phone_values(details: dict[str, Any]) -> list[str]:
    values: list[str] = []
    for contact in details.get("contacts") or []:
        values.extend(str(value) for value in contact.get("phones") or [])
        for marker, value in operator_experience_runtime._custom_values(contact):
            marker = marker.casefold()
            if any(token in marker for token in ("phone", "telefon", "numer", "телефон", "номер")):
                values.append(value)
    for marker, value in operator_experience_runtime._custom_values(details):
        marker = marker.casefold()
        if any(token in marker for token in ("phone", "telefon", "numer", "телефон", "номер")):
            values.append(value)
    return values


def install_operator_experience_phone_patch() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True
    original = operator_experience_runtime._lead_exactly_matches_row

    def lead_exactly_matches_row_with_local_phone(details: dict[str, Any], row: Any) -> bool:
        if original(details, row):
            return True
        wanted = getattr(row, "phone", None)
        return any(phones_equivalent(wanted, value) for value in _all_phone_values(details))

    operator_experience_runtime._lead_exactly_matches_row = (
        lead_exactly_matches_row_with_local_phone
    )
    install_critical_workflow_runtime()
    install_contact_bundle_runtime()
    install_status_sync_all_runtime()
    install_status_sync_create_runtime()
