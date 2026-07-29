"""Runtime fixes for the manual Google Sheets ↔ Kommo lead registry.

Policy:
- every new spreadsheet row with an empty Y receives its own row number;
- Kommo matching is a separate best-effort step and never blocks Y;
- exact phone/email contact lookup searches linked Kommo leads across pipelines;
- existing conflicting Kommo numbers are never overwritten automatically.
"""
from __future__ import annotations

import asyncio
import logging
import re
import unicodedata
from typing import Any

from app.services import (
    google_sheets_service,
    kommo_service,
    lead_status_sync_service,
    telegram_service,
)
from app.services.google_sheets_service import SpreadsheetRow
from app.services.unreviewed_leads_service import build_proposed_name

logger = logging.getLogger(__name__)
_INSTALLED = False


def _clean(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()


def _desired_number(row: SpreadsheetRow) -> str:
    return str(int(row.row_number))


def _fold(value: Any) -> str:
    text = unicodedata.normalize("NFKD", _clean(value).casefold())
    text = "".join(char for char in text if not unicodedata.combining(char))
    return " ".join(re.sub(r"[^\w]+", " ", text).split())


def _product_tokens(value: Any) -> set[str]:
    return {
        token
        for token in _fold(value).split()
        if len(token) >= 3 and token not in {"the", "and", "dla", "oraz"}
    }


def _lead_from_details(details: dict[str, Any]) -> dict[str, Any]:
    contacts = list(details.get("contacts") or [])
    contact = contacts[0] if contacts else {}
    return {
        "id": details.get("id"),
        "name": details.get("name"),
        "pipeline_id": details.get("pipeline_id"),
        "pipeline_name": details.get("pipeline_name"),
        "status_id": details.get("status_id"),
        "status_name": details.get("status_name"),
        "updated_at": details.get("updated_at"),
        "created_at": details.get("created_at"),
        "closed_at": details.get("closed_at"),
        "url": details.get("url"),
        "contact_name": contact.get("name"),
        "phones": list(contact.get("phones") or []),
        "emails": list(contact.get("emails") or []),
    }


async def _find_exact_contact_leads(row: SpreadsheetRow) -> list[dict[str, Any]]:
    """Find leads linked to a contact with an exact phone/email value."""
    queries: list[str] = []
    for value in (row.phone, row.email):
        clean = _clean(value)
        if clean and clean.casefold() not in {item.casefold() for item in queries}:
            queries.append(clean)

    lead_ids: set[int] = set()
    for query in queries:
        try:
            data = await kommo_service._request(
                "GET",
                "/api/v4/contacts",
                params={"query": query, "with": "leads", "limit": 50},
            )
        except Exception as exc:
            logger.warning(
                "Direct Kommo contact lookup failed for registry row %s: %s",
                row.row_number,
                exc,
            )
            continue
        contacts = ((data or {}).get("_embedded") or {}).get("contacts") or []
        for contact in contacts:
            if not kommo_service._contact_has_exact_value(
                contact, row.phone, row.email
            ):
                continue
            for ref in ((contact.get("_embedded") or {}).get("leads") or []):
                lead_id = ref.get("id")
                if isinstance(lead_id, int) and lead_id > 0:
                    lead_ids.add(lead_id)

    leads: list[dict[str, Any]] = []
    for lead_id in sorted(lead_ids):
        try:
            details = await kommo_service.get_lead_details(lead_id)
        except Exception as exc:
            logger.warning(
                "Could not load direct registry candidate %s for row %s: %s",
                lead_id,
                row.row_number,
                exc,
            )
            continue
        leads.append(_lead_from_details(details))
    return leads


def _select_unique_contact_lead(
    row: SpreadsheetRow, leads: list[dict[str, Any]]
) -> dict[str, Any] | None:
    if not leads:
        return None

    desired = _desired_number(row)
    exact_number = [
        lead
        for lead in leads
        if lead_status_sync_service.parse_internal_number(lead.get("name")) == desired
    ]
    if len(exact_number) == 1:
        return exact_number[0]

    open_leads = [lead for lead in leads if not lead.get("closed_at")]
    pool = open_leads if len(open_leads) == 1 else leads
    if len(pool) == 1:
        return pool[0]

    wanted = _product_tokens(row.product)
    scored: list[tuple[int, dict[str, Any]]] = []
    for lead in pool:
        score = len(wanted & _product_tokens(lead.get("name")))
        scored.append((score, lead))
    best_score = max((item[0] for item in scored), default=0)
    best = [lead for score, lead in scored if score == best_score and score > 0]
    return best[0] if len(best) == 1 else None


def _rewrite_action_for_row(
    row: SpreadsheetRow, action: dict[str, Any]
) -> dict[str, Any]:
    result = dict(action)
    desired = _desired_number(row)
    old_name = _clean(result.get("old_name"))
    current_number = lead_status_sync_service.parse_internal_number(old_name)
    product_ru = (
        _clean(result.get("short_product_ru"))
        or re.sub(r"^\s*\d+\s*(?:[-–—]\s*|\s+)", "", old_name)
        or "Новый запрос"
    )[:50]
    number_conflict = bool(current_number and current_number != desired)
    new_name = old_name if number_conflict else build_proposed_name(desired, product_ru)
    matched_by = str(result.get("matched_by") or "unknown")
    task_text = lead_status_sync_service._recommended_action(
        row, desired, product_ru
    )
    lead_stub = {
        "id": int(result.get("kommo_lead_id") or 0),
        "name": old_name,
    }
    analysis_note = lead_status_sync_service._analysis_note(
        row=row,
        lead=lead_stub,
        lead_number=desired,
        product_ru=product_ru,
        matched_by=matched_by,
        task_text=task_text,
    )
    if number_conflict:
        analysis_note += (
            "\n\nВНИМАНИЕ: в названии Kommo уже указан другой внутренний номер "
            f"№{current_number}. Название автоматически не изменено."
        )

    card = dict(result.get("contact_card") or {})
    card["lead_number"] = desired
    card.setdefault("name", _clean(row.client_name))
    card.setdefault("phone", _clean(row.phone))
    card.setdefault("email", _clean(row.email))
    card.setdefault("product", product_ru)

    result.update(
        {
            "row_number": row.row_number,
            "lead_number": desired,
            "new_name": new_name,
            "short_product_ru": product_ru,
            "task_text": task_text,
            "analysis_note": analysis_note[:13_500],
            "contact_card": card,
            "number_conflict": (
                {"kommo_number": current_number, "sheet_number": desired}
                if number_conflict
                else None
            ),
        }
    )
    return result


async def _action_from_direct_match(
    row: SpreadsheetRow, lead: dict[str, Any]
) -> dict[str, Any]:
    desired = _desired_number(row)
    product_ru = await lead_status_sync_service._safe_product_title(row, lead)
    old_name = _clean(lead.get("name"))
    action: dict[str, Any] = {
        "kommo_lead_id": int(lead["id"]),
        "row_number": row.row_number,
        "lead_number": desired,
        "old_name": old_name,
        "new_name": build_proposed_name(desired, product_ru),
        "short_product_ru": product_ru,
        "matched_by": "exact_contact_direct",
        "target_status_id": None,
        "task_text": "",
        "task_due_at": lead_status_sync_service._task_due_timestamp(),
        "analysis_note": "",
        "contact_card": {
            "name": _clean(row.client_name) or _clean(lead.get("contact_name")),
            "phone": _clean(row.phone)
            or (list(lead.get("phones") or [""])[0]),
            "email": _clean(row.email)
            or (list(lead.get("emails") or [""])[0]),
            "product": product_ru,
            "lead_number": desired,
            "kommo_lead_id": int(lead["id"]),
        },
        "url": lead.get("url"),
    }

    pipeline_id = lead.get("pipeline_id")
    if lead_status_sync_service._incoming_status(lead.get("status_name")):
        target_status_id = await lead_status_sync_service._first_contact_status_id(
            pipeline_id if isinstance(pipeline_id, int) else None
        )
        if target_status_id != lead.get("status_id"):
            action["target_status_id"] = target_status_id
    return _rewrite_action_for_row(row, action)


def _sheet_update(
    row: SpreadsheetRow, action: dict[str, Any] | None
) -> dict[str, Any]:
    old_comment = _clean(row.marketing_comment)
    return {
        "row_number": row.row_number,
        "row_fingerprint": lead_status_sync_service._row_fingerprint(row),
        "old_lead_number": "",
        "new_lead_number": _desired_number(row),
        "old_comment": old_comment,
        "new_comment": old_comment,
        "marketing_status": row.lead_status,
        "product": row.product,
        "kommo_lead_id": (
            int(action.get("kommo_lead_id") or 0) or None if action else None
        ),
        "matched_by": action.get("matched_by") if action else None,
    }


def _prospective_table_duplicates(
    rows: list[SpreadsheetRow], new_rows: list[SpreadsheetRow]
) -> list[dict[str, Any]]:
    new_row_numbers = {row.row_number for row in new_rows}
    by_number: dict[str, list[int]] = {}
    for row in rows:
        number = (
            _desired_number(row)
            if row.row_number in new_row_numbers
            else _clean(row.lead_number)
        )
        if number:
            by_number.setdefault(number, []).append(row.row_number)
    return [
        {"lead_number": number, "row_numbers": positions}
        for number, positions in by_number.items()
        if len(positions) > 1
    ]


async def _enhance_report(
    report: dict[str, Any],
    rows: list[SpreadsheetRow],
) -> dict[str, Any]:
    new_rows = [
        row for row in rows if _clean(row.product) and not _clean(row.lead_number)
    ]
    row_by_number = {row.row_number: row for row in new_rows}
    actions_by_row: dict[int, dict[str, Any]] = {
        int(action["row_number"]): dict(action)
        for action in report.get("onboarding_actions") or []
        if int(action.get("row_number") or 0) in row_by_number
    }
    used_leads = {
        int(action.get("kommo_lead_id") or 0)
        for action in actions_by_row.values()
        if int(action.get("kommo_lead_id") or 0) > 0
    }

    for row in new_rows:
        if row.row_number in actions_by_row:
            continue
        candidates = await _find_exact_contact_leads(row)
        lead = _select_unique_contact_lead(row, candidates)
        if not lead:
            continue
        lead_id = int(lead.get("id") or 0)
        if not lead_id or lead_id in used_leads:
            continue
        actions_by_row[row.row_number] = await _action_from_direct_match(row, lead)
        used_leads.add(lead_id)

    normalized_actions: list[dict[str, Any]] = []
    for row_number, action in sorted(actions_by_row.items()):
        row = row_by_number[row_number]
        normalized_actions.append(_rewrite_action_for_row(row, action))
    normalized_by_row = {
        int(action["row_number"]): action for action in normalized_actions
    }

    sheet_updates = [
        _sheet_update(row, normalized_by_row.get(row.row_number))
        for row in sorted(new_rows, key=lambda item: item.row_number)
    ]
    kommo_renames = [
        {
            "kommo_lead_id": int(action["kommo_lead_id"]),
            "row_number": int(action["row_number"]),
            "lead_number": str(action["lead_number"]),
            "old_name": action.get("old_name"),
            "new_name": action.get("new_name"),
            "short_product_ru": action.get("short_product_ru"),
            "url": action.get("url"),
        }
        for action in normalized_actions
        if not action.get("number_conflict")
        and _clean(action.get("old_name")) != _clean(action.get("new_name"))
    ]
    unmatched = [
        {
            "row_number": row.row_number,
            "lead_number": _desired_number(row),
            "product": row.product,
            "client_name": row.client_name,
            "reason": "y_will_be_filled_kommo_match_required",
        }
        for row in sorted(new_rows, key=lambda item: item.row_number)
        if row.row_number not in normalized_by_row
    ]

    report = dict(report)
    report.update(
        {
            "new_rows_count": len(new_rows),
            "matched_count": len(normalized_actions),
            "sheet_updates": sheet_updates,
            "number_assignments_count": len(sheet_updates),
            "kommo_renames": kommo_renames,
            "kommo_renames_count": len(kommo_renames),
            "onboarding_actions": normalized_actions,
            "unmatched_table_rows": unmatched,
            "table_duplicates": _prospective_table_duplicates(rows, new_rows),
            "row_number_policy": True,
            "manual_onboarding_only": True,
            "marketing_status_preserved": True,
        }
    )
    report["updates_count"] = len(sheet_updates)
    report["updates_digest"] = lead_status_sync_service._updates_digest(report)
    report["has_differences"] = bool(
        sheet_updates
        or normalized_actions
        or unmatched
        or report.get("table_duplicates")
        or report.get("kommo_duplicates")
    )
    return report


async def _send_registry_confirmation(
    chat_id: int, report: dict[str, Any]
) -> dict[str, Any]:
    count = int(report.get("updates_count") or 0)
    return await telegram_service.send_message(
        chat_id,
        (
            "⚠️ <b>ПОДТВЕРЖДЕНИЕ ОБРАБОТКИ НОВЫХ ЛИДОВ</b>\n\n"
            f"Будет заполнено номеров Y: <b>{count}</b>.\n\n"
            "Номер Y всегда равен номеру строки Google Sheets. "
            "Отсутствие или неоднозначность сделки Kommo больше не блокирует запись Y.\n\n"
            "Для надёжно найденных сделок бот также переименует Kommo, "
            "при необходимости переведёт на «Первый контакт», добавит анализ и задачу.\n\n"
            "Новые сделки не создаются. Колонки W и X не изменяются."
        ),
        reply_markup={
            "inline_keyboard": [
                [
                    {
                        "text": f"✅ Да, заполнить {count}",
                        "callback_data": "sync:confirm",
                    }
                ],
                [{"text": "❌ Отмена", "callback_data": "sync:cancel"}],
            ]
        },
    )


def install_lead_registry_runtime() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True

    original_build = lead_status_sync_service.build_status_sync_report

    async def build_status_sync_report_with_row_policy() -> dict[str, Any]:
        report = await original_build()
        rows = await asyncio.to_thread(
            google_sheets_service.get_rows, force_refresh=True
        )
        return await _enhance_report(report, rows)

    lead_status_sync_service.build_status_sync_report = (
        build_status_sync_report_with_row_policy
    )
    telegram_service.send_status_sync_confirmation = _send_registry_confirmation
