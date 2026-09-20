"""Extend Kommo bulk JSON with deterministic task creation."""

from __future__ import annotations

import copy
import html
from typing import Any

from app.services import calendar_event_builder, kommo_bulk_json_runtime as bulk, kommo_service

_INSTALLED = False


def _normalize_task(raw: Any, index: int) -> dict[str, str]:
    if not isinstance(raw, dict):
        raise ValueError(f"actions[{index}].task должен быть объектом.")
    text = " ".join(str(raw.get("text") or "").split()).strip()
    due_at = " ".join(str(raw.get("due_at") or "").split()).strip()
    unknown = set(raw) - {"text", "due_at"}
    if unknown:
        raise ValueError(
            f"actions[{index}].task: неизвестные поля: {', '.join(sorted(unknown))}."
        )
    if not text:
        raise ValueError(f"actions[{index}].task.text не может быть пустым.")
    if len(text) > 1000:
        raise ValueError(f"actions[{index}].task.text длиннее 1000 символов.")
    if not due_at:
        raise ValueError(f"actions[{index}].task.due_at не может быть пустым.")
    try:
        calendar_event_builder.parse_natural_datetime(
            due_at,
            duration_minutes=30,
        )
    except ValueError as exc:
        raise ValueError(f"actions[{index}].task.due_at: {exc}") from exc
    return {"text": text, "due_at": due_at}


def install_kommo_bulk_task_runtime() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True

    bulk._ALLOWED_ACTIONS.add("create_task")

    original_validate = bulk.validate_bulk_payload

    def validate_with_tasks(raw: Any) -> dict[str, Any]:
        if not isinstance(raw, dict):
            return original_validate(raw)
        source_actions = raw.get("actions")
        if not isinstance(source_actions, list):
            return original_validate(raw)

        transformed = copy.deepcopy(raw)
        for item in transformed.get("actions") or []:
            if isinstance(item, dict) and str(item.get("action") or "").casefold() == "create_task":
                item["action"] = "keep"
                item.pop("task", None)

        normalized = original_validate(transformed)
        for index, source in enumerate(source_actions):
            if not isinstance(source, dict):
                continue
            action = str(source.get("action") or "").strip().casefold()
            if action == "create_task":
                task = _normalize_task(source.get("task"), index)
                normalized["actions"][index]["action"] = "create_task"
                normalized["actions"][index]["task"] = task
            elif source.get("task") not in (None, "", {}):
                raise ValueError(
                    f"actions[{index}]: task разрешён только для action=create_task."
                )
        return normalized

    bulk.validate_bulk_payload = validate_with_tasks

    original_preview = bulk.build_bulk_preview

    async def preview_with_tasks(payload: dict[str, Any]) -> dict[str, Any]:
        report = await original_preview(payload)
        commands = list(payload.get("actions") or [])
        for item in report.get("items") or []:
            idx = int(item.get("source_index") or 0) - 1
            if idx < 0 or idx >= len(commands):
                continue
            command = commands[idx]
            if str(command.get("action") or "") != "create_task":
                continue
            task = dict(command.get("task") or {})
            item["task"] = task
            item["executable"] = bool(task.get("text") and task.get("due_at"))
        return report

    bulk.build_bulk_preview = preview_with_tasks

    original_format = bulk.format_bulk_preview

    def format_with_tasks(report: dict[str, Any]) -> str:
        text = original_format(report)
        tasks = [
            item
            for item in report.get("items") or []
            if item.get("action") == "create_task" and item.get("task")
        ]
        if not tasks:
            return text
        suffix = f"\n\n📞 Задач Kommo к созданию: <b>{len(tasks)}</b>"
        return (text + suffix)[:4000]

    bulk.format_bulk_preview = format_with_tasks

    original_execute = bulk._execute_bulk_json

    async def execute_with_tasks(action: Any) -> dict[str, Any]:
        payload = dict(action.payload or {})
        all_items = list(payload.get("items") or [])
        task_items = [item for item in all_items if item.get("action") == "create_task"]
        other_items = [item for item in all_items if item.get("action") != "create_task"]

        combined_results = dict(payload.get("item_results") or {})
        lines: list[str] = []
        failed = 0

        if other_items:
            shadow = copy.copy(action)
            shadow.payload = {**payload, "items": other_items, "item_results": combined_results}
            result = await original_execute(shadow)
            combined_results.update((result.get("data") or {}).get("item_results") or {})
            if result.get("partial_failed"):
                failed += 1

        for item in task_items:
            lead_id = int(item["lead_id"])
            key = str(lead_id)
            prior = dict(combined_results.get(key) or {})
            operations = dict(prior.get("operations") or {})
            if (operations.get("task") or {}).get("status") == "ok":
                continue
            task = dict(item.get("task") or {})
            try:
                start_at, _ = calendar_event_builder.parse_natural_datetime(
                    str(task.get("due_at") or ""),
                    duration_minutes=30,
                )
                result = await kommo_service.create_lead_task(
                    lead_id=lead_id,
                    text=str(task.get("text") or "Связаться с клиентом")[:1000],
                    complete_till=int(start_at.timestamp()),
                )
                operations["task"] = {"status": "ok", "result": result}
                combined_results[key] = {
                    "status": "ok",
                    "operations": operations,
                }
                label = item.get("internal_lead_number")
                ref = f"№{label}" if label else f"Kommo ID {lead_id}"
                lines.append(
                    f"✅ {html.escape(ref)} — задача создана до "
                    f"{html.escape(str(task.get('due_at') or ''))}"
                )
            except Exception as exc:
                failed += 1
                operations["task"] = {"status": "failed", "error": str(exc)[:500]}
                combined_results[key] = {
                    "status": "failed",
                    "operations": operations,
                }
                label = item.get("internal_lead_number")
                ref = f"№{label}" if label else f"Kommo ID {lead_id}"
                lines.append(
                    f"❌ {html.escape(ref)} — {html.escape(str(exc)[:200])}"
                )

        payload["item_results"] = combined_results
        action.payload = payload

        success = sum(
            1
            for item in combined_results.values()
            if isinstance(item, dict) and item.get("status") == "ok"
        )
        skipped = sum(
            1
            for item in combined_results.values()
            if isinstance(item, dict) and item.get("status") == "skipped"
        )
        result_lines = ["<b>Результат Kommo Bulk JSON</b>", ""]
        result_lines.extend(lines[:40])
        if len(lines) > 40:
            result_lines.append(f"…и ещё {len(lines) - 40}")
        result_lines.extend(
            [
                "",
                f"Успешно: <b>{success}</b> · Ошибок: <b>{failed}</b> · "
                f"Пропущено: <b>{skipped}</b>",
            ]
        )
        return {
            "text": "\n".join(result_lines)[:4000],
            "data": {"item_results": combined_results, "items": all_items},
            "partial_failed": failed > 0,
            "error_message": "Часть лидов не обработана." if failed else None,
        }

    bulk._execute_bulk_json = execute_with_tasks
