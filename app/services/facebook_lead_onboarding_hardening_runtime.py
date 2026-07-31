"""Final hardening for the sequential Facebook lead onboarding workflow.

This layer addresses two production-specific behaviours:
- Meta form phone/email may live on the Kommo lead instead of the contact;
- Kommo robots may rewrite the title while an unsorted lead moves to First contact.

The final write order is therefore:
Google Sheets Y -> First contact -> note -> task -> final title verification.
"""
from __future__ import annotations

import asyncio
import html
import logging
from typing import Any

from app.services import operator_experience_runtime

logger = logging.getLogger(__name__)
_INSTALLED = False


def _identity_snapshot(details: dict[str, Any]) -> dict[str, Any]:
    """Combine standard contact channels with Facebook form fields on the lead."""
    from app.services import facebook_lead_onboarding_runtime as onboarding

    snapshot = dict(onboarding.lead_contact_snapshot(details) or {})
    phones = [onboarding._clean(value) for value in snapshot.get("phones") or [] if onboarding._clean(value)]
    emails = [onboarding._clean(value) for value in snapshot.get("emails") or [] if onboarding._clean(value)]
    contact_name = onboarding._clean(snapshot.get("contact_name"))
    company = onboarding._clean(snapshot.get("company"))

    entities = list(details.get("contacts") or []) + [details]
    for entity in entities:
        for marker, raw_value in operator_experience_runtime._custom_values(entity):
            marker_folded = marker.casefold()
            value = onboarding._clean(raw_value)
            if not value:
                continue
            if any(
                token in marker_folded
                for token in ("phone", "telefon", "numer", "телефон", "номер")
            ):
                if value.casefold() not in {item.casefold() for item in phones}:
                    phones.append(value)
            if (
                "@" in value
                or any(token in marker_folded for token in ("email", "e-mail", "poczta", "почт"))
            ):
                if "@" in value and value.casefold() not in {
                    item.casefold() for item in emails
                }:
                    emails.append(value)
            if not contact_name and any(
                token in marker_folded
                for token in ("contact name", "full name", "imię", "imie", "клиент", "имя")
            ):
                contact_name = value
            if not company and any(
                token in marker_folded for token in ("company", "firma", "компания")
            ):
                company = value

    return {
        **snapshot,
        "phones": phones,
        "emails": emails,
        "contact_name": contact_name or None,
        "company": company or None,
    }


async def _discover_from_facebook_form_fields() -> dict[str, Any]:
    from app.services import facebook_lead_onboarding_runtime as onboarding

    rows, unsorted = await asyncio.gather(
        asyncio.to_thread(
            onboarding.google_sheets_service.get_rows,
            force_refresh=True,
        ),
        onboarding.kommo_service.get_all_unsorted_leads(
            pipeline_id=(
                onboarding.settings.kommo_unreviewed_pipeline_id
                or onboarding.settings.lead_status_sync_pipeline_id
                or None
            )
        ),
    )
    used_rows: set[int] = set()
    queue: list[dict[str, Any]] = []
    unmatched: list[dict[str, Any]] = []

    for lead in sorted(
        list(unsorted.get("leads") or []),
        key=lambda item: item.get("created_at") or 0,
    ):
        if not onboarding._is_facebook(lead):
            continue
        lead_id = int(lead.get("id") or 0)
        if not lead_id:
            continue
        try:
            details = await onboarding.kommo_service.get_lead_details(lead_id)
        except Exception as exc:
            logger.warning("Could not load Facebook lead %s: %s", lead_id, exc)
            unmatched.append(
                {
                    "lead_id": lead_id,
                    "name": lead.get("name"),
                    "reason": "kommo_details_unavailable",
                    "candidate_rows": [],
                }
            )
            continue

        identity = _identity_snapshot(details)
        match = onboarding.match_lead_to_rows(
            phones=identity.get("phones"),
            emails=identity.get("emails"),
            contact_name=identity.get("contact_name"),
            company=identity.get("company"),
            product_hint=details.get("name"),
            rows=rows,
            require_lead_number=False,
        )
        candidate = match.single
        if (
            candidate is None
            or candidate.score < 60
            or candidate.row.row_number in used_rows
        ):
            unmatched.append(
                {
                    "lead_id": lead_id,
                    "name": details.get("name") or lead.get("name"),
                    "reason": (
                        "sheet_row_already_used"
                        if candidate is not None
                        and candidate.row.row_number in used_rows
                        else "ambiguous_or_no_exact_match"
                    ),
                    "candidate_rows": [
                        item.row.row_number for item in match.candidates
                    ],
                }
            )
            continue
        used_rows.add(candidate.row.row_number)
        queue.append(
            {
                "lead_id": lead_id,
                "row_number": candidate.row.row_number,
                "matched_by": candidate.matched_by,
            }
        )
    return {"items": queue, "unmatched": unmatched}


