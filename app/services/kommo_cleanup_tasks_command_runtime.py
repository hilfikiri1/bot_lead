"""One-command cleanup of overdue Kommo tasks across all open leads."""

from __future__ import annotations

import html
from typing import Any

from app.agent import planner
from app.agent.contracts import AgentPlan, AgentReply
from app.agent import service as agent_service
from app.services import identity_service, kommo_bulk_json_runtime as bulk, kommo_service

_INSTALLED = False
_COMMANDS = {"/cleanup_tasks", "/cleanup_tasks@bbs_poland_bot"}


def _is_cleanup_command(text: str) -> bool:
    token = (text or "").strip().split(maxsplit=1)[0].casefold()
    if token == "/cleanup_tasks":
        return True
    return token.startswith("/cleanup_tasks@")


async def _build_cleanup_payload() -> dict[str, Any]:
    result = await kommo_service.get_all_open_leads()
    leads = list((result or {}).get("leads") or [])
    seen: set[int] = set()
    actions: list[dict[str, Any]] = []
    for lead in leads:
        lead_id = lead.get("id")
        if not isinstance(lead_id, int) or lead_id <= 0 or lead_id in seen:
            continue
        seen.add(lead_id)
        actions.append(
            {
                "lead_ref": {"kommo_id": lead_id},
                "action": "delete_overdue_tasks",
            }
        )
    return {"version": 1, "actions": actions}


def install_kommo_cleanup_tasks_command_runtime() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True

    original_deterministic_plan = planner.deterministic_plan

    def deterministic_plan_with_cleanup_tasks(
        text: str, context: dict[str, Any]
    ) -> AgentPlan | None:
        if _is_cleanup_command(text):
            return AgentPlan(
                intent="kommo_cleanup_tasks",
                mode="write",
                confidence=1.0,
            )
        return original_deterministic_plan(text, context)

    planner.deterministic_plan = deterministic_plan_with_cleanup_tasks

    original_execute_plan = agent_service._execute_plan

    async def execute_plan_with_cleanup_tasks(
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
                "🔒 Очистка задач Kommo доступна только Owner и Admin.",
                intent="permission_denied",
            )

        try:
            payload = await _build_cleanup_payload()
            count = len(payload["actions"])
            if not count:
                return AgentReply(
                    "✅ Открытых сделок для проверки нет.",
                    intent="kommo_cleanup_tasks_empty",
                )
            if count > bulk._MAX_ACTIONS:
                return AgentReply(
                    f"❌ Открытых сделок: <b>{count}</b>, а один безопасный пакет поддерживает максимум "
                    f"<b>{bulk._MAX_ACTIONS}</b>. Сначала нужно добавить постраничную очистку.",
                    intent="kommo_cleanup_tasks_too_many",
                )

            parsed = bulk.validate_bulk_payload(payload)
            report = await bulk.build_bulk_preview(parsed)
            reply = await bulk._stage_report(
                db,
                report=report,
                chat_id=chat_id,
                telegram_user_id=telegram_user_id,
            )
            total = int(report.get("overdue_task_count") or 0)
            affected = sum(
                1
                for item in report.get("items") or []
                if int(item.get("overdue_task_count") or 0) > 0
            )
            prefix = (
                "<b>🧹 Очистка просроченных задач</b>\n"
                f"Проверено открытых сделок: <b>{count}</b>\n"
                f"Сделок с просроченными задачами: <b>{affected}</b>\n"
                f"Просроченных задач найдено: <b>{total}</b>\n\n"
            )
            reply.text = (prefix + reply.text)[:4000]
            return reply
        except Exception as exc:
            return AgentReply(
                "❌ <b>Не удалось подготовить очистку задач</b>\n\n"
                f"<code>{html.escape(str(exc)[:900])}</code>",
                intent="kommo_cleanup_tasks_failed",
            )

    agent_service._execute_plan = execute_plan_with_cleanup_tasks
