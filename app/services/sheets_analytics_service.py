"""Google Sheets analytics sync and internal lead numbering (not a second CRM)."""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from typing import Any

from app.config import get_settings
from app.services.contact_resolver import normalize_phone, resolve_contact

settings = get_settings()


@dataclass
class SheetsSyncPreview:
    matched: list[dict[str, Any]] = field(default_factory=list)
    number_assignments: list[dict[str, Any]] = field(default_factory=list)
    conflicts: list[dict[str, Any]] = field(default_factory=list)
    unmatched: list[dict[str, Any]] = field(default_factory=list)
    skipped_formulas: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


_TITLE_PREFIX_RE = re.compile(r"^(\d{1,4})\s+")


def extract_title_number(name: str | None) -> str | None:
    match = _TITLE_PREFIX_RE.match(str(name or "").strip())
    return match.group(1) if match else None


def apply_number_to_title(name: str | None, number: str) -> str:
    current = str(name or "").strip()
    existing = extract_title_number(current)
    if existing == str(number):
        return current
    if existing and existing != str(number):
        # Conflict: do not double-prefix; caller should surface conflict.
        return current
    return f"{number} {current}".strip()


def next_internal_number(existing_numbers: list[str | int]) -> str:
    values: list[int] = []
    for item in existing_numbers:
        text = str(item or "").strip()
        if text.isdigit():
            values.append(int(text))
    nxt = max(values) + 1 if values else 1
    return str(nxt)


def build_sheets_sync_preview(
    *,
    leads: list[dict[str, Any]],
    sheet_rows: list[dict[str, Any]],
) -> SheetsSyncPreview:
    preview = SheetsSyncPreview()
    by_phone: dict[str, dict[str, Any]] = {}
    by_email: dict[str, dict[str, Any]] = {}
    by_kommo: dict[str, dict[str, Any]] = {}
    numbers: list[str] = []

    for row in sheet_rows:
        number = str(row.get("lead_number") or row.get("Y") or "").strip()
        if number:
            numbers.append(number)
        phone = normalize_phone(row.get("phone"))
        email = str(row.get("email") or "").strip().casefold()
        kommo_id = str(row.get("kommo_id") or row.get("kommo_lead_id") or "").strip()
        if phone:
            by_phone[phone] = row
        if email:
            by_email[email] = row
        if kommo_id:
            by_kommo[kommo_id] = row
        for key, value in row.items():
            if isinstance(value, str) and value.startswith("="):
                preview.skipped_formulas.append(f"row={row.get('row')} col={key}")

    for lead in leads:
        kommo_id = str(lead.get("id") or "")
        contact = resolve_contact(lead)
        row = by_kommo.get(kommo_id)
        match_via = "kommo_id" if row else None
        if not row and contact.phone_normalized:
            row = by_phone.get(contact.phone_normalized)
            match_via = "phone" if row else None
        if not row and contact.email:
            row = by_email.get(contact.email.casefold())
            match_via = "email" if row else None

        title_number = extract_title_number(lead.get("name"))
        sheet_number = str((row or {}).get("lead_number") or (row or {}).get("Y") or "").strip() or None

        if not row:
            preview.unmatched.append({"kommo_lead_id": lead.get("id"), "name": lead.get("name")})
            continue

        preview.matched.append(
            {
                "kommo_lead_id": lead.get("id"),
                "sheet_row": row.get("row"),
                "match_via": match_via,
                "sheet_number": sheet_number,
                "title_number": title_number,
            }
        )

        if title_number and sheet_number and title_number != sheet_number:
            preview.conflicts.append(
                {
                    "kommo_lead_id": lead.get("id"),
                    "title_number": title_number,
                    "sheet_number": sheet_number,
                }
            )
            continue

        if not sheet_number and not title_number:
            assigned = next_internal_number(numbers)
            numbers.append(assigned)
            new_title = apply_number_to_title(lead.get("name"), assigned)
            preview.number_assignments.append(
                {
                    "kommo_lead_id": lead.get("id"),
                    "sheet_row": row.get("row"),
                    "internal_number": assigned,
                    "new_title": new_title,
                    "column_y": assigned,
                }
            )
        elif sheet_number and not title_number:
            new_title = apply_number_to_title(lead.get("name"), sheet_number)
            preview.number_assignments.append(
                {
                    "kommo_lead_id": lead.get("id"),
                    "sheet_row": row.get("row"),
                    "internal_number": sheet_number,
                    "new_title": new_title,
                    "column_y": sheet_number,
                    "title_only": True,
                }
            )

    if not settings.google_sheets_write_enabled:
        preview.warnings.append("GOOGLE_SHEETS_WRITE_ENABLED=false — запись только после явного включения и подтверждения")
    return preview


def format_sheets_preview(preview: SheetsSyncPreview) -> str:
    lines = [
        "<b>📊 Sheets sync preview</b>",
        "",
        f"Сопоставлено: <b>{len(preview.matched)}</b>",
        f"Назначений номеров: <b>{len(preview.number_assignments)}</b>",
        f"Конфликтов: <b>{len(preview.conflicts)}</b>",
        f"Без пары: <b>{len(preview.unmatched)}</b>",
        "",
    ]
    for item in preview.number_assignments[:10]:
        lines.append(
            f"• Kommo {html.escape(str(item['kommo_lead_id']))}: "
            f"Y={html.escape(str(item['internal_number']))} → "
            f"{html.escape(str(item['new_title'])[:80])}"
        )
    if preview.conflicts:
        lines.extend(["", "<b>Конфликты номеров</b>"])
        for item in preview.conflicts[:8]:
            lines.append(
                f"• {html.escape(str(item['kommo_lead_id']))}: "
                f"title={html.escape(str(item['title_number']))} / "
                f"sheet={html.escape(str(item['sheet_number']))}"
            )
    if preview.skipped_formulas:
        lines.extend(["", f"Формулы не будут перезаписаны: {len(preview.skipped_formulas)}"])
    for warning in preview.warnings:
        lines.append(f"⚠️ {html.escape(warning)}")
    lines.extend(["", "Запись выполняется только после подтверждения."])
    return "\n".join(lines)
