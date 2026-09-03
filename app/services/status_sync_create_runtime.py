"""Create missing Kommo deals during the confirmed Google Sheets status sync.

This runtime extends the owner-facing registry workflow without weakening its
safety guarantees:

* rows that already have one reliable Kommo match keep the existing onboarding;
* rows with no exact contact-linked Kommo lead become explicit create actions;
* rows with ambiguous existing contact-linked leads stay pending and are not
  assigned Y automatically;
* missing deals are created before the final Google Sheets write, then the whole
  report is rebuilt so the normal onboarding path works with real Kommo IDs;
* if a create fails, Y is left empty so the next /status_sync can retry it.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from contextvars import ContextVar
from typing import Any

from app.services import (
    lead_registry_runtime,
    lead_status_sync_service,
    kommo_service,
    runtime_extensions,
    telegram_service,
)

logger = logging.getLogger(__name__)
_INSTALLED = False
_CREATE_CONCURRENCY = 4

# _select_unique_contact_lead runs in the parent task after its parallel lookups,
# therefore ContextVar safely keeps one preview's classification isolated.
_CONTACT_CLASSIFICATION: ContextVar[dict[int, dict[str, Any]]] = ContextVar(
    "status_sync_contact_classification", default={}
)


def _clean(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()


def _creation_digest(report: dict[str, Any]) -> str:
    payload = {
        "base": lead_status_sync_service._updates_digest(report),  # noqa: SLF001
        "create": [
            {
                "row_number": item.get("row_number"),
                "lead_number": item.get("lead_number"),
                "phone": item.get("phone"),
                "email": item.get("email"),
                "product": item.get("product"),
            }
            for item in report.get("create_actions") or []
        ],
        "ambiguous": [
            {
                "row_number": item.get("row_number"),
                "reason": item.get("reason"),
            }
            for item in report.get("unmatched_table_rows") or []
        ],
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


async def _build_create_actions(rows: list[Any]) -> list[dict[str, Any]]:
    if not rows:
        return []

    semaphore = asyncio.Semaphore(lead_status_sync_service._BRIEFING_CONCURRENCY)  # noqa: SLF001

    async def one(row: Any) -> dict[str, Any]:
        async with semaphore:
            briefing = await lead_status_sync_service._safe_briefing(row)  # noqa: SLF001
        lead_stub = {"name": _clean(getattr(row, "product", None))}
        product_ru = lead_status_sync_service._title_from_briefing(  # noqa: SLF001
            briefing, lead_stub
        )
        lead_number = lead_registry_runtime._desired_number(row)  # noqa: SLF001
        new_name = lead_registry_runtime.build_proposed_name(lead_number, product_ru)
        return {
            "row_number": int(row.row_number),
            "lead_number": lead_number,
            "new_name": new_name,
            "short_product_ru": product_ru,
            "client_name": _clean(getattr(row, "client_name", None)),
            "company": _clean(getattr(row, "company", None)),
            "phone": _clean(getattr(row, "phone", None)),
            "email": _clean(getattr(row, "email", None)),
            "product": _clean(getattr(row, "product", None)),
            "budget": _clean(getattr(row, "budget", None)),
            "contact_channel": _clean(getattr(row, "contact_channel", None)),
            "region": _clean(getattr(row, "region", None)),
            "briefing": {
                "about_ru": briefing.about_ru,
                "talk_points_ru": list(briefing.talk_points_ru),
                "call_goal_ru": briefing.call_goal_ru,
            },
        }

    return list(await asyncio.gather(*[one(row) for row in rows]))


async def _target_placement(report: dict[str, Any]) -> tuple[int | None, int | None]:
    settings = lead_status_sync_service.settings
    pipeline_id = (
        settings.kommo_poland_pipeline_id
        or settings.lead_status_sync_pipeline_id
        or report.get("pipeline_id")
        or kommo_service.configured_menu_pipeline_id()
    )
    pipeline_id = int(pipeline_id) if isinstance(pipeline_id, int) else None

    status_id = settings.kommo_first_contact_status_id
    if not isinstance(status_id, int) and pipeline_id is not None:
        status_id = await lead_status_sync_service._first_contact_status_id(  # noqa: SLF001
            pipeline_id
        )
    status_id = int(status_id) if isinstance(status_id, int) else None
    return pipeline_id, status_id


async def _create_one(action: dict[str, Any], report: dict[str, Any]) -> dict[str, Any]:
    pipeline_id, status_id = await _target_placement(report)
    client_data = {
        "name": _clean(action.get("client_name")),
        "company": _clean(action.get("company")),
        "phone": _clean(action.get("phone")),
        "email": _clean(action.get("email")),
    }
    note_text = (
        f"[BBS-STATUS-SYNC-CREATE-{action['lead_number']}]\n"
        "Сделка создана автоматически из подтверждённого /status_sync.\n"
        f"Google Sheets: строка {action['row_number']}.\n"
        f"Запрос: {_clean(action.get('product')) or 'не указан'}."
    )
    created = await kommo_service.create_lead_from_external_intake(
        lead_title=str(action["new_name"]),
        client_data=client_data,
        # Empty dict deliberately enables deterministic company linking while
        # avoiding website-specific custom-field mapping.
        lead_fields={},
        note_text=note_text,
    )

    update_kwargs: dict[str, int] = {}
    if pipeline_id is not None and created.get("pipeline_id") != pipeline_id:
        update_kwargs["pipeline_id"] = pipeline_id
    if status_id is not None and created.get("status_id") != status_id:
        update_kwargs["status_id"] = status_id
    if update_kwargs:
        await kommo_service.update_kommo_lead(
            int(created["lead_id"]),
            **update_kwargs,
        )
        created = {**created, **update_kwargs}
    return created


async def _create_missing(actions: list[dict[str, Any]], report: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not actions:
        return [], []
    semaphore = asyncio.Semaphore(_CREATE_CONCURRENCY)

    async def one(action: dict[str, Any]) -> tuple[dict[str, Any], Any]:
        async with semaphore:
            try:
                return action, await _create_one(action, report)
            except Exception as exc:  # keep other independent rows retryable
                logger.exception(
                    "status_sync_create_failed row=%s lead_number=%s",
                    action.get("row_number"),
                    action.get("lead_number"),
                )
                return action, exc

    results = await asyncio.gather(*[one(action) for action in actions])
    created: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    for action, outcome in results:
        if isinstance(outcome, Exception):
            failed.append(
                {
                    "row_number": action.get("row_number"),
                    "lead_number": action.get("lead_number"),
                    "error": type(outcome).__name__,
                }
            )
        else:
            created.append({"action": action, "result": outcome})
    return created, failed


def install_status_sync_create_runtime() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True

    original_select = lead_registry_runtime._select_unique_contact_lead  # noqa: SLF001

    def select_with_classification(row: Any, leads: list[dict[str, Any]]) -> dict[str, Any] | None:
        selected = original_select(row, leads)
        state = dict(_CONTACT_CLASSIFICATION.get())
        state[int(row.row_number)] = {
            "candidate_count": len(leads),
            "selected": bool(selected),
        }
        _CONTACT_CLASSIFICATION.set(state)
        return selected

    lead_registry_runtime._select_unique_contact_lead = select_with_classification  # noqa: SLF001

    original_enhance = lead_registry_runtime._enhance_report  # noqa: SLF001

    async def enhance_report_with_create_actions(report: dict[str, Any], rows: list[Any]) -> dict[str, Any]:
        token = _CONTACT_CLASSIFICATION.set({})
        try:
            enhanced = await original_enhance(report, rows)
            classification = dict(_CONTACT_CLASSIFICATION.get())
        finally:
            _CONTACT_CLASSIFICATION.reset(token)

        row_by_number = {int(row.row_number): row for row in rows}
        create_rows: list[Any] = []
        ambiguous: list[dict[str, Any]] = []

        for item in enhanced.get("unmatched_table_rows") or []:
            row_number = int(item.get("row_number") or 0)
            row = row_by_number.get(row_number)
            if row is None:
                continue
            info = classification.get(row_number, {})
            candidate_count = int(info.get("candidate_count") or 0)
            if candidate_count > 0:
                ambiguous.append(
                    {
                        **item,
                        "reason": "ambiguous_existing_kommo_contact",
                        "candidate_count": candidate_count,
                    }
                )
                continue

            # A stable phone/email lets the freshly created deal be found again
            # on the mandatory post-create verification pass. Without one, keep
            # the row pending rather than risking duplicate creation.
            if not (_clean(getattr(row, "phone", None)) or _clean(getattr(row, "email", None))):
                ambiguous.append({**item, "reason": "missing_phone_or_email"})
                continue
            create_rows.append(row)

        create_actions = await _build_create_actions(create_rows)
        ambiguous_rows = {int(item.get("row_number") or 0) for item in ambiguous}
        safe_sheet_updates = [
            item
            for item in enhanced.get("sheet_updates") or []
            if int(item.get("row_number") or 0) not in ambiguous_rows
        ]

        enhanced = dict(enhanced)
        enhanced.update(
            {
                "create_actions": create_actions,
                "create_count": len(create_actions),
                "unmatched_table_rows": ambiguous,
                "sheet_updates": safe_sheet_updates,
                "number_assignments_count": len(safe_sheet_updates),
                "updates_count": len(safe_sheet_updates),
                "manual_onboarding_only": False,
                "creates_missing_kommo_leads": True,
            }
        )
        enhanced["updates_digest"] = _creation_digest(enhanced)
        enhanced["has_differences"] = bool(
            safe_sheet_updates
            or enhanced.get("onboarding_actions")
            or create_actions
            or ambiguous
            or enhanced.get("table_duplicates")
            or enhanced.get("kommo_duplicates")
        )
        return enhanced

    lead_registry_runtime._enhance_report = enhance_report_with_create_actions  # noqa: SLF001

    original_apply = lead_status_sync_service.apply_confirmed_report

    async def apply_with_missing_deal_creation(*, expected_digest: str, expected_updates_count: int) -> dict[str, Any]:
        pre_report = await lead_status_sync_service.build_status_sync_report()
        if (
            pre_report.get("updates_digest") != expected_digest
            or int(pre_report.get("updates_count") or 0) != expected_updates_count
        ):
            return {
                "stale": True,
                "report": pre_report,
                "updated_count": 0,
                "created_count": 0,
                "creation_failed_count": 0,
            }

        create_actions = list(pre_report.get("create_actions") or [])
        created, failed = await _create_missing(create_actions, pre_report)
        if failed:
            # Do not write Y for any row in this pass. Successful Kommo creates
            # are harmless: the next preview will match them and continue.
            fresh = await lead_status_sync_service.build_status_sync_report()
            return {
                "stale": False,
                "report": fresh,
                "updated_count": 0,
                "renamed_count": 0,
                "status_moved_count": 0,
                "note_count": 0,
                "task_count": 0,
                "contact_cards": [],
                "rename_skipped": [],
                "skipped": [],
                "created_count": len(created),
                "creation_failed_count": len(failed),
                "creation_errors": failed,
                "needs_retry": True,
            }

        if created:
            # Give Kommo's contact/lead indexes a brief moment, then rebuild the
            # report so base onboarding operates only on real Kommo IDs.
            await asyncio.sleep(0.35)
            post_report = await lead_status_sync_service.build_status_sync_report()
            created_rows = {
                int(item["action"]["row_number"])
                for item in created
            }
            remaining_create_rows = {
                int(item.get("row_number") or 0)
                for item in post_report.get("create_actions") or []
            }
            if created_rows & remaining_create_rows:
                await asyncio.sleep(0.75)
                post_report = await lead_status_sync_service.build_status_sync_report()
                remaining_create_rows = {
                    int(item.get("row_number") or 0)
                    for item in post_report.get("create_actions") or []
                }
            if created_rows & remaining_create_rows:
                return {
                    "stale": False,
                    "report": post_report,
                    "updated_count": 0,
                    "renamed_count": 0,
                    "status_moved_count": 0,
                    "note_count": 0,
                    "task_count": 0,
                    "contact_cards": [],
                    "rename_skipped": [],
                    "skipped": [],
                    "created_count": len(created),
                    "creation_failed_count": 0,
                    "creation_errors": [],
                    "needs_retry": True,
                }
            expected_digest = str(post_report.get("updates_digest") or "")
            expected_updates_count = int(post_report.get("updates_count") or 0)

        result = await original_apply(
            expected_digest=expected_digest,
            expected_updates_count=expected_updates_count,
        )
        if not result.get("stale"):
            result["created_count"] = len(created)
            result["creation_failed_count"] = 0
        return result

    lead_status_sync_service.apply_confirmed_report = apply_with_missing_deal_creation

    original_format = runtime_extensions._format_onboarding_report

    def format_report_with_creation(report: dict[str, Any]) -> str:
        text = original_format(report)
        create_count = int(report.get("create_count") or 0)
        replacement = (
            f"🆕 Будет создано отсутствующих сделок Kommo: <b>{create_count}</b>."
        )
        text = text.replace(
            "🔒 <b>Сделки в Kommo не создаются.</b>",
            replacement,
        )
        return text

    runtime_extensions._format_onboarding_report = format_report_with_creation

    async def send_confirmation_with_creation(chat_id: int, report: dict[str, Any]) -> dict[str, Any]:
        count = int(report.get("updates_count") or 0)
        return await telegram_service.send_message(
            chat_id,
            (
                "⚠️ <b>ПОДТВЕРЖДЕНИЕ ОБРАБОТКИ НОВЫХ ЛИДОВ</b>\n\n"
                f"Будет обработано строк: <b>{count}</b>.\n\n"
                "Для строки без сделки бот сначала создаст сделку Kommo и контакт, "
                "затем повторно проверит Kommo и только после этого заполнит Y. "
                "При неоднозначном совпадении новая сделка не создаётся и Y остаётся пустым.\n\n"
                "Номер Y равен номеру строки Google Sheets. Колонки W и X не изменяются."
            ),
            reply_markup={
                "inline_keyboard": [
                    [
                        {
                            "text": f"✅ Да, обработать {count}",
                            "callback_data": "sync:confirm",
                        }
                    ],
                    [{"text": "❌ Отмена", "callback_data": "sync:cancel"}],
                ]
            },
        )

    telegram_service.send_status_sync_confirmation = send_confirmation_with_creation

    original_send_result = telegram_service.send_status_sync_result

    async def send_result_with_creation(chat_id: int, result: dict[str, Any]) -> dict[str, Any]:
        failed_count = int(result.get("creation_failed_count") or 0)
        created_count = int(result.get("created_count") or 0)
        if result.get("needs_retry"):
            response = await telegram_service.send_message(
                chat_id,
                (
                    "⚠️ <b>ОБРАБОТКА НЕ ЗАВЕРШЕНА</b>\n\n"
                    f"Создано новых сделок Kommo: <b>{created_count}</b>\n"
                    f"Не удалось создать: <b>{failed_count}</b>\n"
                    "Номера Y в этом проходе не заполнялись. Запустите проверку ещё раз — "
                    "успешно созданные сделки будут найдены, а оставшиеся будут повторены."
                ),
                reply_markup={
                    "inline_keyboard": [
                        [{"text": "🔄 Проверить ещё", "callback_data": "sync:run"}],
                        [{"text": "🏠 Главное меню", "callback_data": "menu:home"}],
                    ]
                },
            )
            return response

        response = await original_send_result(chat_id, result)
        if created_count:
            await telegram_service.send_message(
                chat_id,
                f"🆕 Создано новых сделок Kommo: <b>{created_count}</b>",
            )
        return response

    telegram_service.send_status_sync_result = send_result_with_creation

    logger.info("Status-sync missing Kommo deal creation installed")
