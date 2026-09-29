"""Safe Telegram /cleanup_tasks command for Kommo overdue tasks.

The command scans all open leads, previews incomplete tasks whose deadline is in
past, stages one approval action, and after approval completes only the task IDs
captured in that preview that are still overdue at execution time.

Kommo v4 has no supported task hard-delete endpoint, so cleanup means marking
those tasks completed.
"""

from __future__ import annotations

import asyncio
import html
import re
import time
from typing import Any

from app.agent import actions, executor, planner, service as agent_service
from app.agent.contracts import AgentPlan, AgentReply
from app.services import identity_service, kommo_service
from app.services import kommo_bulk_overdue_task_runtime as overdue_runtime

_INSTALLED = False
_COMMAND_RE = re.compile(r"(?i)^\s*/cleanup_tasks(?:@[A-Za-z0-9_]+)?\s*$")
_ACTION_TYPE = "cleanup_kommo_overdue_tasks"
_MAX_CONCURRENCY = 6


def _matches_command(text: str) -> bool:
    return bool(_COMMAND_RE.match(text or ""))


def _lead_label(item: dict[str, Any]) -> str:
    lead_id = int(item.get("lead_id") or 0)
    lead_name = " ".join(str(item.get("lead_name") or "").split()).strip()
    return f"Kommo ID {lead_id} · {lead_name or lead_id}"


async def build_cleanup_preview() -> dict[str, Any]:
    actor = identity_service.current_user()
    if actor is not None and actor.role not in {"owner", "admin"}:
        raise PermissionError("Очистка просроченных задач доступна только Owner и Admin.")

    open_result = await kommo_service.get_all_open_leads(allow_menu_fallback=False)
    leads = [
        lead
        for lead in (open_result.get("leads") or [])
        if isinstance(lead, dict) and isinstance(lead.get("id"), int)
    ]
    semaphore = asyncio.Semaphore(_MAX_CONCURRENCY)
    now_ts = int(time.time())

    async def inspect(lead: dict[str, Any]) -> dict[str, Any] | None:
        lead_id = int(lead["id"])
        async with semaphore:
            tasks = await kommo_service.get_open_lead_tasks(lead_id, limit=50)
        overdue = overdue_runtime._overdue_tasks(tasks, now_ts=now_ts)
        if not overdue:
            return None
        task_ids = [int(task["id"]) for task in overdue]
        return {
            "lead_id": lead_id,
            "lead_name": str(lead.get("name") or lead_id),
            "lead_url": lead.get("url"),
            "overdue_task_ids": task_ids,
            "overdue_task_count": len(task_ids),
        }

    raw_results = await asyncio.gather(
        *(inspect(lead) for lead in leads),
        return_exceptions=True,
    )
    items: list[dict[str, Any]] = []
    scan_errors: list[str] = []
    for lead, result in zip(leads, raw_results):
        if isinstance(result, Exception):
            scan_errors.append(
                f"Kommo ID {int(lead['id'])} · {str(lead.get('name') or lead['id'])}: {str(result)[:250]}"
            )
        elif result is not None:
            items.append(result)

    items.sort(key=lambda item: (str(item.get("lead_name") or "").casefold(), int(item["lead_id"])))
    return {
        "scanned_at": now_ts,
        "scanned_leads": len(leads),
        "items": items,
        "leads_with_overdue": len(items),
        "overdue_task_count": sum(int(item["overdue_task_count"]) for item in items),
        "scan_errors": scan_errors,
    }


