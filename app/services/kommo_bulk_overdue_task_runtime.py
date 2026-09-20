"""Extend /bulk_json with cleanup of overdue Kommo tasks.

Kommo's supported v4 API exposes task listing and task completion, but no
documented task hard-delete endpoint. Therefore action=delete_overdue_tasks
means: find incomplete tasks whose deadline is in the past and mark them
completed after the normal Telegram preview/approval flow.
"""

from __future__ import annotations

import copy
import html
import time
from typing import Any

from app.services import kommo_bulk_json_runtime as bulk, kommo_service

_INSTALLED = False
_ACTION = "delete_overdue_tasks"
_RESULT_TEXT = "Закрыто автоматически при очистке просроченных задач"


def _overdue_tasks(tasks: list[dict[str, Any]], *, now_ts: int | None = None) -> list[dict[str, Any]]:
    now = int(time.time()) if now_ts is None else int(now_ts)
    result: list[dict[str, Any]] = []
    for task in tasks:
        if task.get("is_completed"):
            continue
        task_id = task.get("id")
        due = task.get("complete_till")
        if not isinstance(task_id, int) or task_id <= 0:
            continue
        if not isinstance(due, int) or due <= 0 or due >= now:
            continue
        result.append(task)
    return result


async def _complete_tasks(task_ids: list[int], *, chunk_size: int = 50) -> int:
    """Complete reviewed overdue tasks through Kommo's supported PATCH API."""
    unique_ids = list(dict.fromkeys(int(task_id) for task_id in task_ids if int(task_id) > 0))
    completed = 0
    chunk_size = max(1, min(int(chunk_size), 50))
    for start in range(0, len(unique_ids), chunk_size):
        batch_ids = unique_ids[start : start + chunk_size]
        payload = [
            {
                "id": task_id,
                "is_completed": True,
                "result": {"text": _RESULT_TEXT},
            }
            for task_id in batch_ids
        ]
        await kommo_service._request("PATCH", "/api/v4/tasks", json_body=payload)
        completed += len(batch_ids)
    return completed


