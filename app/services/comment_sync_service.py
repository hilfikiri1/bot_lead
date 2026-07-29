"""Confirmed synchronization of meaningful Kommo notes into Google Sheets column X.

The service is deliberately manual. It builds a stable preview, rechecks both
systems at confirmation time and only writes column X. Columns W and Y are never
changed.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from app.services import google_sheets_service, kommo_service, lead_status_sync_service
from app.services.google_sheets_service import SpreadsheetRow

_MAX_COMMENT_LENGTH = 700
_NOTE_CONCURRENCY = 8


def _clean(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()


def _normal(value: Any) -> str:
    return _clean(value).casefold()


def _row_fingerprint(row: SpreadsheetRow) -> list[str]:
    return [
        _normal(row.phone),
        _normal(row.email),
        _normal(row.client_name),
        _normal(row.product),
    ]


def _strip_technical_note(value: Any) -> str | None:
    """Return manager-relevant note text or None for technical audit notes."""
    raw = str(value or "").strip()
    if not raw:
        return None

    raw = re.sub(r"^\s*\[BBS-[^\]]+\]\s*", "", raw, flags=re.I)
    folded = raw.casefold()
    skip_markers = (
        "первичный анализ нового лида",
        "файл проекта загружен через b&bs telegram agent",
        "whatsapp-сообщение отправлено вручную после подтверждения",
        "входящее whatsapp cloud api сообщение",
        "исходящее whatsapp cloud api сообщение",
    )
    if any(marker in folded for marker in skip_markers):
        return None

    # Keep the actual manager note, not the technical wrapper produced by the agent.
    raw = re.sub(
        r"^\s*Примечание добавлено через B&BS AI Agent\.\s*"
        r"(?:Время:\s*[^\n.]+(?:UTC)?[.\s]*)?",
        "",
        raw,
        flags=re.I,
    ).strip()
    raw = re.sub(r"^\s*Источник:\s*[^\n]+\n", "", raw, flags=re.I).strip()
    text = _clean(raw)
    if not text or len(text) < 3:
        return None
    return text[:_MAX_COMMENT_LENGTH]


def _timestamp_prefix(value: Any) -> str:
    if value in (None, ""):
        return ""
    try:
        if isinstance(value, (int, float)) or str(value).isdigit():
            moment = datetime.fromtimestamp(int(value), tz=timezone.utc)
        else:
            moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return moment.astimezone().strftime("%d.%m")
    except Exception:
        return ""


def _latest_meaningful_comment(notes: list[dict[str, Any]]) -> str | None:
    ordered = sorted(
        notes,
        key=lambda item: int(item.get("created_at") or item.get("updated_at") or 0),
        reverse=True,
    )
    for note in ordered:
        text = _strip_technical_note(note.get("text"))
        if not text:
            continue
        prefix = _timestamp_prefix(note.get("created_at"))
        return f"{prefix}: {text}"[:_MAX_COMMENT_LENGTH] if prefix else text
    return None


def _digest(report: dict[str, Any]) -> str:
    stable = [
        {
            "row_number": item.get("row_number"),
            "lead_number": item.get("lead_number"),
            "kommo_lead_id": item.get("kommo_lead_id"),
            "old_comment": item.get("old_comment"),
            "new_comment": item.get("new_comment"),
        }
        for item in report.get("updates") or []
    ]
    payload = json.dumps(stable, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:18]


async def _comment_for_lead(
    semaphore: asyncio.Semaphore,
    lead: dict[str, Any],
) -> tuple[int, str | None, str | None]:
    lead_id = int(lead.get("id") or 0)
    if not lead_id:
        return 0, None, "missing_lead_id"
    async with semaphore:
        try:
            notes = await kommo_service.get_recent_common_notes(lead_id, limit=30)
        except Exception as exc:
            return lead_id, None, exc.__class__.__name__
    return lead_id, _latest_meaningful_comment(list(notes or [])), None


async def build_comment_sync_report(project_query: str | None = None) -> dict[str, Any]:
    rows, kommo_result = await asyncio.gather(
        asyncio.to_thread(google_sheets_service.get_rows, force_refresh=True),
        kommo_service.get_all_leads_for_status_sync(),
    )
    leads = list(kommo_result.get("leads") or [])
    by_number: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for lead in leads:
        number = lead_status_sync_service.parse_internal_number(lead.get("name"))
        if number:
            by_number[str(number)].append(lead)

    query = _clean(project_query)
    selected_rows = [row for row in rows if _clean(row.lead_number)]
    if query:
        digits = re.sub(r"\D", "", query)
        selected_rows = [
            row
            for row in selected_rows
            if _clean(row.lead_number) == query
            or (digits and _clean(row.lead_number) == digits)
            or str(row.row_number) == query
        ]

    unique_pairs: list[tuple[SpreadsheetRow, dict[str, Any]]] = []
    ambiguous: list[dict[str, Any]] = []
    missing_in_kommo: list[dict[str, Any]] = []
    for row in selected_rows:
        number = _clean(row.lead_number)
        candidates = by_number.get(number) or []
        if len(candidates) == 1:
            unique_pairs.append((row, candidates[0]))
        elif len(candidates) > 1:
            ambiguous.append(
                {
                    "row_number": row.row_number,
                    "lead_number": number,
                    "kommo_lead_ids": [item.get("id") for item in candidates],
                }
            )
        else:
            missing_in_kommo.append(
                {
                    "row_number": row.row_number,
                    "lead_number": number,
                    "product": row.product,
                }
            )

    semaphore = asyncio.Semaphore(_NOTE_CONCURRENCY)
    note_results = await asyncio.gather(
        *[_comment_for_lead(semaphore, lead) for _, lead in unique_pairs]
    )

    updates: list[dict[str, Any]] = []
    no_meaningful_note: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for (row, lead), (lead_id, new_comment, error) in zip(unique_pairs, note_results):
        if error:
            errors.append(
                {
                    "row_number": row.row_number,
                    "lead_number": row.lead_number,
                    "kommo_lead_id": lead_id,
                    "error": error,
                }
            )
            continue
        if not new_comment:
            no_meaningful_note.append(
                {
                    "row_number": row.row_number,
                    "lead_number": row.lead_number,
                    "kommo_lead_id": lead_id,
                }
            )
            continue
        old_comment = _clean(row.marketing_comment)
        if _normal(old_comment) == _normal(new_comment):
            continue
        updates.append(
            {
                "row_number": row.row_number,
                "row_fingerprint": _row_fingerprint(row),
                "old_lead_number": _clean(row.lead_number),
                "new_lead_number": _clean(row.lead_number),
                "old_comment": old_comment,
                "new_comment": new_comment,
                "lead_number": _clean(row.lead_number),
                "kommo_lead_id": lead_id,
                "lead_name": lead.get("name"),
                "product": row.product,
            }
        )

    updates.sort(key=lambda item: int(item.get("row_number") or 0))
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "project_query": query or None,
        "pipeline_name": kommo_result.get("pipeline_name"),
        "rows_scanned": len(selected_rows),
        "updates": updates,
        "updates_count": len(updates),
        "ambiguous": ambiguous,
        "missing_in_kommo": missing_in_kommo,
        "no_meaningful_note": no_meaningful_note,
        "errors": errors,
        "column": "X",
        "preserves_columns": ["W", "Y"],
    }
    report["digest"] = _digest(report)
    return report


async def apply_confirmed_report(
    *,
    expected_digest: str,
    expected_count: int,
    project_query: str | None = None,
) -> dict[str, Any]:
    fresh = await build_comment_sync_report(project_query)
    if fresh.get("digest") != expected_digest or int(fresh.get("updates_count") or 0) != int(
        expected_count
    ):
        return {"stale": True, "report": fresh, "updated_count": 0, "updated": [], "skipped": []}
    result = await asyncio.to_thread(
        google_sheets_service.apply_lead_registry_updates,
        list(fresh.get("updates") or []),
    )
    return {"stale": False, "report": fresh, **result}
