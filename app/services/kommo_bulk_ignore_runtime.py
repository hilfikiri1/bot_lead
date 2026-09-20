"""Make bulk DELETE actionable by routing reviewed leads to an ignore/lost status.

Kommo's public v4 lead API does not expose a supported hard-delete endpoint.
When the operator explicitly sends action=delete, this runtime therefore resolves
an existing ignore-like stage in the lead's current pipeline. If none exists, it
falls back to that pipeline's unambiguous lost/unsuccessful terminal stage.

The preview always shows the resolved target stage before confirmation.
"""

from __future__ import annotations

import copy
import html
from typing import Any

from app.services import kommo_bulk_json_runtime as bulk, kommo_service

_INSTALLED = False

_IGNORE_STAGE_ALIASES = {
    "ignore",
    "ignored",
    "игнор",
    "в игнор",
    "нецелевой",
    "не целевой",
    "нецелевой лид",
    "спам",
    "spam",
    "мусор",
    "trash",
}


def _normal(value: Any) -> str:
    return bulk._normal(value)


def _match_ignore_stage(
    statuses: list[dict[str, Any]],
) -> tuple[int | None, str | None]:
    aliases = {_normal(value) for value in _IGNORE_STAGE_ALIASES}

    exact = [
        item
        for item in statuses
        if isinstance(item.get("id"), int)
        and _normal(item.get("name")) in aliases
    ]
    if len(exact) == 1:
        return int(exact[0]["id"]), str(exact[0].get("name") or "Игнор")

    tokens = ("игнор", "нецелев", "не целев", "спам", "spam", "trash")
    fuzzy = [
        item
        for item in statuses
        if isinstance(item.get("id"), int)
        and any(token in _normal(item.get("name")) for token in tokens)
    ]
    if len(fuzzy) == 1:
        return int(fuzzy[0]["id"]), str(fuzzy[0].get("name") or "Игнор")

    return None, None


def install_kommo_bulk_ignore_runtime() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True

    original_preview = bulk.build_bulk_preview

    async def preview_with_delete_to_ignore(
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        report = await original_preview(payload)
        status_cache: dict[int, list[dict[str, Any]]] = {}

        for item in report.get("items") or []:
            if item.get("action") != "delete":
                continue

            pipeline_id = int(item.get("pipeline_id") or 0)
            if pipeline_id not in status_cache:
                status_cache[pipeline_id] = (
                    await kommo_service.get_pipeline_statuses(pipeline_id)
                    if pipeline_id
                    else []
                )
            statuses = status_cache[pipeline_id]

            status_id, status_name = _match_ignore_stage(statuses)
            mode = "ignore"
            if status_id is None:
                status_id, status_name = bulk._match_terminal_stage(
                    "close_lost",
                    statuses,
                )
                mode = "lost"

            if status_id is None:
                item["executable"] = False
                item["update_payload"] = None
                item["delete_mode"] = "unresolved"
                item["warnings"] = [
                    "Не найден однозначный этап Игнор/Нецелевой и не удалось "
                    "определить неуспешный финальный статус."
                ]
                report.setdefault("unresolved", []).append(
                    {
                        "index": item.get("source_index"),
                        "ref": {"kommo_id": int(item["lead_id"])},
                        "reason": item["warnings"][0],
                    }
                )
                continue

            item["update_payload"] = {
                "id": int(item["lead_id"]),
                "status_id": int(status_id),
            }
            item["target_status_name"] = status_name
            item["delete_mode"] = mode
            item["executable"] = True
            item["warnings"] = []

        report["warnings"] = [
            warning
            for warning in (report.get("warnings") or [])
            if "Удаление распознано, но не будет выполнено" not in str(warning)
        ]
        return report

    bulk.build_bulk_preview = preview_with_delete_to_ignore

    original_format = bulk.format_bulk_preview

    def format_with_delete_to_ignore(report: dict[str, Any]) -> str:
        text = original_format(report)
        text = text.replace(
            "Delete (не выполняется):",
            "DELETE → Игнор/неуспешно закрыть:",
        )
        text = text.replace(
            "delete — пропустить",
            "delete",
        )
        old_warning = (
            "🗑 <b>Важно:</b> команды delete только отображаются в отчёте и "
            "не заменяются автоматически на закрытие сделки."
        )
        new_warning = (
            "🗑 <b>DELETE:</b> hard-delete через публичный Kommo API недоступен. "
            "После подтверждения сделка будет переведена в указанный выше этап "
            "Игнор/Нецелевой либо в неуспешный финальный статус."
        )
        text = text.replace(old_warning, new_warning)
        return text[:4000]

    bulk.format_bulk_preview = format_with_delete_to_ignore

    original_execute = bulk._execute_bulk_json

    async def execute_delete_as_ignore(action: Any) -> dict[str, Any]:
        source_payload = dict(action.payload or {})
        source_items = list(source_payload.get("items") or [])
        if not any(item.get("action") == "delete" for item in source_items):
            return await original_execute(action)

        transformed_items: list[dict[str, Any]] = []
        for source in source_items:
            item = copy.deepcopy(source)
            if item.get("action") == "delete" and item.get("executable"):
                item["action"] = "update"
            transformed_items.append(item)

        shadow = copy.copy(action)
        shadow.payload = {
            **source_payload,
            "items": transformed_items,
        }
        result = await original_execute(shadow)

        result_data = dict(result.get("data") or {})
        item_results = dict(result_data.get("item_results") or {})
        source_payload["item_results"] = item_results
        action.payload = source_payload
        result_data["items"] = source_items
        result["data"] = result_data

        text = str(result.get("text") or "")
        for item in source_items:
            if item.get("action") != "delete":
                continue
            label = bulk._item_label(item)
            target = str(item.get("target_status_name") or "Игнор")
            text = text.replace(
                f"{html.escape(label)} — update",
                f"{html.escape(label)} — delete → {html.escape(target)}",
            )
        result["text"] = text[:4000]
        return result

    bulk._execute_bulk_json = execute_delete_as_ignore
