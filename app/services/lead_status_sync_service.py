"""Manual onboarding of new Google Sheets leads into existing Kommo deals.

The spreadsheet and Kommo receive the same advertising lead independently.
This service never creates Kommo deals. It only processes spreadsheet rows where
column Y is empty, finds one reliable existing Kommo deal, assigns the next
internal number, renames the deal and prepares the first operational actions.

Confirmed writes are limited to:
* Google Sheets column Y — sequential internal lead number;
* Kommo lead name — ``<number> - <short product in Russian>``;
* optionally moving an incoming/unreviewed deal to ``Первый контакт``;
* one initial analysis note and one qualification task.

Column W and the manager's comment in X are never changed here.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from app.config import get_settings
from app.services import google_sheets_service, kommo_service
from app.services.google_sheets_service import SpreadsheetRow
from app.services.lead_matching_service import match_lead_to_rows
from app.services.onboarding_briefing_service import (
    OnboardingBriefing,
    build_heuristic_briefing,
    build_onboarding_briefing,
)
from app.services.unreviewed_leads_service import build_proposed_name

logger = logging.getLogger(__name__)
settings = get_settings()

# Accept both ``118 - Карнизы`` and the historical ``118 Карнизы`` format.
INTERNAL_NUMBER_RE = re.compile(r"^\s*(\d+)\s*(?:[-–—]\s*|\s+).+$")
_GENERIC_TITLES = {"товар", "новый товар"}
_NOTE_CONCURRENCY = 8
_BRIEFING_CONCURRENCY = 5


def parse_internal_number(name: str | None) -> str | None:
    match = INTERNAL_NUMBER_RE.match(str(name or "").strip())
    return match.group(1) if match else None


def _clean(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()


def _row_fingerprint(row: SpreadsheetRow) -> list[str]:
    return [
        _clean(row.phone).casefold(),
        _clean(row.email).casefold(),
        _clean(row.client_name).casefold(),
        _clean(row.product).casefold(),
    ]


def _number_sort_key(value: str) -> tuple[int, str]:
    return (int(value), value) if value.isdigit() else (10**12, value)


def _updates_digest(report: dict[str, Any]) -> str:
    stable = {
        "sheet_updates": [
            {
                "row_number": item.get("row_number"),
                "new_lead_number": item.get("new_lead_number"),
                "kommo_lead_id": item.get("kommo_lead_id"),
            }
            for item in report.get("sheet_updates") or []
        ],
        "onboarding_actions": [
            {
                "row_number": item.get("row_number"),
                "kommo_lead_id": item.get("kommo_lead_id"),
                "lead_number": item.get("lead_number"),
                "old_name": item.get("old_name"),
                "new_name": item.get("new_name"),
                "target_status_id": item.get("target_status_id"),
                "task_text": item.get("task_text"),
                "task_due_at": item.get("task_due_at"),
            }
            for item in report.get("onboarding_actions") or []
        ],
    }
    payload = json.dumps(stable, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20]


def _incoming_status(name: str | None) -> bool:
    lowered = _clean(name).casefold()
    return any(
        token in lowered
        for token in (
            "incoming",
            "неразобран",
            "входящ",
            "новый лид",
            "new lead",
        )
    )


async def _first_contact_status_id(pipeline_id: int | None) -> int | None:
    if not isinstance(pipeline_id, int):
        return settings.kommo_default_status_id
    try:
        statuses = await kommo_service.get_pipeline_statuses(pipeline_id)
    except Exception as exc:
        logger.warning("Could not load Kommo stages for onboarding: %s", exc)
        return settings.kommo_default_status_id
    preferred = (
        "первый контакт",
        "first contact",
        "pierwszy kontakt",
    )
    for wanted in preferred:
        for status in statuses:
            if _clean(status.get("name")).casefold() == wanted:
                return int(status["id"])
    return settings.kommo_default_status_id


def _task_due_timestamp() -> int:
    try:
        tz = ZoneInfo(settings.manager_timezone or "Europe/Warsaw")
    except Exception:
        tz = ZoneInfo("Europe/Warsaw")
    now = datetime.now(tz)
    due = now + timedelta(hours=4)
    if due.hour >= 18:
        due = (due + timedelta(days=1)).replace(
            hour=10, minute=0, second=0, microsecond=0
        )
    while due.weekday() >= 5:
        due = (due + timedelta(days=1)).replace(
            hour=10, minute=0, second=0, microsecond=0
        )
    return int(due.timestamp())


def _recommended_action(row: SpreadsheetRow, lead_number: str, product: str) -> str:
    client = _clean(row.client_name) or "клиенту"
    if _clean(row.phone):
        return f"Позвонить {client} по лиду №{lead_number}: {product}"
    if _clean(row.email):
        return f"Связаться с {client} по лиду №{lead_number}: {product}"
    return f"Открыть переписку и квалифицировать лид №{lead_number}: {product}"


def _initial_assessment(row: SpreadsheetRow) -> str:
    explicit = _clean(row.lead_status)
    if explicit:
        return explicit
    budget = _clean(row.budget).casefold()
    if "20_000" in budget or "20000" in budget or "powyżej" in budget:
        return "Высокий потенциальный бюджет; требуется квалификация"
    return "Новый лид; требуется квалификация"


def _analysis_note(
    *,
    row: SpreadsheetRow,
    lead: dict[str, Any],
    lead_number: str,
    product_ru: str,
    matched_by: str,
    task_text: str,
    briefing: OnboardingBriefing | None = None,
) -> str:
    marker = f"[BBS-ONBOARD-{lead_number}-{int(lead['id'])}]"
    brief = briefing or build_heuristic_briefing(
        product=row.product,
        product_ru=product_ru,
        budget=row.budget,
        channel=row.contact_channel,
        region=row.region,
        client_name=row.client_name,
    )
    lines = [
        marker,
        "ПЕРВИЧНЫЙ АНАЛИЗ НОВОГО ЛИДА",
        "",
        f"Внутренний номер: №{lead_number}",
        f"Клиент: {_clean(row.client_name) or 'не указан'}",
        f"Запрос из рекламы: {_clean(row.product) or 'не указан'}",
        f"Краткое название: {product_ru}",
        f"Телефон: {_clean(row.phone) or 'не указан'}",
        f"Email: {_clean(row.email) or 'не указан'}",
        f"Регион: {_clean(row.region) or 'не указан'}",
        f"Бюджет: {_clean(row.budget) or 'не указан'}",
        f"Канал: {_clean(row.contact_channel) or 'не указан'}",
        f"Сопоставление Sheets ↔ Kommo: {matched_by}",
        "",
        "О ЧЁМ ЗАЯВКА",
        "",
        brief.about_ru,
        "",
        "ЦЕЛЬ ПЕРВОГО КОНТАКТА",
        "",
        brief.call_goal_ru or task_text,
        "",
        "О ЧЁМ ГОВОРИТЬ",
        "",
    ]
    for point in brief.talk_points_ru:
        lines.append(f"– {point}")
    lines.extend(
        [
            "",
            f"Предварительная оценка: {_initial_assessment(row)}.",
            f"Следующий шаг: {task_text}.",
            "",
            "Статус маркетинговой оценки W не изменён.",
        ]
    )
    return "\n".join(lines)[:13_500]


def _newest_new_rows(rows: list[SpreadsheetRow]) -> list[SpreadsheetRow]:
    """Return only the newest empty-Y product rows for this manual run."""
    new_rows = [
        row
        for row in rows
        if _clean(row.product) and not _clean(row.lead_number)
    ]
    new_rows.sort(key=lambda item: item.row_number, reverse=True)
    limit = max(1, int(settings.lead_status_sync_max_new_rows or 5))
    return new_rows[:limit]


def _title_from_briefing(
    briefing: OnboardingBriefing, lead: dict[str, Any]
) -> str:
    title = _clean(briefing.short_product_ru)
    if title and title.casefold() not in _GENERIC_TITLES:
        return title[:50]

    # Never replace a meaningful Kommo title with the destructive fallback "Товар".
    current_name = _clean(lead.get("name"))
    current_name = re.sub(r"^\s*\d+\s*(?:[-–—]\s*|\s+)", "", current_name)
    if current_name and current_name.casefold() not in {
        "просьба о контакте",
        "lead",
        "new lead",
    }:
        return current_name[:50]
    return "Новый запрос"


async def _safe_briefing(row: SpreadsheetRow) -> OnboardingBriefing:
    try:
        return await build_onboarding_briefing(
            product=row.product,
            budget=row.budget,
            channel=row.contact_channel,
            region=row.region,
            client_name=row.client_name,
            lead_status=row.lead_status,
        )
    except Exception as exc:
        logger.warning(
            "Onboarding briefing failed for row %s (%s): %s",
            row.row_number,
            (row.product or "")[:80],
            exc,
        )
        return build_heuristic_briefing(
            product=row.product,
            budget=row.budget,
            channel=row.contact_channel,
            region=row.region,
            client_name=row.client_name,
        )


async def _safe_product_title(row: SpreadsheetRow, lead: dict[str, Any]) -> str:
    briefing = await _safe_briefing(row)
    return _title_from_briefing(briefing, lead)


async def _match_new_rows(
    rows: list[SpreadsheetRow],
    leads: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[SpreadsheetRow]]:
    if not rows or not leads:
        return [], rows
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
    paired_rows: set[int] = set()
    used_leads: set[int] = set()
    for row_number, candidates in candidates_by_row.items():
        if len(candidates) != 1:
            continue
        pair = candidates[0]
        lead_id = int(pair["lead"].get("id") or 0)
        if not lead_id or lead_id in used_leads:
            continue
        used_leads.add(lead_id)
        paired_rows.add(row_number)
        pairs.append(pair)
    return pairs, [row for row in rows if row.row_number not in paired_rows]


async def build_status_sync_report() -> dict[str, Any]:
    """Build a dry-run preview for manually onboarding rows with empty Y."""
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
    unnumbered_leads: list[dict[str, Any]] = []
    for lead in kommo_leads:
        number = parse_internal_number(lead.get("name"))
        if number:
            kommo_by_number.setdefault(number, []).append(lead)
        else:
            unnumbered_leads.append(lead)

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

    new_rows = _newest_new_rows(rows)
    pairs, unmatched_new_rows = await _match_new_rows(new_rows, unnumbered_leads)

    used_numbers = {
        int(number)
        for number in list(table_by_number) + list(kommo_by_number)
        if str(number).isdigit()
    }
    next_number = max(used_numbers, default=0) + 1
    # Newest spreadsheet rows first so fresh Facebook leads get numbers/analysis
    # before older empty-Y backlog rows.
    pairs.sort(key=lambda item: item["row"].row_number, reverse=True)
    for pair in pairs:
        while next_number in used_numbers:
            next_number += 1
        pair["lead_number"] = str(next_number)
        used_numbers.add(next_number)
        next_number += 1

    status_cache: dict[int, int | None] = {}
    sheet_updates: list[dict[str, Any]] = []
    kommo_renames: list[dict[str, Any]] = []
    onboarding_actions: list[dict[str, Any]] = []

    semaphore = asyncio.Semaphore(_BRIEFING_CONCURRENCY)

    async def _brief_pair(pair: dict[str, Any]) -> OnboardingBriefing:
        async with semaphore:
            return await _safe_briefing(pair["row"])

    briefings = await asyncio.gather(*[_brief_pair(pair) for pair in pairs])

    for pair, briefing in zip(pairs, briefings):
        row: SpreadsheetRow = pair["row"]
        lead = pair["lead"]
        lead_id = int(lead["id"])
        lead_number = str(pair["lead_number"])
        product_ru = _title_from_briefing(briefing, lead)
        new_name = build_proposed_name(lead_number, product_ru)
        old_name = _clean(lead.get("name"))
        task_text = _recommended_action(row, lead_number, product_ru)
        due_at = _task_due_timestamp()

        pipeline_id = lead.get("pipeline_id")
        target_status_id = None
        if _incoming_status(lead.get("status_name")):
            if isinstance(pipeline_id, int) and pipeline_id not in status_cache:
                status_cache[pipeline_id] = await _first_contact_status_id(pipeline_id)
            target_status_id = (
                status_cache.get(pipeline_id)
                if isinstance(pipeline_id, int)
                else settings.kommo_default_status_id
            )
            if target_status_id == lead.get("status_id"):
                target_status_id = None

        old_comment = _clean(row.marketing_comment)
        sheet_updates.append(
            {
                "row_number": row.row_number,
                "row_fingerprint": _row_fingerprint(row),
                "old_lead_number": "",
                "new_lead_number": lead_number,
                "old_comment": old_comment,
                "new_comment": old_comment,
                "marketing_status": row.lead_status,
                "product": row.product,
                "kommo_lead_id": lead_id,
                "matched_by": pair.get("matched_by"),
            }
        )
        if old_name != new_name:
            kommo_renames.append(
                {
                    "kommo_lead_id": lead_id,
                    "row_number": row.row_number,
                    "lead_number": lead_number,
                    "old_name": old_name,
                    "new_name": new_name,
                    "short_product_ru": product_ru,
                    "url": lead.get("url"),
                }
            )

        onboarding_actions.append(
            {
                "kommo_lead_id": lead_id,
                "row_number": row.row_number,
                "lead_number": lead_number,
                "old_name": old_name,
                "new_name": new_name,
                "short_product_ru": product_ru,
                "matched_by": pair.get("matched_by"),
                "target_status_id": target_status_id,
                "task_text": task_text,
                "task_due_at": due_at,
                "briefing": {
                    "about_ru": briefing.about_ru,
                    "talk_points_ru": list(briefing.talk_points_ru),
                    "call_goal_ru": briefing.call_goal_ru,
                },
                "analysis_note": _analysis_note(
                    row=row,
                    lead=lead,
                    lead_number=lead_number,
                    product_ru=product_ru,
                    matched_by=str(pair.get("matched_by") or "unknown"),
                    task_text=task_text,
                    briefing=briefing,
                ),
                "contact_card": {
                    "name": _clean(row.client_name) or _clean(lead.get("contact_name")),
                    "phone": _clean(row.phone) or (list(lead.get("phones") or [""])[0]),
                    "email": _clean(row.email) or (list(lead.get("emails") or [""])[0]),
                    "product": product_ru,
                    "lead_number": lead_number,
                    "kommo_lead_id": lead_id,
                },
                "url": lead.get("url"),
            }
        )

    # Keep newest-first order in the Telegram preview / apply payload.
    sheet_updates.sort(key=lambda item: item["row_number"], reverse=True)
    kommo_renames.sort(key=lambda item: item["row_number"], reverse=True)
    onboarding_actions.sort(key=lambda item: item["row_number"], reverse=True)
    table_duplicates.sort(key=lambda item: _number_sort_key(item["lead_number"]))
    kommo_duplicates.sort(key=lambda item: _number_sort_key(item["lead_number"]))

    report: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "spreadsheet_rows_count": len(rows),
        "kommo_leads_count": len(kommo_leads),
        "matched_count": len(onboarding_actions),
        "new_rows_count": len(new_rows),
        "newest_first": True,
        "max_new_rows": max(1, int(settings.lead_status_sync_max_new_rows or 5)),
        "sheet_updates": sheet_updates,
        "comment_updates_count": 0,
        "number_assignments_count": len(sheet_updates),
        "kommo_renames": kommo_renames,
        "kommo_renames_count": len(kommo_renames),
        "onboarding_actions": onboarding_actions,
        "unmatched_table_rows": [
            {
                "row_number": row.row_number,
                "lead_number": row.lead_number,
                "product": row.product,
                "client_name": row.client_name,
                "reason": "no_unique_kommo_match",
            }
            for row in sorted(
                unmatched_new_rows, key=lambda item: item.row_number, reverse=True
            )
        ],
        # The manual command intentionally does not report all historical Kommo-only leads.
        "kommo_only": [],
        "unnumbered_kommo": [],
        "table_duplicates": table_duplicates,
        "kommo_duplicates": kommo_duplicates,
        "kommo_truncated": bool(kommo_result.get("truncated")),
        "pipeline_id": kommo_result.get("pipeline_id"),
        "pipeline_name": kommo_result.get("pipeline_name"),
        "marketing_status_preserved": True,
        "manual_onboarding_only": True,
    }
    report["updates_count"] = len(onboarding_actions)
    report["updates_digest"] = _updates_digest(report)
    report["has_differences"] = bool(
        onboarding_actions
        or unmatched_new_rows
        or table_duplicates
        or kommo_duplicates
    )
    return report


async def apply_confirmed_report(
    *,
    expected_digest: str,
    expected_updates_count: int,
) -> dict[str, Any]:
    """Rebuild preview, write Y and onboard only the verified matched deals."""
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

    completed: list[dict[str, Any]] = []
    skipped_actions: list[dict[str, Any]] = []
    contact_cards: list[dict[str, Any]] = []
    renamed_count = 0
    moved_count = 0
    note_count = 0
    task_count = 0

    for action in fresh_report.get("onboarding_actions") or []:
        row = rows_by_position.get(int(action["row_number"]))
        if row is None or _clean(row.lead_number) != _clean(action["lead_number"]):
            skipped_actions.append({**action, "reason": "sheet_number_not_applied"})
            continue
        lead_id = int(action["kommo_lead_id"])
        try:
            details = await kommo_service.get_lead_details(lead_id)
            current_name = _clean(details.get("name"))
            current_number = parse_internal_number(current_name)
            if current_name != _clean(action.get("old_name")) and current_number != _clean(
                action.get("lead_number")
            ):
                skipped_actions.append({**action, "reason": "kommo_name_changed"})
                continue

            update_kwargs: dict[str, Any] = {}
            if current_name != _clean(action.get("new_name")):
                update_kwargs["name"] = str(action["new_name"])
            target_status_id = action.get("target_status_id")
            if isinstance(target_status_id, int) and details.get("status_id") != target_status_id:
                update_kwargs["status_id"] = target_status_id
            if update_kwargs:
                await kommo_service.update_kommo_lead(lead_id, **update_kwargs)
                if "name" in update_kwargs:
                    renamed_count += 1
                if "status_id" in update_kwargs:
                    moved_count += 1

            marker = f"[BBS-ONBOARD-{action['lead_number']}-{lead_id}]"
            recent_notes = await kommo_service.get_recent_common_notes(lead_id, limit=50)
            if not any(marker in _clean(note.get("text")) for note in recent_notes):
                await kommo_service.add_common_note(
                    lead_id, str(action.get("analysis_note") or "")
                )
                note_count += 1

            tasks = await kommo_service.get_open_lead_tasks(lead_id, limit=50)
            task_marker = f"№{action['lead_number']}"
            if not any(task_marker in _clean(task.get("text")) for task in tasks):
                await kommo_service.create_lead_task(
                    lead_id=lead_id,
                    text=str(action.get("task_text") or "Квалифицировать новый лид"),
                    complete_till=int(action.get("task_due_at") or _task_due_timestamp()),
                )
                task_count += 1

            card = dict(action.get("contact_card") or {})
            if _clean(card.get("phone")):
                contact_cards.append(card)
            completed.append(action)
        except Exception as exc:
            logger.warning("Could not onboard Kommo lead %s: %s", lead_id, exc)
            skipped_actions.append(
                {**action, "reason": "kommo_onboarding_failed", "error": type(exc).__name__}
            )

    return {
        "stale": False,
        "report": fresh_report,
        **sheet_result,
        "renamed_count": renamed_count,
        "renamed": completed,
        "status_moved_count": moved_count,
        "note_count": note_count,
        "task_count": task_count,
        "contact_cards": contact_cards,
        "rename_skipped": skipped_actions,
    }


async def periodic_status_sync_loop() -> None:
    """Periodic onboarding is intentionally disabled; the Telegram button is manual."""
    logger.info(
        "Periodic lead registry sync is disabled by design; use the Telegram sync button."
    )
    return
