"""Synchronize the marketing lead registry with Kommo identities and history.

Google Sheets column W is a marketing classification and is intentionally
independent from the operational Kommo pipeline. This service never compares
or writes W. Confirmed writes are limited to:

* X — concise marketing history/comment;
* Y — sequential internal lead number;
* Kommo lead name — ``<number> - <short product>``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from datetime import datetime, timezone
from typing import Any

from app.config import get_settings
from app.services import (
    google_sheets_service,
    kommo_service,
    product_title_service,
)
from app.services.google_sheets_service import SpreadsheetRow
from app.services.lead_matching_service import match_lead_to_rows
from app.services.unreviewed_leads_service import build_proposed_name

logger = logging.getLogger(__name__)
settings = get_settings()

INTERNAL_NUMBER_RE = re.compile(r"^\s*(\d+)\s*[-–—]\s*.+$")
_MANAGED_COMMENT_PREFIX = "Клиент:"
_COMMENT_LIMIT = 1200
_NOTE_LIMIT = 20
_NOTE_CONCURRENCY = 8


def parse_internal_number(name: str | None) -> str | None:
    match = INTERNAL_NUMBER_RE.match(str(name or "").strip())
    return match.group(1) if match else None


def _clean(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()


def _number_sort_key(value: str) -> tuple[int, str]:
    return (int(value), value) if value.isdigit() else (10**12, value)


def _row_fingerprint(row: SpreadsheetRow) -> list[str]:
    return [
        _clean(row.phone).casefold(),
        _clean(row.email).casefold(),
        _clean(row.client_name).casefold(),
        _clean(row.product).casefold(),
    ]


def _marketing_status_text(value: str | None) -> str:
    status = _clean(value)
    labels = {
        "sql": "SQL — целевой B2B-лид",
        "mql": "MQL — потенциально целевой, требуется квалификация",
        "нецелевой": "Нецелевой",
        "недозвон": "Недозвон",
        "игнор": "Игнор / нет ответа",
        "первый контакт": "Первый контакт",
        "сделка / продажа": "Сделка / продажа",
    }
    return labels.get(status.casefold(), status or "пока не определена")


def _manual_basis(comment: str | None) -> str:
    clean = _clean(comment)
    if not clean:
        return ""
    if not clean.startswith(_MANAGED_COMMENT_PREFIX):
        return clean[:600]
    match = re.search(
        r"(?:^|\. )Основание: (.*?)(?=\. (?:Kommo|История):|$)",
        clean,
    )
    return _clean(match.group(1))[:600] if match else ""


def _note_excerpt(note: dict[str, Any]) -> str:
    text = _clean(note.get("text"))
    if not text:
        return ""
    for marker in ("КРАТКОЕ РЕЗЮМЕ", "Краткое резюме:"):
        if marker in text:
            text = text.split(marker, 1)[1].strip(" :-")
            break
    text = re.sub(r"\s*(?:ПОЛНАЯ РАСШИФРОВКА|ЗАДАЧА|РИСКИ)\b.*$", "", text)
    return _clean(text)[:220]


def build_marketing_comment(
    row: SpreadsheetRow,
    lead: dict[str, Any],
    notes: list[dict[str, Any]],
) -> str:
    """Build a stable manager-readable snapshot for marketing reporting."""
    client_name = _clean(row.client_name) or "не указан"
    product = _clean(row.product) or _clean(lead.get("name")) or "не указан"
    parts = [
        f"Клиент: {client_name}",
        f"Запрос: {product}",
    ]

    context = []
    if _clean(row.budget):
        context.append(f"бюджет {_clean(row.budget)}")
    if _clean(row.contact_channel):
        context.append(f"канал {_clean(row.contact_channel)}")
    if _clean(row.region):
        context.append(f"регион {_clean(row.region)}")
    if context:
        parts.append("Параметры: " + ", ".join(context))

    parts.append(
        "Маркетинговая оценка: " + _marketing_status_text(row.lead_status)
    )
    basis = _manual_basis(row.marketing_comment)
    if basis:
        parts.append("Основание: " + basis)

    kommo_status = _clean(lead.get("status_name"))
    if kommo_status:
        parts.append("Kommo: " + kommo_status)

    excerpts: list[str] = []
    recent_notes = list(notes[:4])
    for note in reversed(recent_notes):
        excerpt = _note_excerpt(note)
        if excerpt and excerpt.casefold() not in {item.casefold() for item in excerpts}:
            excerpts.append(excerpt)
        if len(excerpts) >= 4:
            break
    if excerpts:
        parts.append("История: " + " → ".join(excerpts))

    comment = ". ".join(part.rstrip(" .") for part in parts if part) + "."
    return comment[:_COMMENT_LIMIT].rstrip()


def _updates_digest(report: dict[str, Any]) -> str:
    stable = {
        "sheet_updates": [
            {
                "row_number": item.get("row_number"),
                "old_lead_number": item.get("old_lead_number") or "",
                "new_lead_number": item.get("new_lead_number") or "",
                "old_comment": item.get("old_comment") or "",
                "new_comment": item.get("new_comment") or "",
                "kommo_lead_id": item.get("kommo_lead_id"),
            }
            for item in report.get("sheet_updates") or []
        ],
        "kommo_renames": [
            {
                "kommo_lead_id": item.get("kommo_lead_id"),
                "row_number": item.get("row_number"),
                "old_name": item.get("old_name") or "",
                "new_name": item.get("new_name") or "",
                "lead_number": item.get("lead_number") or "",
            }
            for item in report.get("kommo_renames") or []
        ],
    }
    payload = json.dumps(stable, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20]


async def _load_notes(pairs: list[dict[str, Any]]) -> None:
    semaphore = asyncio.Semaphore(_NOTE_CONCURRENCY)

    async def load(pair: dict[str, Any]) -> None:
        lead_id = int(pair["lead"]["id"])
        async with semaphore:
            try:
                pair["notes"] = await kommo_service.get_recent_common_notes(
                    lead_id, limit=_NOTE_LIMIT
                )
            except Exception as exc:
                logger.warning("Could not load notes for sync lead %s: %s", lead_id, exc)
                pair["notes"] = []

    await asyncio.gather(*(load(pair) for pair in pairs))


async def _match_unresolved_pairs(
    rows: list[SpreadsheetRow],
    leads: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not rows or not leads:
        return [], leads
    enriched = await kommo_service.enrich_leads_with_contacts(leads)
    candidates_by_row: dict[int, list[dict[str, Any]]] = {}
    for lead in enriched:
        match = match_lead_to_rows(
            phones=list(lead.get("phones") or []),
            emails=list(lead.get("emails") or []),
            contact_name=lead.get("contact_name"),
            company=lead.get("company"),
            product_hint=lead.get("name"),
            rows=rows,
            require_lead_number=False,
        )
        if match.single is None:
            continue
        candidate = match.single
        candidates_by_row.setdefault(candidate.row.row_number, []).append(
            {
                "row": candidate.row,
                "lead": lead,
                "matched_by": candidate.matched_by,
            }
        )

    pairs: list[dict[str, Any]] = []
    used_lead_ids: set[int] = set()
    for items in candidates_by_row.values():
        if len(items) != 1:
            continue
        pair = items[0]
        lead_id = int(pair["lead"]["id"])
        if lead_id in used_lead_ids:
            continue
        used_lead_ids.add(lead_id)
        pairs.append(pair)
    unmatched = [
        lead for lead in enriched if int(lead.get("id") or 0) not in used_lead_ids
    ]
    return pairs, unmatched


async def build_status_sync_report() -> dict[str, Any]:
    """Read both systems and prepare a non-mutating registry sync preview."""
    rows, kommo_result = await asyncio.gather(
        asyncio.to_thread(google_sheets_service.get_rows, force_refresh=True),
        kommo_service.get_all_leads_for_status_sync(),
    )
    kommo_leads = list(kommo_result.get("leads") or [])

    table_by_number: dict[str, list[SpreadsheetRow]] = {}
    for row in rows:
        number = _clean(row.lead_number)
        if number:
            table_by_number.setdefault(number, []).append(row)

    kommo_by_number: dict[str, list[dict[str, Any]]] = {}
    unnumbered_kommo: list[dict[str, Any]] = []
    for lead in kommo_leads:
        number = parse_internal_number(lead.get("name"))
        if number:
            kommo_by_number.setdefault(number, []).append(lead)
        else:
            unnumbered_kommo.append(lead)

    table_duplicates = [
        {
            "lead_number": number,
            "row_numbers": [row.row_number for row in duplicate_rows],
        }
        for number, duplicate_rows in table_by_number.items()
        if len(duplicate_rows) > 1
    ]
    kommo_duplicates = [
        {
            "lead_number": number,
            "kommo_lead_ids": [lead.get("id") for lead in duplicate_leads],
            "names": [lead.get("name") for lead in duplicate_leads],
        }
        for number, duplicate_leads in kommo_by_number.items()
        if len(duplicate_leads) > 1
    ]
    duplicate_numbers = {
        item["lead_number"] for item in table_duplicates + kommo_duplicates
    }

    pairs: list[dict[str, Any]] = []
    paired_rows: set[int] = set()
    paired_leads: set[int] = set()
    for number, matching_rows in table_by_number.items():
        matching_leads = kommo_by_number.get(number) or []
        if (
            number in duplicate_numbers
            or len(matching_rows) != 1
            or len(matching_leads) != 1
        ):
            continue
        row = matching_rows[0]
        lead = matching_leads[0]
        pairs.append({"row": row, "lead": lead, "matched_by": "number"})
        paired_rows.add(row.row_number)
        paired_leads.add(int(lead["id"]))

    unresolved_rows = [
        row
        for row in rows
        if row.row_number not in paired_rows
        and _clean(row.product)
        and _clean(row.lead_number) not in duplicate_numbers
    ]
    unresolved_leads = [
        lead
        for lead in unnumbered_kommo
        if int(lead.get("id") or 0) not in paired_leads
    ]
    contact_pairs, remaining_unnumbered = await _match_unresolved_pairs(
        unresolved_rows, unresolved_leads
    )
    for pair in contact_pairs:
        pairs.append(pair)
        paired_rows.add(pair["row"].row_number)
        paired_leads.add(int(pair["lead"]["id"]))

    used_numbers = {
        int(number)
        for number in list(table_by_number) + list(kommo_by_number)
        if str(number).isdigit()
    }
    next_number = max(used_numbers, default=0) + 1
    for pair in sorted(pairs, key=lambda item: item["row"].row_number):
        row = pair["row"]
        old_number = _clean(row.lead_number)
        if old_number:
            pair["lead_number"] = old_number
            pair["new_number"] = False
            continue
        while next_number in used_numbers:
            next_number += 1
        pair["lead_number"] = str(next_number)
        pair["new_number"] = True
        used_numbers.add(next_number)
        next_number += 1

    await _load_notes(pairs)

    sheet_updates: list[dict[str, Any]] = []
    kommo_renames: list[dict[str, Any]] = []
    for pair in pairs:
        row: SpreadsheetRow = pair["row"]
        lead = pair["lead"]
        lead_number = str(pair["lead_number"])
        old_number = _clean(row.lead_number)
        old_comment = _clean(row.marketing_comment)
        new_comment = build_marketing_comment(
            row, lead, list(pair.get("notes") or [])
        )
        if old_number != lead_number or old_comment != new_comment:
            sheet_updates.append(
                {
                    "row_number": row.row_number,
                    "row_fingerprint": _row_fingerprint(row),
                    "old_lead_number": old_number,
                    "new_lead_number": lead_number,
                    "old_comment": old_comment,
                    "new_comment": new_comment,
                    "marketing_status": row.lead_status,
                    "product": row.product,
                    "kommo_lead_id": lead.get("id"),
                    "matched_by": pair.get("matched_by"),
                }
            )

        if pair.get("matched_by") != "number":
            short_product = await product_title_service.short_product_title(row.product)
            proposed_name = build_proposed_name(lead_number, short_product)
            current_name = _clean(lead.get("name"))
            if current_name != proposed_name:
                kommo_renames.append(
                    {
                        "kommo_lead_id": int(lead["id"]),
                        "row_number": row.row_number,
                        "lead_number": lead_number,
                        "old_name": current_name,
                        "new_name": proposed_name,
                        "short_product_ru": short_product,
                        "url": lead.get("url"),
                    }
                )

    unmatched_table_rows = [
        {
            "row_number": row.row_number,
            "lead_number": row.lead_number,
            "product": row.product,
            "client_name": row.client_name,
        }
        for row in rows
        if row.row_number not in paired_rows
    ]
    kommo_only = [
        {
            "lead_number": number,
            "kommo_lead_id": lead.get("id"),
            "name": lead.get("name"),
            "status_name": lead.get("status_name"),
            "url": lead.get("url"),
        }
        for number, matching_leads in kommo_by_number.items()
        if number not in duplicate_numbers
        for lead in matching_leads
        if int(lead.get("id") or 0) not in paired_leads
    ]

    sheet_updates.sort(key=lambda item: item["row_number"])
    kommo_renames.sort(key=lambda item: item["row_number"])
    table_duplicates.sort(key=lambda item: _number_sort_key(item["lead_number"]))
    kommo_duplicates.sort(key=lambda item: _number_sort_key(item["lead_number"]))

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "spreadsheet_rows_count": len(rows),
        "kommo_leads_count": len(kommo_leads),
        "matched_count": len(pairs),
        "sheet_updates": sheet_updates,
        "comment_updates_count": sum(
            1
            for item in sheet_updates
            if _clean(item.get("old_comment")) != _clean(item.get("new_comment"))
        ),
        "number_assignments_count": sum(
            1
            for item in sheet_updates
            if _clean(item.get("old_lead_number"))
            != _clean(item.get("new_lead_number"))
        ),
        "kommo_renames": kommo_renames,
        "kommo_renames_count": len(kommo_renames),
        "unmatched_table_rows": unmatched_table_rows,
        "kommo_only": kommo_only,
        "unnumbered_kommo": [
            {
                "kommo_lead_id": lead.get("id"),
                "name": lead.get("name"),
                "status_name": lead.get("status_name"),
                "url": lead.get("url"),
            }
            for lead in remaining_unnumbered
        ],
        "table_duplicates": table_duplicates,
        "kommo_duplicates": kommo_duplicates,
        "kommo_truncated": bool(kommo_result.get("truncated")),
        "pipeline_id": kommo_result.get("pipeline_id"),
        "pipeline_name": kommo_result.get("pipeline_name"),
        "marketing_status_preserved": True,
    }
    report["updates_count"] = len(sheet_updates) + len(kommo_renames)
    report["updates_digest"] = _updates_digest(report)
    report["has_differences"] = bool(
        report["updates_count"]
        or unmatched_table_rows
        or kommo_only
        or remaining_unnumbered
        or table_duplicates
        or kommo_duplicates
    )
    return report


async def apply_confirmed_report(
    *,
    expected_digest: str,
    expected_updates_count: int,
) -> dict[str, Any]:
    """Rebuild the preview, write X/Y, then rename verified Kommo leads."""
    fresh_report = await build_status_sync_report()
    if (
        fresh_report.get("updates_digest") != expected_digest
        or int(fresh_report.get("updates_count") or 0) != expected_updates_count
    ):
        return {
            "stale": True,
            "report": fresh_report,
            "updated_count": 0,
            "renamed_count": 0,
            "skipped": [],
        }

    sheet_result = await asyncio.to_thread(
        google_sheets_service.apply_lead_registry_updates,
        list(fresh_report.get("sheet_updates") or []),
    )
    rows_after_write = await asyncio.to_thread(
        google_sheets_service.get_rows, force_refresh=True
    )
    rows_by_position = {row.row_number: row for row in rows_after_write}

    renamed: list[dict[str, Any]] = []
    rename_skipped: list[dict[str, Any]] = []
    for item in fresh_report.get("kommo_renames") or []:
        row = rows_by_position.get(int(item["row_number"]))
        if row is None or _clean(row.lead_number) != _clean(item["lead_number"]):
            rename_skipped.append({**item, "reason": "sheet_number_not_applied"})
            continue
        try:
            lead = await kommo_service.get_lead_details(int(item["kommo_lead_id"]))
            if _clean(lead.get("name")) != _clean(item.get("old_name")):
                rename_skipped.append({**item, "reason": "kommo_name_changed"})
                continue
            result = await kommo_service.update_kommo_lead(
                int(item["kommo_lead_id"]),
                name=str(item["new_name"]),
            )
            renamed.append({**item, **result})
        except Exception as exc:
            logger.warning(
                "Could not rename Kommo lead %s during registry sync: %s",
                item.get("kommo_lead_id"),
                exc,
            )
            rename_skipped.append(
                {**item, "reason": "kommo_update_failed", "error": type(exc).__name__}
            )

    return {
        "stale": False,
        "report": fresh_report,
        **sheet_result,
        "renamed_count": len(renamed),
        "renamed": renamed,
        "rename_skipped": rename_skipped,
    }


async def periodic_status_sync_loop() -> None:
    """Send read-only registry discrepancy notifications on a schedule."""
    from app.services import telegram_service

    delay = max(10, int(settings.lead_status_sync_initial_delay_seconds or 90))
    interval = max(15, int(settings.lead_status_sync_interval_minutes or 180))
    await asyncio.sleep(delay)

    while True:
        chat_ids = settings.get_allowed_user_ids()
        if not chat_ids:
            logger.warning(
                "Lead registry sync scheduler is enabled but "
                "ALLOWED_TELEGRAM_USER_IDS is empty"
            )
        else:
            try:
                report = await build_status_sync_report()
                should_notify = (
                    report.get("has_differences")
                    or not settings.lead_status_sync_notify_only_on_differences
                )
                if should_notify:
                    for chat_id in chat_ids:
                        await telegram_service.send_status_sync_notification(
                            chat_id, report
                        )
            except Exception:
                logger.exception("Periodic lead registry sync failed")
                for chat_id in chat_ids:
                    try:
                        await telegram_service.send_message(
                            chat_id,
                            "❌ <b>Синхронизация лидов не выполнена</b>\n\n"
                            "Проверьте Kommo и Google Sheets.",
                        )
                    except Exception:
                        logger.exception(
                            "Could not send lead registry sync failure notification"
                        )

        await asyncio.sleep(interval * 60)