async def _apply_in_safe_order(preview: dict[str, Any]) -> dict[str, Any]:
    """Apply one lead idempotently and verify the final title after stage robots."""
    from app.services import facebook_lead_onboarding_runtime as onboarding

    lead_id = int(preview["lead_id"])
    desired = str(preview["lead_number"])
    target_row = int(preview["row_number"])
    desired_name = onboarding._clean(preview.get("new_name"))
    completed_steps: list[str] = []

    rows, details = await asyncio.gather(
        asyncio.to_thread(
            onboarding.google_sheets_service.get_rows,
            force_refresh=True,
        ),
        onboarding.kommo_service.get_lead_details(lead_id),
    )
    row = next((item for item in rows if int(item.row_number) == target_row), None)
    if row is None:
        return {"stale": True, "reason": "sheet_row_missing"}
    if (
        onboarding._digest(row, details, desired, str(preview["product_ru"]))
        != preview.get("digest")
    ):
        return {"stale": True, "reason": "source_changed"}

    identity = _identity_snapshot(details)
    match = onboarding.match_lead_to_rows(
        phones=identity.get("phones"),
        emails=identity.get("emails"),
        contact_name=identity.get("contact_name"),
        company=identity.get("company"),
        product_hint=details.get("name"),
        rows=[row],
        require_lead_number=False,
    )
    if match.single is None or match.single.score < 60:
        return {"stale": True, "reason": "contact_match_changed"}

    duplicate_rows = [
        int(item.row_number)
        for item in rows
        if int(item.row_number) != target_row
        and onboarding._clean(item.lead_number) == desired
    ]
    if duplicate_rows:
        return {
            "stale": True,
            "reason": "duplicate_sheet_lead_number",
            "duplicate_rows": duplicate_rows,
        }

    current_y = onboarding._clean(row.lead_number)
    if current_y not in {"", desired}:
        return {"stale": True, "reason": "lead_number_changed"}

    current_name = onboarding._clean(details.get("name"))
    existing_number = onboarding.lead_status_sync_service.parse_internal_number(
        current_name
    )
    if existing_number and existing_number != desired:
        return {
            "stale": True,
            "reason": "kommo_number_conflict",
            "existing_number": existing_number,
        }
    if current_name not in {
        onboarding._clean(preview.get("old_name")),
        desired_name,
    } and existing_number is None and not onboarding._is_facebook(
        {"name": current_name}
    ):
        return {"stale": True, "reason": "kommo_name_changed_manually"}

    sheet_updated = False
    note_added = False
    task_added = False
    stage_updated = False
    name_updated = False
    try:
        if current_y != desired:
            sheet = await asyncio.to_thread(
                onboarding.google_sheets_service.apply_lead_registry_updates,
                [
                    {
                        "row_number": row.row_number,
                        "row_fingerprint": preview["row_fingerprint"],
                        "old_lead_number": current_y,
                        "new_lead_number": desired,
                        "old_comment": onboarding._clean(row.marketing_comment),
                        "new_comment": onboarding._clean(row.marketing_comment),
                        "marketing_status": row.lead_status,
                        "product": row.product,
                        "kommo_lead_id": lead_id,
                        "matched_by": preview.get("matched_by"),
                    }
                ],
            )
            if int(sheet.get("updated_count") or 0) != 1:
                return {"stale": True, "reason": "sheet_write_not_applied"}
            sheet_updated = True
        completed_steps.append("google_sheets_y")

        target_status_id = preview.get("target_status_id")
        if (
            isinstance(target_status_id, int)
            and details.get("status_id") != target_status_id
        ):
            await onboarding.kommo_service.update_kommo_lead(
                lead_id,
                status_id=target_status_id,
            )
            stage_updated = True
        completed_steps.append("first_contact")

        # Give Kommo robots a short chance to finish changes caused by accepting the
        # unsorted lead. The final title is deliberately written only afterwards.
        await asyncio.sleep(0.6)

        marker = f"[BBS-SMART-ONBOARD-{desired}-{lead_id}]"
        notes = await onboarding.kommo_service.get_recent_common_notes(
            lead_id,
            limit=50,
        )
        note_added = not any(
            marker in onboarding._clean(note.get("text")) for note in notes
        )
        if note_added:
            await onboarding.kommo_service.add_common_note(
                lead_id,
                preview["analysis_note"],
            )
        completed_steps.append("analysis_note")

        tasks = await onboarding.kommo_service.get_open_lead_tasks(
            lead_id,
            limit=50,
        )
        task_added = not any(
            f"№{desired}" in onboarding._clean(task.get("text"))
            for task in tasks
        )
        if task_added:
            await onboarding.kommo_service.create_lead_task(
                lead_id=lead_id,
                text=preview["task_text"],
                complete_till=int(preview["task_due_at"]),
                responsible_user_id=details.get("responsible_user_id"),
            )
        completed_steps.append("qualification_task")

        final_details = await onboarding.kommo_service.get_lead_details(lead_id)
        for delay in (0.0, 0.8, 1.8):
            if onboarding._clean(final_details.get("name")) == desired_name:
                break
            if delay:
                await asyncio.sleep(delay)
            await onboarding.kommo_service.update_kommo_lead(
                lead_id,
                name=desired_name,
            )
            name_updated = True
            final_details = await onboarding.kommo_service.get_lead_details(lead_id)
        if onboarding._clean(final_details.get("name")) != desired_name:
            return {
                "stale": True,
                "partial": True,
                "reason": "final_name_not_persisted",
                "completed_steps": completed_steps,
                "sheet_updated": sheet_updated,
                "stage_updated": stage_updated,
                "note_added": note_added,
                "task_added": task_added,
            }
        completed_steps.append("final_name")
    except Exception as exc:
        logger.exception("Safe Facebook onboarding failed for lead %s", lead_id)
        return {
            "stale": True,
            "partial": bool(completed_steps),
            "reason": "partial_apply_failed" if completed_steps else "apply_failed",
            "error_type": type(exc).__name__,
            "completed_steps": completed_steps,
            "sheet_updated": sheet_updated,
            "stage_updated": stage_updated,
            "note_added": note_added,
            "task_added": task_added,
        }

    return {
        "stale": False,
        "lead_number": desired,
        "new_name": desired_name,
        "sheet_updated": sheet_updated,
        "kommo_updated": stage_updated or name_updated,
        "stage_updated": stage_updated,
        "name_updated": name_updated,
        "note_added": note_added,
        "task_added": task_added,
        "completed_steps": completed_steps,
        "kommo_url": preview.get("kommo_url"),
    }


