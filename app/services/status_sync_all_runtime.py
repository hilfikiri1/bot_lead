"""Full-backlog status sync with safe creation of missing Kommo deals.

Owner workflow:
- process every Google Sheets product row with empty Y;
- reuse one reliably matched Kommo deal when it exists;
- create a new Kommo deal + contact when there is no candidate at all;
- never auto-create when a row has one or more ambiguous Kommo candidates;
- export one iPhone vCard bundle for every processed row that has a phone.

The manager confirmation remains mandatory. Columns W and X are never changed.
"""
from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

from app.services import (
    google_sheets_service,
    kommo_service,
    lead_status_sync_service,
    runtime_extensions,
)
from app.services.lead_matching_service import match_lead_to_rows
from app.services.unreviewed_leads_service import build_proposed_name

logger = logging.getLogger(__name__)
_INSTALLED = False


def _clean(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()


def all_pending_rows(rows: list[Any]) -> list[Any]:
    """Return every product row whose internal number Y is still empty."""
    pending = [
        row
        for row in rows
        if _clean(getattr(row, "product", None))
        and not _clean(getattr(row, "lead_number", None))
    ]
    pending.sort(key=lambda item: int(getattr(item, "row_number", 0) or 0), reverse=True)
    return pending


def _contact_key(card: dict[str, Any]) -> tuple[str, str, str]:
    return (
        _clean(card.get("lead_number")),
        _clean(card.get("kommo_lead_id")),
        _clean(card.get("phone")),
    )


def _all_matched_contact_cards(result: dict[str, Any]) -> list[dict[str, Any]]:
    cards: dict[tuple[str, str, str], dict[str, Any]] = {}
    for raw in result.get("contact_cards") or []:
        card = dict(raw or {})
        if _clean(card.get("phone")):
            cards[_contact_key(card)] = card

    report = dict(result.get("report") or {})
    for action in report.get("onboarding_actions") or []:
        card = dict((action or {}).get("contact_card") or {})
        if _clean(card.get("phone")):
            cards.setdefault(_contact_key(card), card)
    return list(cards.values())


def _sync_digest(report: dict[str, Any]) -> str:
    stable = {
        "matched": [
            {
                "row": item.get("row_number"),
                "lead": item.get("kommo_lead_id"),
                "number": item.get("lead_number"),
                "name": item.get("new_name"),
            }
            for item in report.get("onboarding_actions") or []
        ],
        "create": [
            {
                "row": item.get("row_number"),
                "number": item.get("lead_number"),
                "name": item.get("new_name"),
                "phone": (item.get("contact_card") or {}).get("phone"),
                "email": (item.get("contact_card") or {}).get("email"),
            }
            for item in report.get("create_actions") or []
        ],
    }
    payload = json.dumps(stable, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20]


async def _rows_without_any_kommo_candidate(
    row_numbers: set[int],
) -> tuple[list[Any], list[Any]]:
    """Split unmatched rows into safe-to-create and ambiguous rows.

    A row is safe only when *zero* unnumbered Kommo leads point to it. Any
    candidate at all is treated conservatively and remains manual, preventing
    accidental duplicate deals.
    """
    if not row_numbers:
        return [], []

    rows = await __import__("asyncio").to_thread(
        google_sheets_service.get_rows, force_refresh=True
    )
    targets = [row for row in rows if int(row.row_number) in row_numbers]
    if not targets:
        return [], []

    kommo_result = await kommo_service.get_all_leads_for_status_sync()
    unnumbered = [
        lead
        for lead in (kommo_result.get("leads") or [])
        if not lead_status_sync_service.parse_internal_number(lead.get("name"))
    ]
    enriched = await kommo_service.enrich_leads_with_contacts(unnumbered)
    candidate_counts = {int(row.row_number): 0 for row in targets}

    for lead in enriched:
        match = match_lead_to_rows(
            phones=list(lead.get("phones") or []),
            emails=list(lead.get("emails") or []),
            contact_name=lead.get("contact_name"),
            company=lead.get("company"),
            product_hint=lead.get("name"),
            rows=targets,
            require_lead_number=False,
        )
        if match.single is not None:
            candidate_counts[int(match.single.row.row_number)] += 1

    safe = [row for row in targets if candidate_counts[int(row.row_number)] == 0]
    ambiguous = [row for row in targets if candidate_counts[int(row.row_number)] > 0]
    safe.sort(key=lambda row: int(row.row_number), reverse=True)
    ambiguous.sort(key=lambda row: int(row.row_number), reverse=True)
    return safe, ambiguous


def _created_note(row: Any, action: dict[str, Any]) -> str:
    briefing = action.get("briefing") or {}
    points = list(briefing.get("talk_points_ru") or [])
    lines = [
        f"[BBS-ONBOARD-{action['lead_number']}-CREATED]",
        "ПЕРВИЧНЫЙ АНАЛИЗ НОВОГО ЛИДА",
        "",
        "Сделка создана автоматически из новой строки Google Sheets: в Kommo не было ни одного кандидата.",
        f"Внутренний номер: №{action['lead_number']}",
        f"Клиент: {_clean(getattr(row, 'client_name', None)) or 'не указан'}",
        f"Запрос из рекламы: {_clean(getattr(row, 'product', None)) or 'не указан'}",
        f"Телефон: {_clean(getattr(row, 'phone', None)) or 'не указан'}",
        f"Email: {_clean(getattr(row, 'email', None)) or 'не указан'}",
        f"Регион: {_clean(getattr(row, 'region', None)) or 'не указан'}",
        f"Бюджет: {_clean(getattr(row, 'budget', None)) or 'не указан'}",
        f"Канал: {_clean(getattr(row, 'contact_channel', None)) or 'не указан'}",
        "",
        "О ЧЁМ ЗАЯВКА",
        _clean(briefing.get("about_ru")) or _clean(getattr(row, "product", None)),
        "",
        "О ЧЁМ ГОВОРИТЬ",
    ]
    lines.extend(f"– {point}" for point in points)
    lines.extend(["", f"Следующий шаг: {action['task_text']}."])
    return "\n".join(lines)[:13_500]


def _sheet_update_for_created(row: Any, action: dict[str, Any], lead_id: int) -> dict[str, Any]:
    old_comment = _clean(getattr(row, "marketing_comment", None))
    return {
        "row_number": int(row.row_number),
        "row_fingerprint": lead_status_sync_service._row_fingerprint(row),  # noqa: SLF001
        "old_lead_number": "",
        "new_lead_number": str(action["lead_number"]),
        "old_comment": old_comment,
        "new_comment": old_comment,
        "marketing_status": getattr(row, "lead_status", None),
        "product": getattr(row, "product", None),
        "kommo_lead_id": lead_id,
        "matched_by": "created_from_sheets",
    }


def _format_row_for_manual(row: Any) -> dict[str, Any]:
    return {
        "row_number": int(row.row_number),
        "lead_number": _clean(getattr(row, "lead_number", None)),
        "product": getattr(row, "product", None),
        "client_name": getattr(row, "client_name", None),
        "reason": "possible_kommo_candidate",
    }


def install_status_sync_all_runtime() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True

    lead_status_sync_service._newest_new_rows = all_pending_rows  # noqa: SLF001

    original_build = lead_status_sync_service.build_status_sync_report

    async def build_report_with_creates() -> dict[str, Any]:
        report = await original_build()
        unmatched = list(report.get("unmatched_table_rows") or [])
        row_numbers = {
            int(item.get("row_number") or 0)
            for item in unmatched
            if int(item.get("row_number") or 0) > 0
        }
        safe_rows, ambiguous_rows = await _rows_without_any_kommo_candidate(row_numbers)

        used_numbers = {
            int(str(item.get("lead_number")))
            for item in report.get("onboarding_actions") or []
            if str(item.get("lead_number") or "").isdigit()
        }
        all_rows = await __import__("asyncio").to_thread(
            google_sheets_service.get_rows, force_refresh=True
        )
        used_numbers.update(
            int(str(row.lead_number))
            for row in all_rows
            if str(getattr(row, "lead_number", "") or "").isdigit()
        )
        next_number = max(used_numbers, default=0) + 1

        create_actions: list[dict[str, Any]] = []
        pipeline_id = report.get("pipeline_id")
        target_status_id = await lead_status_sync_service._first_contact_status_id(  # noqa: SLF001
            pipeline_id if isinstance(pipeline_id, int) else None
        )

        for row in safe_rows:
            while next_number in used_numbers:
                next_number += 1
            lead_number = str(next_number)
            used_numbers.add(next_number)
            next_number += 1

            briefing = await lead_status_sync_service._safe_briefing(row)  # noqa: SLF001
            product_ru = lead_status_sync_service._title_from_briefing(  # noqa: SLF001
                briefing, {"name": getattr(row, "product", None) or "Новый запрос"}
            )
            new_name = build_proposed_name(lead_number, product_ru)
            task_text = lead_status_sync_service._recommended_action(  # noqa: SLF001
                row, lead_number, product_ru
            )
            action = {
                "row_number": int(row.row_number),
                "lead_number": lead_number,
                "new_name": new_name,
                "short_product_ru": product_ru,
                "pipeline_id": pipeline_id,
                "target_status_id": target_status_id,
                "task_text": task_text,
                "task_due_at": lead_status_sync_service._task_due_timestamp(),  # noqa: SLF001
                "briefing": {
                    "about_ru": briefing.about_ru,
                    "talk_points_ru": list(briefing.talk_points_ru),
                    "call_goal_ru": briefing.call_goal_ru,
                },
                "contact_card": {
                    "name": _clean(getattr(row, "client_name", None)),
                    "phone": _clean(getattr(row, "phone", None)),
                    "email": _clean(getattr(row, "email", None)),
                    "product": product_ru,
                    "lead_number": lead_number,
                },
            }
            action["analysis_note"] = _created_note(row, action)
            create_actions.append(action)

        report["create_actions"] = create_actions
        report["create_count"] = len(create_actions)
        report["unmatched_table_rows"] = [_format_row_for_manual(row) for row in ambiguous_rows]
        report["updates_count"] = len(report.get("onboarding_actions") or []) + len(create_actions)
        report["updates_digest"] = _sync_digest(report)
        report["has_differences"] = bool(
            report["updates_count"]
            or report.get("unmatched_table_rows")
            or report.get("table_duplicates")
            or report.get("kommo_duplicates")
        )
        return report

    lead_status_sync_service.build_status_sync_report = build_report_with_creates

    async def apply_report_with_creates(
        *, expected_digest: str, expected_updates_count: int
    ) -> dict[str, Any]:
        fresh_report = await build_report_with_creates()
        if (
            fresh_report.get("updates_digest") != expected_digest
            or int(fresh_report.get("updates_count") or 0) != expected_updates_count
        ):
            return {
                "stale": True,
                "report": fresh_report,
                "updated_count": 0,
                "renamed_count": 0,
                "created_count": 0,
                "skipped": [],
            }

        matched_sheet = await __import__("asyncio").to_thread(
            google_sheets_service.apply_lead_registry_updates,
            list(fresh_report.get("sheet_updates") or []),
        )
        rows_after_write = await __import__("asyncio").to_thread(
            google_sheets_service.get_rows, force_refresh=True
        )
        rows_by_position = {int(row.row_number): row for row in rows_after_write}

        completed: list[dict[str, Any]] = []
        skipped_actions: list[dict[str, Any]] = []
        contact_cards: list[dict[str, Any]] = []
        renamed_count = moved_count = note_count = task_count = 0

        for action in fresh_report.get("onboarding_actions") or []:
            row = rows_by_position.get(int(action["row_number"]))
            if row is None or _clean(row.lead_number) != _clean(action["lead_number"]):
                skipped_actions.append({**action, "reason": "sheet_number_not_applied"})
                continue
            lead_id = int(action["kommo_lead_id"])
            try:
                details = await kommo_service.get_lead_details(lead_id)
                current_name = _clean(details.get("name"))
                current_number = lead_status_sync_service.parse_internal_number(current_name)
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
                    renamed_count += int("name" in update_kwargs)
                    moved_count += int("status_id" in update_kwargs)

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
                        complete_till=int(action.get("task_due_at") or 0),
                    )
                    task_count += 1
                completed.append(action)
            except Exception as exc:
                logger.warning("Could not onboard matched Kommo lead %s: %s", lead_id, exc)
                skipped_actions.append(
                    {**action, "reason": "kommo_onboarding_failed", "error": type(exc).__name__}
                )

        created_count = 0
        created_sheet_updates = 0
        creation_skipped: list[dict[str, Any]] = []
        rows_before_create = await __import__("asyncio").to_thread(
            google_sheets_service.get_rows, force_refresh=True
        )
        create_rows = {int(row.row_number): row for row in rows_before_create}

        for action in fresh_report.get("create_actions") or []:
            row = create_rows.get(int(action["row_number"]))
            if row is None or _clean(getattr(row, "lead_number", None)):
                creation_skipped.append({**action, "reason": "sheet_row_changed"})
                continue
            try:
                created = await kommo_service.create_lead_from_external_intake(
                    lead_title=str(action["new_name"]),
                    client_data={
                        "name": _clean(getattr(row, "client_name", None)),
                        "company": _clean(getattr(row, "company", None)),
                        "phone": _clean(getattr(row, "phone", None)),
                        "email": _clean(getattr(row, "email", None)),
                    },
                    lead_fields={
                        "product": _clean(getattr(row, "product", None)),
                        "budget": _clean(getattr(row, "budget", None)),
                        "region": _clean(getattr(row, "region", None)),
                        "contact_channel": _clean(getattr(row, "contact_channel", None)),
                    },
                    note_text=str(action.get("analysis_note") or ""),
                )
                lead_id = int(created["lead_id"])
                placement: dict[str, Any] = {}
                if isinstance(action.get("pipeline_id"), int):
                    placement["pipeline_id"] = int(action["pipeline_id"])
                if isinstance(action.get("target_status_id"), int):
                    placement["status_id"] = int(action["target_status_id"])
                if placement:
                    await kommo_service.update_kommo_lead(lead_id, **placement)
                    moved_count += int("status_id" in placement)

                await kommo_service.create_lead_task(
                    lead_id=lead_id,
                    text=str(action.get("task_text") or "Квалифицировать новый лид"),
                    complete_till=int(action.get("task_due_at") or 0),
                )
                task_count += 1
                note_count += int(bool(str(action.get("analysis_note") or "").strip()))

                sheet_result = await __import__("asyncio").to_thread(
                    google_sheets_service.apply_lead_registry_updates,
                    [_sheet_update_for_created(row, action, lead_id)],
                )
                created_sheet_updates += int(sheet_result.get("updated_count") or 0)
                created_count += 1
                card = dict(action.get("contact_card") or {})
                card["kommo_lead_id"] = lead_id
                if _clean(card.get("phone")):
                    contact_cards.append(card)
            except Exception as exc:
                logger.warning("Could not create Kommo lead for row %s: %s", action.get("row_number"), exc)
                creation_skipped.append(
                    {**action, "reason": "kommo_create_failed", "error": type(exc).__name__}
                )

        result = {
            "stale": False,
            "report": fresh_report,
            **matched_sheet,
            "updated_count": int(matched_sheet.get("updated_count") or 0) + created_sheet_updates,
            "renamed_count": renamed_count,
            "renamed": completed,
            "created_count": created_count,
            "status_moved_count": moved_count,
            "note_count": note_count,
            "task_count": task_count,
            "contact_cards": contact_cards,
            "rename_skipped": skipped_actions + creation_skipped,
        }
        result["contact_cards"] = _all_matched_contact_cards(result)
        result["contact_cards_count"] = len(result["contact_cards"])
        return result

    lead_status_sync_service.apply_confirmed_report = apply_report_with_creates

    original_format = runtime_extensions._format_onboarding_report

    def format_with_create_notice(report: dict[str, Any]) -> str:
        text = original_format(report)
        create_count = int(report.get("create_count") or 0)
        text = text.replace(
            "🔒 <b>Сделки в Kommo не создаются.</b>",
            f"➕ Новых сделок Kommo будет создано: <b>{create_count}</b>.",
        )
        return text

    runtime_extensions._format_onboarding_report = format_with_create_notice

    original_confirmation = runtime_extensions._send_onboarding_confirmation

    async def confirmation_with_create_notice(chat_id: int, report: dict[str, Any]) -> dict[str, Any]:
        # Reimplement only the short confirmation text so the manager sees the
        # potentially mutating create count before pressing Yes.
        count = int(report.get("updates_count") or 0)
        create_count = int(report.get("create_count") or 0)
        return await runtime_extensions.telegram_service.send_message(
            chat_id,
            (
                "⚠️ <b>ПОДТВЕРЖДЕНИЕ ОБРАБОТКИ НОВЫХ ЛИДОВ</b>\n\n"
                f"Всего к обработке: <b>{count}</b>.\n"
                f"Из них новых сделок Kommo будет создано: <b>{create_count}</b>.\n\n"
                "После подтверждения бот заполнит пустые Y, обновит найденные сделки, "
                "создаст отсутствующие сделки и контакты, добавит анализ и задачу, "
                "а затем пришлёт один файл .vcf для iPhone.\n\n"
                "При неоднозначном совпадении новая сделка НЕ создаётся. W и X не изменяются."
            ),
            reply_markup={
                "inline_keyboard": [
                    [{"text": f"✅ Да, обработать {count}", "callback_data": "sync:confirm"}],
                    [{"text": "❌ Отмена", "callback_data": "sync:cancel"}],
                ]
            },
        )

    runtime_extensions._send_onboarding_confirmation = confirmation_with_create_notice

    original_result = runtime_extensions._send_onboarding_result

    async def result_with_created_count(chat_id: int, result: dict[str, Any]) -> dict[str, Any]:
        response = await original_result(chat_id, result)
        created_count = int(result.get("created_count") or 0)
        if created_count:
            await runtime_extensions.telegram_service.send_message(
                chat_id, f"➕ Создано новых сделок Kommo: <b>{created_count}</b>"
            )
        return response

    runtime_extensions._send_onboarding_result = result_with_created_count
    logger.info("Full status-sync with safe Kommo deal creation installed")
