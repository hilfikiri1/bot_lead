"""Business logic for unreviewed Kommo leads and spreadsheet ID assignment."""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.services import crm_service, kommo_service, product_title_service
from app.services.google_sheets_service import SpreadsheetRow, get_row_by_number, get_rows
from app.services.kommo_service import KommoAPIError
from app.services.lead_matching_service import (
    MatchCandidate,
    MatchResult,
    lead_contact_snapshot,
    match_lead_to_rows,
    match_value_hash,
)

INTERNAL_LEAD_NAME_RE = re.compile(r"^\d+\s*-\s*.+$")
INTERNAL_NUMBER_RE = re.compile(r"^(\d+)\s*-\s*(.+)$")


def has_internal_lead_number(name: str | None) -> bool:
    return bool(INTERNAL_LEAD_NAME_RE.match((name or "").strip()))


def parse_internal_lead_name(name: str | None) -> tuple[str | None, str | None]:
    match = INTERNAL_NUMBER_RE.match((name or "").strip())
    if not match:
        return None, None
    return match.group(1), match.group(2).strip()


def build_proposed_name(lead_number: str, short_product_ru: str) -> str:
    number = str(lead_number).strip()
    product = str(short_product_ru).strip()
    if not number:
        raise ValueError("Внутренний номер лида пустой.")
    if not product:
        raise ValueError("Краткое название товара пустое.")
    return f"{number} - {product}"[:255]


def find_row_by_lead_number(
    rows: list[SpreadsheetRow], lead_number: str
) -> SpreadsheetRow | None:
    target = str(lead_number).strip()
    if not target:
        return None
    matches = [
        row
        for row in rows
        if row.lead_number and str(row.lead_number).strip() == target
    ]
    if len(matches) == 1:
        return matches[0]
    return None


async def match_lead_from_sheets(
    details: dict[str, Any],
    *,
    force_refresh: bool = False,
) -> MatchResult:
    snapshot = lead_contact_snapshot(details)
    rows = get_rows(force_refresh=force_refresh)
    return match_lead_to_rows(
        phones=snapshot.get("phones"),
        emails=snapshot.get("emails"),
        contact_name=snapshot.get("contact_name"),
        company=snapshot.get("company"),
        product_hint=snapshot.get("product_hint"),
        rows=rows,
    )


async def build_preview_from_row(
    row: SpreadsheetRow,
    *,
    candidate: MatchCandidate | None = None,
) -> dict[str, Any]:
    if not row.lead_number or not str(row.lead_number).strip():
        raise ValueError("В таблице пустая колонка с внутренним номером.")
    if not row.product or not str(row.product).strip():
        raise ValueError("В таблице пустая колонка с товаром.")
    short_product = await product_title_service.short_product_title(row.product)
    proposed_name = build_proposed_name(str(row.lead_number).strip(), short_product)
    return {
        "spreadsheet_row_number": row.row_number,
        "spreadsheet_lead_number": str(row.lead_number).strip(),
        "original_product": row.product,
        "short_product_ru": short_product,
        "proposed_name": proposed_name,
        "matched_by": candidate.matched_by if candidate else "manual",
        "matched_value_hash": match_value_hash(candidate)
        if candidate
        else None,
    }


async def apply_lead_rename(
    db: AsyncSession,
    *,
    lead_id: int,
    current_name: str,
    preview: dict[str, Any],
    telegram_user_id: int,
    allow_replace: bool = False,
) -> dict[str, Any]:
    proposed_name = str(preview.get("proposed_name") or "").strip()
    if not proposed_name:
        raise ValueError("Новое название не задано.")

    if current_name.strip() == proposed_name:
        return {
            "lead_id": lead_id,
            "lead_name": proposed_name,
            "internal_number": preview.get("spreadsheet_lead_number"),
            "short_product_ru": preview.get("short_product_ru"),
            "skipped": True,
        }

    existing_number, _ = parse_internal_lead_name(current_name)
    new_number = str(preview.get("spreadsheet_lead_number") or "").strip()
    if existing_number and existing_number != new_number and not allow_replace:
        raise ValueError("replace_required")

    result = await kommo_service.update_kommo_lead(lead_id, name=proposed_name)
    await crm_service.save_spreadsheet_lead_mapping(
        db,
        kommo_lead_id=lead_id,
        spreadsheet_lead_number=new_number,
        original_product=str(preview.get("original_product") or ""),
        short_product_ru=str(preview.get("short_product_ru") or ""),
        old_kommo_name=current_name,
        new_kommo_name=proposed_name,
        spreadsheet_row_number=int(preview.get("spreadsheet_row_number") or 0) or None,
        matched_by=str(preview.get("matched_by") or ""),
        matched_value_hash=preview.get("matched_value_hash"),
        created_by_telegram_user_id=telegram_user_id,
    )
    return {
        **result,
        "internal_number": new_number,
        "short_product_ru": preview.get("short_product_ru"),
        "skipped": False,
    }


async def build_preview_from_manual_number(
    lead_number: str,
    *,
    force_refresh: bool = False,
) -> dict[str, Any]:
    rows = get_rows(force_refresh=force_refresh)
    row = find_row_by_lead_number(rows, lead_number)
    if not row:
        raise ValueError("Строка с таким номером не найдена в таблице.")
    return await build_preview_from_row(row)