def format_cleanup_preview(report: dict[str, Any]) -> str:
    items = list(report.get("items") or [])
    scan_errors = list(report.get("scan_errors") or [])
    lines = [
        "<b>🧹 Очистка просроченных задач — предпросмотр</b>",
        "",
        f"Проверено активных сделок: <b>{int(report.get('scanned_leads') or 0)}</b>",
        f"Лидов с просроченными задачами: <b>{int(report.get('leads_with_overdue') or 0)}</b>",
        f"Всего просроченных задач: <b>{int(report.get('overdue_task_count') or 0)}</b>",
    ]

    if items:
        lines.extend(["", "<b>Сделки</b>"])
        hidden = 0
        for index, item in enumerate(items):
            line = (
                f"• <b>{html.escape(_lead_label(item))}</b> — "
                f"просроченных задач: <b>{int(item.get('overdue_task_count') or 0)}</b>"
            )
            candidate = "\n".join([*lines, line])
            if len(candidate) > 3450:
                hidden = len(items) - index
                break
            lines.append(line)
        if hidden:
            lines.append(f"…и ещё лидов: <b>{hidden}</b>")

    if scan_errors:
        lines.extend(
            [
                "",
                f"⚠️ Не удалось проверить сделок: <b>{len(scan_errors)}</b>.",
                "Для безопасности выполнение заблокировано. Повторите команду после устранения ошибки Kommo.",
            ]
        )
        for error in scan_errors[:3]:
            lines.append(f"• {html.escape(error)}")
        return "\n".join(lines)[:4000]

    if not items:
        lines.extend(
            [
                "",
                "✅ <b>Просроченных задач нет.</b>",
                "Все незавершённые задачи имеют актуальный срок.",
            ]
        )
        return "\n".join(lines)[:4000]

    lines.extend(
        [
            "",
            "⚠️ После подтверждения перечисленные просроченные задачи будут отмечены выполненными.",
            "Новые задачи и задачи, срок которых изменится после этого предпросмотра, затронуты не будут.",
            "Сделки, этапы, бюджеты и поля Kommo не изменяются.",
            "",
            "Никаких изменений ещё не сделано.",
        ]
    )
    return "\n".join(lines)[:4000]


async def _stage_cleanup(
    db: Any,
    *,
    report: dict[str, Any],
    chat_id: int,
    telegram_user_id: int,
) -> AgentReply:
    preview = format_cleanup_preview(report)
    if report.get("scan_errors") or not int(report.get("overdue_task_count") or 0):
        return AgentReply(
            preview,
            intent="kommo_cleanup_tasks_preview",
            metadata=report,
        )

    action = await actions.stage_action(
        db,
        telegram_user_id=telegram_user_id,
        chat_id=chat_id,
        action_type=_ACTION_TYPE,
        payload=report,
        preview_text=preview,
    )
    markup = actions.approval_markup(action.id)
    markup["inline_keyboard"][0][0]["text"] = "✅ Закрыть все просроченные"
    return AgentReply(
        preview,
        reply_markup=markup,
        intent="kommo_cleanup_tasks",
        metadata={"action_id": int(action.id), **report},
    )


