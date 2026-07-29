"""Reconcile Kommo stages with the status column in the lead spreadsheet."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from datetime import datetime, timezone
from typing import Any

from app.config import get_settings
from app.services import google_sheets_service, kommo_service

logger = logging.getLogger(__name__)
settings = get_settings()

INTERNAL_NUMBER_RE = re.compile(r"^\s*(\d+)\s*-\s*.+$")


def parse_internal_number(name: str | None) -> str | None:
    match = INTERNAL_NUMBER_RE.match(str(name or "").strip())
    return match.group(1) if match else None


def _normalized_status(value: Any) -> str:
    return " ".join(str(value or "").split()).casefold()


def _number_sort_key(value: str) -> tuple[int, str]:
    return (int(value), value) if value.isdigit() else (10**12, value)


def _updates_digest(updates: list[dict[str, Any]]) -> str:
    stable = [
        {
            "lead_number": item.get("lead_number"),
            "row_number": item.get("row_number"),
            "old_status": item.get("old_status") or "",
            "new_status": item.get("new_status") or "",
            "kommo_lead_id": item.get("kommo_lead_id"),
        }
        for item in updates
    ]
    payload = json.dumps(stable, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20]


async def build_status_sync_report() -> dict[str, Any]:
    """Read both systems and return a serializable, non-mutating comparison."""
    rows, kommo_result = await asyncio.gather(
        asyncio.to_thread(google_sheets_service.get_rows, force_refresh=True),
        kommo_service.get_all_leads_for_status_sync(),
    )
    kommo_leads = kommo_result.get("leads") or []

    table_by_number: dict[str, list[Any]] = {}
    for row in rows:
        number = str(row.lead_number or "").strip()
        if number:
            table_by_number.setdefault(number, []).append(row)

    kommo_by_number: dict[str, list[dict[str, Any]]] = {}
    unnumbered_kommo: list[dict[str, Any]] = []
    for lead in kommo_leads:
        number = parse_internal_number(lead.get("name"))
        if number:
            kommo_by_number.setdefault(number, []).append(lead)
        else:
            unnumbered_kommo.append(
                {
                    "kommo_lead_id": lead.get("id"),
                    "name": lead.get("name"),
                    "status_name": lead.get("status_name"),
                    "url": lead.get("url"),
                }
            )

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

    updates: list[dict[str, Any]] = []
    matching_count = 0
    table_only: list[dict[str, Any]] = []
    for number, matching_rows in table_by_number.items():
        if len(matching_rows) != 1:
            continue
        row = matching_rows[0]
        matching_leads = kommo_by_number.get(number) or []
        if not matching_leads:
            table_only.append(
                {
                    "lead_number": number,
                    "row_number": row.row_number,
                    "table_status": row.lead_status,
                    "product": row.product,
                }
            )
            continue
        if len(matching_leads) != 1:
            continue

        lead = matching_leads[0]
        old_status = row.lead_status or ""
        new_status = str(lead.get("status_name") or "").strip()
        if _normalized_status(old_status) == _normalized_status(new_status):
            matching_count += 1
            continue
        updates.append(
            {
                "lead_number": number,
                "row_number": row.row_number,
                "old_status": old_status,
                "new_status": new_status,
                "kommo_lead_id": lead.get("id"),
                "kommo_lead_name": lead.get("name"),
                "pipeline_name": lead.get("pipeline_name"),
                "kommo_updated_at": lead.get("updated_at"),
                "url": lead.get("url"),
            }
        )

    kommo_only: list[dict[str, Any]] = []
    for number, matching_leads in kommo_by_number.items():
        if number in table_by_number or len(matching_leads) != 1:
            continue
        lead = matching_leads[0]
        kommo_only.append(
            {
                "lead_number": number,
                "kommo_lead_id": lead.get("id"),
                "name": lead.get("name"),
                "status_name": lead.get("status_name"),
                "url": lead.get("url"),
            }
        )

    updates.sort(key=lambda item: _number_sort_key(item["lead_number"]))
    table_only.sort(key=lambda item: _number_sort_key(item["lead_number"]))
    kommo_only.sort(key=lambda item: _number_sort_key(item["lead_number"]))
    table_duplicates.sort(key=lambda item: _number_sort_key(item["lead_number"]))
    kommo_duplicates.sort(key=lambda item: _number_sort_key(item["lead_number"]))

    differences_count = (
        len(updates)
        + len(table_only)
        + len(kommo_only)
        + len(table_duplicates)
        + len(kommo_duplicates)
    )
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "spreadsheet_rows_count": len(rows),
        "kommo_leads_count": len(kommo_leads),
        "matching_count": matching_count,
        "updates": updates,
        "updates_count": len(updates),
        "updates_digest": _updates_digest(updates),
        "table_only": table_only,
        "kommo_only": kommo_only,
        "table_duplicates": table_duplicates,
        "kommo_duplicates": kommo_duplicates,
        "unnumbered_kommo": unnumbered_kommo,
        "unnumbered_kommo_count": len(unnumbered_kommo),
        "differences_count": differences_count,
        "has_differences": differences_count > 0,
        "kommo_truncated": bool(kommo_result.get("truncated")),
        "pipeline_id": kommo_result.get("pipeline_id"),
        "pipeline_name": kommo_result.get("pipeline_name"),
    }


async def apply_confirmed_report(
    *,
    expected_digest: str,
    expected_updates_count: int,
) -> dict[str, Any]:
    """Rebuild the preview and apply it only when neither source changed."""
    fresh_report = await build_status_sync_report()
    if (
        fresh_report.get("updates_digest") != expected_digest
        or int(fresh_report.get("updates_count") or 0) != expected_updates_count
    ):
        return {
            "stale": True,
            "report": fresh_report,
            "updated_count": 0,
            "updated": [],
            "skipped": [],
        }

    result = await asyncio.to_thread(
        google_sheets_service.apply_status_updates,
        list(fresh_report.get("updates") or []),
    )
    return {"stale": False, "report": fresh_report, **result}


async def periodic_status_sync_loop() -> None:
    """Send read-only discrepancy notifications on a configurable interval."""
    from app.services import telegram_service

    delay = max(10, int(settings.lead_status_sync_initial_delay_seconds or 90))
    interval = max(15, int(settings.lead_status_sync_interval_minutes or 180))
    await asyncio.sleep(delay)

    while True:
        chat_ids = settings.get_allowed_user_ids()
        if not chat_ids:
            logger.warning(
                "Status sync scheduler is enabled but ALLOWED_TELEGRAM_USER_IDS is empty"
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
            except Exception as exc:
                logger.exception("Periodic lead status sync failed")
                for chat_id in chat_ids:
                    try:
                        await telegram_service.send_message(
                            chat_id,
                            "❌ <b>Сверка статусов не выполнена</b>\n\n"
                            f"{type(exc).__name__}: проверьте Kommo и Google Sheets.",
                        )
                    except Exception:
                        logger.exception(
                            "Could not send status sync failure notification"
                        )

        await asyncio.sleep(interval * 60)