def install_kommo_bulk_overdue_task_runtime() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True

    bulk._ALLOWED_ACTIONS.add(_ACTION)

    original_validate = bulk.validate_bulk_payload

    def validate_with_overdue_cleanup(raw: Any) -> dict[str, Any]:
        if not isinstance(raw, dict):
            return original_validate(raw)
        source_actions = raw.get("actions")
        if not isinstance(source_actions, list):
            return original_validate(raw)

        transformed = copy.deepcopy(raw)
        for item in transformed.get("actions") or []:
            if isinstance(item, dict) and str(item.get("action") or "").strip().casefold() == _ACTION:
                item["action"] = "keep"

        normalized = original_validate(transformed)
        for index, source in enumerate(source_actions):
            if not isinstance(source, dict):
                continue
            if str(source.get("action") or "").strip().casefold() == _ACTION:
                normalized["actions"][index]["action"] = _ACTION
        return normalized

    bulk.validate_bulk_payload = validate_with_overdue_cleanup

    original_preview = bulk.build_bulk_preview

    async def preview_with_overdue_cleanup(payload: dict[str, Any]) -> dict[str, Any]:
        report = await original_preview(payload)
        total_tasks = 0

        for item in report.get("items") or []:
            if item.get("action") != _ACTION:
                continue
            lead_id = int(item.get("lead_id") or 0)
            if lead_id <= 0:
                item["executable"] = False
                continue

            tasks = await kommo_service.get_open_lead_tasks(lead_id, limit=50)
            overdue = _overdue_tasks(tasks)
            task_ids = [int(task["id"]) for task in overdue]
            item["overdue_task_ids"] = task_ids
            item["overdue_task_count"] = len(task_ids)
            item["executable"] = bool(task_ids)
            item["warnings"] = [] if task_ids else ["Просроченных незавершённых задач нет."]
            total_tasks += len(task_ids)

        report["overdue_task_count"] = total_tasks
        return report

    bulk.build_bulk_preview = preview_with_overdue_cleanup

    original_format = bulk.format_bulk_preview

    def format_with_overdue_cleanup(report: dict[str, Any]) -> str:
        text = original_format(report)
        items = [
            item
            for item in report.get("items") or []
            if item.get("action") == _ACTION
        ]
        if not items:
            return text

        lines = [
            "",
            f"🧹 Просроченных задач к закрытию: <b>{int(report.get('overdue_task_count') or 0)}</b>",
            "ℹ️ Kommo API не имеет поддерживаемого hard-delete задач: после подтверждения они будут отмечены выполненными.",
        ]
        for item in items[:20]:
            label = bulk._item_label(item)
            count = int(item.get("overdue_task_count") or 0)
            lines.append(f"• {html.escape(label)} — просроченных задач: <b>{count}</b>")
        if len(items) > 20:
            lines.append(f"…и ещё лидов: {len(items) - 20}")
        return (text + "\n" + "\n".join(lines))[:4000]

    bulk.format_bulk_preview = format_with_overdue_cleanup

    original_execute = bulk._execute_bulk_json

    async def execute_with_overdue_cleanup(action: Any) -> dict[str, Any]:
        payload = dict(action.payload or {})
        all_items = list(payload.get("items") or [])
        cleanup_items = [item for item in all_items if item.get("action") == _ACTION]
        other_items = [item for item in all_items if item.get("action") != _ACTION]
        if not cleanup_items:
            return await original_execute(action)

        combined_results = dict(payload.get("item_results") or {})
        result_lines: list[str] = []
        failed = 0

        if other_items:
            shadow = copy.copy(action)
            shadow.payload = {**payload, "items": other_items, "item_results": combined_results}
            other_result = await original_execute(shadow)
            combined_results.update((other_result.get("data") or {}).get("item_results") or {})
            for line in str(other_result.get("text") or "").splitlines():
                stripped = line.strip()
                if not stripped or "Результат Kommo Bulk JSON" in stripped or stripped.startswith("Успешно:"):
                    continue
                result_lines.append(line)
            if other_result.get("partial_failed"):
                failed += 1

        for item in cleanup_items:
            lead_id = int(item["lead_id"])
            key = str(lead_id)
            prior = dict(combined_results.get(key) or {})
            operations = dict(prior.get("operations") or {})
            if (operations.get("overdue_tasks") or {}).get("status") == "ok":
                continue

            task_ids = [int(value) for value in item.get("overdue_task_ids") or []]
            label = bulk._item_label(item)
            if not task_ids:
                operations["overdue_tasks"] = {"status": "ok", "completed": 0}
                combined_results[key] = {"status": "ok", "operations": operations}
                result_lines.append(f"⏭ {html.escape(label)} — просроченных задач нет")
                continue

            try:
                completed = await _complete_tasks(task_ids)
                operations["overdue_tasks"] = {
                    "status": "ok",
                    "completed": completed,
                    "task_ids": task_ids,
                }
                combined_results[key] = {"status": "ok", "operations": operations}
                result_lines.append(
                    f"✅ {html.escape(label)} — закрыто просроченных задач: <b>{completed}</b>"
                )
            except Exception as exc:
                failed += 1
                operations["overdue_tasks"] = {
                    "status": "failed",
                    "error": str(exc)[:500],
                    "task_ids": task_ids,
                }
                combined_results[key] = {"status": "failed", "operations": operations}
                result_lines.append(
                    f"❌ {html.escape(label)} — {html.escape(str(exc)[:200])}"
                )

        payload["item_results"] = combined_results
        action.payload = payload
        success = sum(
            1 for value in combined_results.values()
            if isinstance(value, dict) and value.get("status") == "ok"
        )
        skipped = sum(
            1 for value in combined_results.values()
            if isinstance(value, dict) and value.get("status") == "skipped"
        )
        visible = result_lines[:40]
        lines = ["<b>Результат Kommo Bulk JSON</b>", "", *visible]
        if len(result_lines) > 40:
            lines.append(f"…и ещё {len(result_lines) - 40}")
        lines.extend([
            "",
            f"Успешно: <b>{success}</b> · Ошибок: <b>{failed}</b> · Пропущено: <b>{skipped}</b>",
        ])
        return {
            "text": "\n".join(lines)[:4000],
            "data": {"item_results": combined_results, "items": all_items},
            "partial_failed": failed > 0,
            "error_message": "Часть просроченных задач не закрыта." if failed else None,
        }

    bulk._execute_bulk_json = execute_with_overdue_cleanup