async def _execute_cleanup(action: Any) -> dict[str, Any]:
    payload = dict(action.payload or {})
    items = list(payload.get("items") or [])
    item_results: dict[str, Any] = {}
    completed_total = 0
    skipped_total = 0
    failed_total = 0
    completed_leads = 0
    result_lines: list[str] = []
    now_ts = int(time.time())

    for item in items:
        lead_id = int(item.get("lead_id") or 0)
        label = _lead_label(item)
        snapshot_ids = {
            int(task_id)
            for task_id in (item.get("overdue_task_ids") or [])
            if isinstance(task_id, int) and int(task_id) > 0
        }
        try:
            current_tasks = await kommo_service.get_open_lead_tasks(lead_id, limit=50)
            current_overdue = overdue_runtime._overdue_tasks(current_tasks, now_ts=now_ts)
            current_overdue_ids = {
                int(task["id"])
                for task in current_overdue
                if isinstance(task.get("id"), int)
            }
            valid_ids = sorted(snapshot_ids & current_overdue_ids)
            skipped = len(snapshot_ids - current_overdue_ids)
            completed = 0
            if valid_ids:
                completed = await overdue_runtime._complete_tasks(valid_ids)
                completed_total += completed
                completed_leads += 1
            skipped_total += skipped
            item_results[str(lead_id)] = {
                "status": "ok",
                "completed": completed,
                "skipped": skipped,
                "task_ids": valid_ids,
            }
            if completed or skipped:
                result_lines.append(
                    f"✅ {html.escape(label)} — закрыто: <b>{completed}</b>"
                    + (f" · пропущено после перепроверки: <b>{skipped}</b>" if skipped else "")
                )
        except Exception as exc:
            failed_total += 1
            item_results[str(lead_id)] = {
                "status": "failed",
                "error": str(exc)[:500],
                "task_ids": sorted(snapshot_ids),
            }
            result_lines.append(f"❌ {html.escape(label)} — {html.escape(str(exc)[:220])}")

    payload["item_results"] = item_results
    action.payload = payload

    visible = result_lines[:25]
    lines = [
        "<b>✅ Очистка задач завершена</b>" if not failed_total else "<b>⚠️ Очистка задач завершена частично</b>",
        "",
        f"Закрыто задач: <b>{completed_total}</b>",
        f"Обработано сделок: <b>{completed_leads}</b>",
        f"Пропущено после перепроверки: <b>{skipped_total}</b>",
        f"Ошибок по сделкам: <b>{failed_total}</b>",
    ]
    if visible:
        lines.extend(["", *visible])
    if len(result_lines) > len(visible):
        lines.append(f"…и ещё результатов: {len(result_lines) - len(visible)}")

    return {
        "text": "\n".join(lines)[:4000],
        "data": {
            "item_results": item_results,
            "completed_tasks": completed_total,
            "skipped_tasks": skipped_total,
            "completed_leads": completed_leads,
        },
        "partial_failed": failed_total > 0,
        "error_message": "Часть просроченных задач не закрыта." if failed_total else None,
    }


def install_kommo_cleanup_tasks_runtime() -> None:
    """Install /cleanup_tasks planning, preview/approval, and execution."""

    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True

    original_deterministic_plan = planner.deterministic_plan

    def deterministic_plan_with_cleanup(
        text: str, context: dict[str, Any]
    ) -> AgentPlan | None:
        if _matches_command(text):
            return AgentPlan(
                intent="kommo_cleanup_tasks",
                mode="write",
                confidence=1.0,
            )
        return original_deterministic_plan(text, context)

    planner.deterministic_plan = deterministic_plan_with_cleanup

    original_execute_plan = agent_service._execute_plan

    async def execute_plan_with_cleanup(
        db: Any,
        *,
        plan: AgentPlan,
        text: str,
        chat_id: int,
        telegram_user_id: int,
        source: str,
        context: dict[str, Any],
        session: Any,
        pre_resolved_leads: list[dict[str, Any]] | None = None,
    ) -> AgentReply:
        if plan.intent != "kommo_cleanup_tasks":
            return await original_execute_plan(
                db,
                plan=plan,
                text=text,
                chat_id=chat_id,
                telegram_user_id=telegram_user_id,
                source=source,
                context=context,
                session=session,
                pre_resolved_leads=pre_resolved_leads,
            )
        actor = identity_service.current_user()
        if actor is not None and actor.role not in {"owner", "admin"}:
            return AgentReply(
                "🔒 Очистка просроченных задач доступна только Owner и Admin.",
                intent="permission_denied",
            )
        try:
            report = await build_cleanup_preview()
            return await _stage_cleanup(
                db,
                report=report,
                chat_id=chat_id,
                telegram_user_id=telegram_user_id,
            )
        except Exception as exc:
            return AgentReply(
                "❌ <b>Не удалось проверить просроченные задачи</b>\n\n"
                f"<code>{html.escape(str(exc)[:900])}</code>",
                intent="kommo_cleanup_tasks_failed",
            )

    agent_service._execute_plan = execute_plan_with_cleanup

    original_execute = executor._execute

    async def execute_with_cleanup(db: Any, action: Any) -> dict[str, Any]:
        if action.action_type == _ACTION_TYPE:
            return await _execute_cleanup(action)
        return await original_execute(db, action)

    executor._execute = execute_with_cleanup