async def _confirm_keep_problem_on_screen(
    chat_id: int,
    user_id: int,
    state: dict[str, Any],
) -> None:
    from app.services import facebook_lead_onboarding_runtime as onboarding

    preview = dict(state.get("current_preview") or {})
    if not preview:
        await onboarding._show_current(chat_id, user_id, state)
        return
    if not onboarding.settings.google_sheets_write_enabled:
        await onboarding.telegram_service.send_message(
            chat_id,
            "🔒 Нужны GOOGLE_SHEETS_WRITE_ENABLED=true и право Editor.",
        )
        return

    await onboarding.telegram_service.send_message(
        chat_id,
        "🔐 Проверяю совпадение. Порядок: Y → Первый контакт → анализ и задача → итоговое название…",
    )
    result = await onboarding.apply(preview)
    if result.get("stale"):
        reason = onboarding._esc(result.get("reason"))
        details: list[str] = []
        if result.get("duplicate_rows"):
            details.append(
                "Строки с таким Y: "
                + ", ".join(str(value) for value in result["duplicate_rows"])
            )
        if result.get("existing_number"):
            details.append(
                f"Номер в Kommo: {onboarding._esc(result['existing_number'])}"
            )
        if result.get("partial"):
            steps = ", ".join(result.get("completed_steps") or []) or "нет"
            details.append(f"Уже выполненные шаги: {steps}")
        if result.get("error_type"):
            details.append(f"Тип ошибки: {result['error_type']}")
        await onboarding.telegram_state_service.set_state(
            user_id,
            state,
            ttl_seconds=onboarding.settings.telegram_state_ttl_minutes * 60,
        )
        heading = "⚠️ <b>ОБРАБОТКА НЕ ЗАВЕРШЕНА</b>" if result.get("partial") else "⚠️ <b>ЛИД НЕ ИЗМЕНЁН</b>"
        await onboarding.telegram_service.send_message(
            chat_id,
            heading
            + "\n\n"
            + f"Причина: <code>{reason}</code>"
            + ("\n" + "\n".join(html.escape(value) for value in details) if details else "")
            + "\n\nЛид остаётся текущим. Повтор безопасен: примечание и задача не дублируются.",
            reply_markup={
                "inline_keyboard": [
                    [{"text": "🔄 Повторить", "callback_data": "sync:confirm"}],
                    [{"text": "⏭ Пропустить", "callback_data": "sync:skip"}],
                    [{"text": "❌ Завершить", "callback_data": "sync:cancel"}],
                ]
            },
        )
        return

    await onboarding.telegram_service.send_message(
        chat_id,
        f"✅ <b>ЛИД №{result['lead_number']} ОБРАБОТАН</b>\n"
        f"Название: <b>{onboarding._esc(result['new_name'])}</b>\n"
        f"Y: {'обновлён' if result['sheet_updated'] else 'уже был'}\n"
        f"Первый контакт: {'установлен' if result['stage_updated'] else 'уже был'}\n"
        f"Примечание: {'добавлено' if result['note_added'] else 'уже было'}\n"
        f"Задача: {'добавлена' if result['task_added'] else 'уже была'}\n"
        "Итоговое название проверено после работы роботов Kommo.",
    )
    state["results"] = list(state.get("results") or []) + [result]
    state["index"] = int(state.get("index") or 0) + 1
    state["current_preview"] = None
    await onboarding.telegram_state_service.set_state(
        user_id,
        state,
        ttl_seconds=onboarding.settings.telegram_state_ttl_minutes * 60,
    )
    await onboarding._show_current(chat_id, user_id, state)


def install_facebook_lead_onboarding_hardening_runtime() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True

    from app.services import facebook_lead_onboarding_runtime as onboarding

    onboarding.lead_contact_snapshot = _identity_snapshot
    onboarding.discover = _discover_from_facebook_form_fields
    onboarding.apply = _apply_in_safe_order
    onboarding._confirm = _confirm_keep_problem_on_screen

    original_card = onboarding._card

    def card_with_real_write_order(
        preview: dict[str, Any],
        position: int,
        total: int,
    ) -> str:
        text = original_card(preview, position, total)
        return text.replace(
            "Y → новое название → Первый контакт → подробное примечание → одна задача.",
            "Y → Первый контакт → подробное примечание и задача → итоговое название с проверкой.",
        )

    onboarding._card = card_with_real_write_order
    logger.info("Facebook lead onboarding hardening runtime installed")
