"""Goals, planning, client analysis and QA intake runtime.

Installed after existing production runtimes. It intercepts only explicit goals/QA
commands and conservative natural phrases; everything else stays on the canonical
agent planner/service path.
"""
from __future__ import annotations

import html
import os
import re
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm.attributes import flag_modified

from app.agent import actions, executor, memory, tools
from app.agent.contracts import AgentPlan, AgentReply
from app.models.goal_qa import QAIssue
from app.models.kaizen_journal_entry import KaizenJournalEntry
from app.services import (
    goals_qa_service,
    kommo_service,
    next_action_service,
    unified_communication_service,
    unified_project_service,
)
from app.services import kaizen_runtime

_INSTALLED = False


def _normal(value: str | None) -> str:
    return " ".join(str(value or "").strip().casefold().replace("ё", "е").split()).strip(" ?!.,;:")


def _command_arg(text: str, command: str) -> str:
    return re.sub(rf"^\s*/{re.escape(command)}(?:@\w+)?\s*", "", str(text or ""), count=1, flags=re.I).strip()


def _context_project(context: dict[str, Any]) -> tuple[str | None, int | None]:
    internal = context.get("active_internal_lead_number")
    lead_id = context.get("active_kommo_lead_id") or context.get("kommo_lead_id")
    try:
        parsed = int(lead_id) if lead_id else None
    except (TypeError, ValueError):
        parsed = None
    return (str(internal) if internal else None, parsed)


async def _remember(db: Any, *, session: Any, user_text: str, source: str, reply: AgentReply) -> AgentReply:
    await memory.remember_message(
        db, session=session, role="user", content=user_text, source=source, intent=reply.intent
    )
    if reply.text:
        await memory.remember_message(
            db,
            session=session,
            role="assistant",
            content=reply.text,
            source="agent",
            intent=reply.intent,
            metadata=reply.metadata,
        )
    return reply


def _qa_type_from_command(text: str) -> str | None:
    normalized = _normal(text)
    if normalized.startswith("/bug") or normalized.startswith(("я нашел баг", "я нашёл баг", "нашел баг", "нашёл баг")):
        return "Bug"
    if normalized.startswith("/idea") or normalized.startswith(("запиши идею", "добавь идею")):
        return "Improvement"
    if normalized.startswith("/concern") or normalized.startswith(("у меня есть опасение", "зафиксируй риск")):
        return "Concern"
    if normalized.startswith("/feedback"):
        return None
    if any(phrase in normalized for phrase in ("добавь баг", "сохрани ошибку", "зафиксируй ошибку", "эта кнопка не работает", "вот скриншот проблемы")):
        return "Bug"
    return None


def _qa_body(text: str) -> str:
    raw = str(text or "").strip()
    raw = re.sub(r"^\s*/(?:bug|idea|concern|feedback)(?:@\w+)?\s*", "", raw, flags=re.I)
    raw = re.sub(
        r"(?i)^\s*(?:я\s+)?(?:наш[её]л\s+)?(?:баг|ошибк[ау]?|иде[яю]|опасение|feedback)\s*[:—-]?\s*",
        "",
        raw,
    )
    return raw.strip()


def _explicit_save(text: str) -> bool:
    normalized = _normal(text)
    return str(text or "").lstrip().startswith("/") and bool(_qa_body(text)) or any(
        phrase in normalized
        for phrase in (
            "добавь баг",
            "добавь это как баг",
            "сохрани ошибку",
            "зафиксируй ошибку",
            "добавь идею",
            "запиши идею",
            "зафиксируй риск",
            "добавь в notion",
        )
    )


async def _start_qa_intake(db: Any, *, session: Any, requested_type: str | None) -> AgentReply:
    await memory.update_context(
        db,
        session=session,
        values={
            "qa_intake": {
                "issue_type": requested_type,
                "started_at": datetime.now(timezone.utc).isoformat(),
            }
        },
    )
    label = requested_type or "Bug / Improvement / UX / Concern / Question"
    return AgentReply(
        "🧪 <b>Режим фиксации проблемы</b>\n\n"
        f"Тип: {html.escape(label)}\n"
        "Отправь одним или несколькими сообщениями описание, голос, скриншот, видео, документ или JSON диагностики. "
        "Текст и голос не будут обработаны как новый клиент.\n\n"
        "Опиши: что сделал, что ожидал и что получилось фактически.",
        intent="qa_intake_started",
    )


async def _stage_or_sync_issue(
    db: Any,
    *,
    issue: QAIssue,
    chat_id: int,
    telegram_user_id: int,
    explicit: bool,
) -> AgentReply:
    preview = (
        "<b>Сохранить QA-карточку в Notion?</b>\n\n"
        + goals_qa_service.format_issue(issue)
        + "\n\nЛокальная запись уже сохранена. Kommo, WhatsApp и клиентские данные не изменяются."
    )
    action = await actions.stage_action(
        db,
        telegram_user_id=int(telegram_user_id),
        chat_id=int(chat_id or telegram_user_id),
        action_type="sync_qa_issue_to_notion",
        payload={"issue_id": int(issue.id), "telegram_user_id": int(telegram_user_id)},
        preview_text=preview,
    )
    if explicit:
        try:
            result_text = await executor.execute_action(
                db, action=action, telegram_user_id=int(telegram_user_id)
            )
            return AgentReply(
                goals_qa_service.format_issue(issue) + "\n\n" + result_text,
                intent="qa_issue_saved",
                metadata={"issue_id": int(issue.id), "action_id": int(action.id)},
            )
        except Exception as exc:
            return AgentReply(
                goals_qa_service.format_issue(issue)
                + "\n\n⚠️ Notion сейчас недоступен. Локальная запись сохранена и может быть синхронизирована повторно.\n"
                + html.escape(goals_qa_service.redact_sensitive(str(exc))[:500]),
                intent="qa_issue_saved_local",
                metadata={"issue_id": int(issue.id), "action_id": int(action.id)},
            )
    return AgentReply(
        preview,
        reply_markup=actions.approval_markup(action.id),
        intent="qa_issue_preview",
        metadata={"issue_id": int(issue.id), "action_id": int(action.id)},
    )


async def _capture_issue(
    db: Any,
    *,
    session: Any,
    context: dict[str, Any],
    text: str,
    source: str,
    requested_type: str | None,
    chat_id: int,
    telegram_user_id: int,
    explicit: bool,
) -> AgentReply:
    project_number, lead_id = _context_project(context)
    trace_id = context.get("last_trace_id") or context.get("trace_id")
    issue, duplicate = await goals_qa_service.create_local_issue(
        db,
        telegram_user_id=int(telegram_user_id),
        text=text,
        issue_type=requested_type,
        active_project_number=project_number,
        kommo_lead_id=lead_id,
        trace_id=str(trace_id or "") or None,
        source=source,
        metadata={"active_lead_name": context.get("active_lead_name")},
    )
    await memory.update_context(
        db,
        session=session,
        values={"qa_intake": None, "active_qa_issue_id": int(issue.id)},
    )
    if duplicate is not None:
        return AgentReply(
            "⚠️ Похожая открытая проблема уже зарегистрирована.\n\n"
            + goals_qa_service.format_issue(duplicate)
            + "\n\nДобавь новые детали командой <code>/bug_add "
            + html.escape(duplicate.issue_code or str(duplicate.id))
            + " текст</code> или создай отдельную карточку командой <code>/bug_new ...</code>.",
            intent="qa_duplicate_found",
            metadata={"issue_id": int(duplicate.id)},
        )
    return await _stage_or_sync_issue(
        db,
        issue=issue,
        chat_id=chat_id,
        telegram_user_id=telegram_user_id,
        explicit=explicit,
    )


async def _focus_reply(db: Any) -> AgentReply:
    inbox = await next_action_service.build_inbox(db)
    candidates = list(inbox.overdue) + list(inbox.waiting_us) + list(inbox.without_next) + list(inbox.stale)
    if not candidates:
        return AgentReply(
            "✅ Срочных операционных блокеров не найдено. Главный фокус — выполнить ближайшую цель месяца или одну задачу глубокой работы.",
            intent="focus",
        )
    item = candidates[0]
    action = item.recommended_action or item.action_text or "Определить и выполнить следующий шаг"
    reason = item.action_reason or item.stale_reason or "Проект требует действия менеджера."
    label = getattr(item, "name", None) or getattr(item, "lead_name", None) or str(item.kommo_lead_id)
    return AgentReply(
        "<b>🎯 Главный фокус сейчас</b>\n\n"
        f"Проект: {html.escape(str(label))}\n"
        f"Первое действие: <b>{html.escape(str(action))}</b>\n"
        f"Почему: {html.escape(str(reason))}\n"
        "Время: 45–90 минут без переключений.\n"
        "Сознательно отложить: входящие, которые не блокируют клиента или дедлайн.",
        intent="focus",
        metadata={"lead_id": int(item.kommo_lead_id)},
    )


def _when_reply(text: str) -> AgentReply:
    normalized = _normal(text)
    warsaw = datetime.now(ZoneInfo(os.getenv("MANAGER_TIMEZONE", "Europe/Warsaw")))
    if any(token in normalized for token in ("фабрик", "китай", "поставщик", "производител")):
        best = "05:00–07:00 по Варшаве"
        reason = "В Китае уже рабочий день, поэтому выше вероятность получить ответ в тот же день."
        duration = "30–60 минут"
        backup = "Следующий рабочий день 05:00–07:00"
    elif any(token in normalized for token in ("позвон", "клиент", "follow-up", "написать")):
        best = "09:30–11:30 по часовому поясу клиента"
        reason = "У клиента уже начался рабочий день, но ещё не накопилась дневная загрузка."
        duration = "15–30 минут"
        backup = "14:00–16:00 по часовому поясу клиента"
    elif any(token in normalized for token in ("предложен", "кп", "анализ", "сравн", "документ")):
        best = "Первый свободный блок 90–120 минут до обеда"
        reason = "Сложная работа требует непрерывного внимания и лучше выполняется до потока входящих."
        duration = "90–120 минут"
        backup = "Завтра 08:30–10:30"
    else:
        best = warsaw.replace(minute=0, second=0, microsecond=0).strftime("сегодня после %H:00")
        reason = "Точный слот зависит от типа задачи, дедлайна и часового пояса второй стороны."
        duration = "30–60 минут"
        backup = "Ближайший свободный блок календаря"
    return AgentReply(
        "<b>⏰ Рекомендуемое время</b>\n\n"
        f"Лучшее время: <b>{best}</b>\n"
        f"Почему: {reason}\n"
        f"Ожидаемая длительность: {duration}\n"
        f"Запасной вариант: {backup}\n\n"
        "Событие в календаре не создавалось.",
        intent="when",
    )


async def _resolve_lead(text: str, context: dict[str, Any], intent: str) -> dict[str, Any]:
    query = text.strip()
    active = context.get("active_kommo_lead_id") or context.get("kommo_lead_id")
    if not query and active:
        return await kommo_service.get_lead_details(int(active))
    plan = AgentPlan(intent=intent, mode="read", query=query or None, lead_id=int(active) if active and not query else None)
    return await tools.resolve_lead(lead_id=plan.lead_id, query=plan.query, context=context, plan=plan)


async def _communication_analysis(db: Any, *, lead: dict[str, Any], broad: bool) -> AgentReply:
    timeline = await unified_communication_service.build_unified_timeline(db, lead_id=int(lead["id"]))
    analysis = timeline.analysis
    incoming = [entry for entry in timeline.entries if entry.direction == "incoming"][-5:]
    outgoing = [entry for entry in timeline.entries if entry.direction == "outgoing"][-5:]
    lines = [
        f"<b>{'🧠 Анализ клиента' if broad else '💬 Анализ WhatsApp/переписки'} · {html.escape(str(lead.get('name') or lead['id']))}</b>",
        "",
        f"Стадия переговоров: {html.escape(str(lead.get('status_name') or lead.get('status') or 'не определена'))}",
        f"Сейчас ждём: {'наше действие' if analysis.waiting_on == 'us' else 'ответ клиента' if analysis.waiting_on == 'client' else 'не определено'}",
        f"Основной канал: {html.escape(str(analysis.last_channel or 'не определён'))}",
        f"Риск потери: {'высокий' if analysis.overdue_promises else 'средний' if analysis.waiting_on == 'us' else 'не подтверждён'}",
    ]
    if analysis.client_requests:
        lines.extend(["", "<b>Вопросы и потребности клиента</b>"])
        lines.extend(f"• {html.escape(item[:500])}" for item in analysis.client_requests[-5:])
    if analysis.promises_by_us:
        lines.extend(["", "<b>Что мы обещали</b>"])
        lines.extend(f"• {html.escape(item.text[:500])}" for item in analysis.promises_by_us[-5:])
    if incoming:
        lines.extend(["", "<b>Последние сообщения клиента</b>"])
        for item in incoming:
            lines.append(f"• {item.occurred_at.astimezone().strftime('%d.%m %H:%M')} — {html.escape(item.text[:450])}")
    if broad and outgoing:
        lines.extend(["", "<b>Наши последние сообщения</b>"])
        for item in outgoing:
            lines.append(f"• {item.occurred_at.astimezone().strftime('%d.%m %H:%M')} — {html.escape(item.text[:350])}")
    lines.extend(["", f"<b>Следующий лучший шаг:</b> {html.escape(str(analysis.recommended_action or 'Уточнить следующий шаг'))}"])
    if analysis.last_client_message:
        lines.extend(["", "<b>Черновик направления ответа</b>", "Ответить на последний вопрос клиента, подтвердить конкретное действие и назвать срок следующего контакта."])
    if timeline.source_errors:
        lines.extend(["", "⚠️ История неполная. Недоступные источники: " + html.escape(", ".join(timeline.source_errors))])
    return AgentReply(
        "\n".join(lines)[:3900],
        intent="client_analysis" if broad else "whatsapp_analysis",
        metadata={"lead_id": int(lead["id"]), "messages": len(timeline.entries), "source_errors": timeline.source_errors},
    )


async def _pipeline_health(db: Any) -> AgentReply:
    inbox = await next_action_service.build_inbox(db)
    counts = {
        "Просрочено": len(inbox.overdue),
        "Ждут нас": len(inbox.waiting_us),
        "Ждём клиента": len(inbox.waiting_client),
        "Без шага": len(inbox.without_next),
        "Без движения": len(inbox.stale),
    }
    score = max(0, 100 - counts["Просрочено"] * 12 - counts["Ждут нас"] * 8 - counts["Без шага"] * 5)
    lines = ["<b>📈 Здоровье воронки</b>", "", f"Операционный индекс: <b>{score}/100</b>"]
    lines.extend(f"• {name}: {value}" for name, value in counts.items())
    first = (list(inbox.overdue) + list(inbox.waiting_us) + list(inbox.without_next))[:3]
    if first:
        lines.extend(["", "<b>Что исправить первым</b>"])
        for item in first:
            lines.append(f"• {html.escape(str(item.recommended_action or item.action_text or item.stale_reason or item.kommo_lead_id))}")
    return AgentReply("\n".join(lines), intent="pipeline_health", metadata=counts)


async def _day_results(db: Any, *, telegram_user_id: int) -> AgentReply:
    today = datetime.now(ZoneInfo(os.getenv("MANAGER_TIMEZONE", "Europe/Warsaw"))).date()
    entry = (
        await db.execute(
            select(KaizenJournalEntry).where(
                KaizenJournalEntry.telegram_user_id == int(telegram_user_id),
                KaizenJournalEntry.entry_type == "daily",
                KaizenJournalEntry.period_start == today,
            )
        )
    ).scalar_one_or_none()
    lines = ["<b>✅ Итоги дня</b>", today.strftime("%d.%m.%Y")]
    if entry and entry.raw_text:
        lines.extend(["", "<b>Зафиксировано в дневнике</b>", html.escape(entry.raw_text[:2500])])
        analysis = dict(entry.analysis or {})
        for label, key in (("Что получилось", "wins"), ("Что мешало", "obstacles"), ("Фокус на завтра", "tomorrow_focus")):
            values = analysis.get(key) or []
            if isinstance(values, str):
                values = [values]
            if values:
                lines.extend(["", f"<b>{label}</b>"])
                lines.extend(f"• {html.escape(str(value)[:400])}" for value in values[:5])
    else:
        lines.extend(["", "Сегодняшняя рефлексия ещё не заполнена. Команда: /evening"])
    inbox = await next_action_service.build_inbox(db)
    if inbox.overdue or inbox.waiting_us:
        lines.extend(["", f"Незакрыто на конец дня: просрочено {len(inbox.overdue)}, ждут нас {len(inbox.waiting_us)}."])
    return AgentReply("\n".join(lines)[:3900], intent="day_results")


def _goal_capture(text: str) -> str | None:
    raw = str(text or "").strip()
    patterns = (
        r"(?i)^\s*/goal\s+(.+)$",
        r"(?i)^\s*(?:добавь|запиши|создай)\s+цель(?:\s+на\s+месяц|\s+месяца)?\s*[:—-]?\s*(.+)$",
    )
    for pattern in patterns:
        match = re.match(pattern, raw)
        if match and match.group(1).strip():
            return match.group(1).strip()
    return None


async def _execute_goal_action(db: Any, action: Any) -> dict[str, Any]:
    payload = dict(action.payload or {})
    if int(payload.get("telegram_user_id") or 0) != int(action.telegram_user_id):
        raise PermissionError("Goal action owner mismatch.")
    goal = await goals_qa_service.create_goal(
        db,
        telegram_user_id=int(action.telegram_user_id),
        title=str(payload.get("title") or "Цель"),
        goal_type=str(payload.get("goal_type") or "month"),
        target_value=Decimal(str(payload["target_value"])) if payload.get("target_value") is not None else None,
        metric_name=str(payload.get("metric_name") or "") or None,
    )
    return {
        "text": f"✅ Цель сохранена: {html.escape(goal.title)}",
        "data": {"goal_id": int(goal.id), "external_id": goal.external_id},
    }


async def _execute_qa_sync(db: Any, action: Any) -> dict[str, Any]:
    payload = dict(action.payload or {})
    issue = await goals_qa_service.get_issue(
        db,
        telegram_user_id=int(action.telegram_user_id),
        issue_ref=int(payload["issue_id"]),
    )
    if issue is None:
        raise ValueError("QA-карточка не найдена.")
    await goals_qa_service.sync_issue_to_notion(db, issue)
    return {
        "text": f"✅ {html.escape(issue.issue_code or str(issue.id))} сохранён в Notion.",
        "data": {"issue_id": int(issue.id), "notion_page_id": issue.notion_page_id, "notion_url": issue.notion_url},
    }


async def _execute_qa_close(db: Any, action: Any) -> dict[str, Any]:
    payload = dict(action.payload or {})
    issue = await goals_qa_service.get_issue(db, telegram_user_id=int(action.telegram_user_id), issue_ref=int(payload["issue_id"]))
    if issue is None:
        raise ValueError("QA-карточка не найдена.")
    issue.status = "Closed"
    issue.retest_result = str(payload.get("retest_result") or "Исправлено")
    issue.retested_at = datetime.now(timezone.utc)
    await db.commit()
    try:
        if os.getenv("NOTION_QA_DATA_SOURCE_ID", "").strip():
            await goals_qa_service.sync_issue_to_notion(db, issue)
    except Exception:
        pass
    return {"text": f"✅ {html.escape(issue.issue_code or str(issue.id))} закрыт после подтверждения.", "data": {"issue_id": int(issue.id)}}


def install_goals_qa_runtime() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True

    agent_service = __import__("app.agent.service", fromlist=["handle_message"])
    original_handle = agent_service.handle_message
    original_execute = executor._execute

    async def execute_with_goals_qa(db: Any, action: Any) -> dict[str, Any]:
        if action.action_type == "sync_qa_issue_to_notion":
            return await _execute_qa_sync(db, action)
        if action.action_type == "create_business_goal":
            return await _execute_goal_action(db, action)
        if action.action_type == "close_qa_issue":
            return await _execute_qa_close(db, action)
        return await original_execute(db, action)

    executor._execute = execute_with_goals_qa

    async def handle_with_goals_qa(
        db: Any,
        *,
        chat_id: int,
        telegram_user_id: int,
        text: str,
        source: str = "text",
        allow_conversation_passthrough: bool = False,
        active_kommo_lead_id: int | None = None,
    ) -> AgentReply:
        session = await memory.get_or_create_session(db, telegram_user_id=int(telegram_user_id))
        context = await memory.build_context(db, telegram_user_id=int(telegram_user_id), session=session)
        if active_kommo_lead_id is not None:
            context["active_kommo_lead_id"] = int(active_kommo_lead_id)
        raw = str(text or "").strip()
        normalized = _normal(raw)

        pending = context.get("qa_intake") if isinstance(context.get("qa_intake"), dict) else None
        qa_type = _qa_type_from_command(raw)
        body = _qa_body(raw)
        if qa_type is not None or normalized.startswith("/feedback"):
            if not body:
                reply = await _start_qa_intake(db, session=session, requested_type=qa_type)
            else:
                reply = await _capture_issue(
                    db,
                    session=session,
                    context=context,
                    text=body,
                    source=source,
                    requested_type=qa_type,
                    chat_id=chat_id,
                    telegram_user_id=telegram_user_id,
                    explicit=_explicit_save(raw),
                )
            return await _remember(db, session=session, user_text=raw, source=source, reply=reply)

        if pending and not raw.startswith("/"):
            reply = await _capture_issue(
                db,
                session=session,
                context=context,
                text=raw,
                source=source,
                requested_type=pending.get("issue_type"),
                chat_id=chat_id,
                telegram_user_id=telegram_user_id,
                explicit=False,
            )
            return await _remember(db, session=session, user_text=raw, source=source, reply=reply)

        if normalized in {"/bugs", "баги", "покажи баги", "какие баги открыты"}:
            reply = AgentReply(goals_qa_service.format_issue_list(await goals_qa_service.list_issues(db, telegram_user_id=telegram_user_id)), intent="qa_issue_list")
            return await _remember(db, session=session, user_text=raw, source=source, reply=reply)

        issue_match = re.fullmatch(r"/(?:bug|issue)\s+([A-Za-z]+-\d+|\d+)", raw, flags=re.I)
        if issue_match:
            issue = await goals_qa_service.get_issue(db, telegram_user_id=telegram_user_id, issue_ref=issue_match.group(1))
            reply = AgentReply(goals_qa_service.format_issue(issue) if issue else "❓ QA-карточка не найдена.", intent="qa_issue_detail")
            return await _remember(db, session=session, user_text=raw, source=source, reply=reply)

        add_match = re.match(r"^/bug_add\s+(\S+)\s+(.+)$", raw, flags=re.I | re.S)
        if add_match:
            issue = await goals_qa_service.get_issue(db, telegram_user_id=telegram_user_id, issue_ref=add_match.group(1))
            if issue:
                await goals_qa_service.append_issue_comment(db, issue, add_match.group(2))
                reply = AgentReply("✅ Детали добавлены.\n\n" + goals_qa_service.format_issue(issue), intent="qa_issue_updated")
            else:
                reply = AgentReply("❓ QA-карточка не найдена.", intent="qa_issue_missing")
            return await _remember(db, session=session, user_text=raw, source=source, reply=reply)

        new_match = re.match(r"^/bug_new\s+(.+)$", raw, flags=re.I | re.S)
        if new_match:
            project_number, lead_id = _context_project(context)
            issue, _ = await goals_qa_service.create_local_issue(
                db,
                telegram_user_id=telegram_user_id,
                text=new_match.group(1),
                issue_type="Bug",
                active_project_number=project_number,
                kommo_lead_id=lead_id,
                source=source,
                force_new=True,
            )
            reply = await _stage_or_sync_issue(db, issue=issue, chat_id=chat_id, telegram_user_id=telegram_user_id, explicit=True)
            return await _remember(db, session=session, user_text=raw, source=source, reply=reply)

        test_match = re.fullmatch(r"/bug_test\s+(\S+)", raw, flags=re.I)
        if test_match:
            issue = await goals_qa_service.get_issue(db, telegram_user_id=telegram_user_id, issue_ref=test_match.group(1))
            if not issue:
                reply = AgentReply("❓ QA-карточка не найдена.", intent="qa_issue_missing")
            else:
                issue.status = "Testing"
                await db.commit()
                await memory.update_context(db, session=session, values={"qa_retest_issue_id": int(issue.id)})
                reply = AgentReply(
                    f"🧪 Повторная проверка {html.escape(issue.issue_code or str(issue.id))}\n\n"
                    "Выполни исходный сценарий и ответь: «исправлено», «частично», «не исправлено» или «новая проблема». Можно приложить новый скриншот или голос.",
                    intent="qa_retest_started",
                )
            return await _remember(db, session=session, user_text=raw, source=source, reply=reply)

        if context.get("qa_retest_issue_id") and normalized in goals_qa_service.RETEST_RESULTS:
            issue = await goals_qa_service.get_issue(db, telegram_user_id=telegram_user_id, issue_ref=int(context["qa_retest_issue_id"]))
            if issue:
                result = goals_qa_service.RETEST_RESULTS[normalized]
                issue.retest_result = result
                issue.retested_at = datetime.now(timezone.utc)
                issue.status = "Verified" if normalized == "исправлено" else "Ready for test" if normalized == "частично" else "Confirmed"
                await db.commit()
                await memory.update_context(db, session=session, values={"qa_retest_issue_id": None})
                reply = AgentReply(f"✅ Результат проверки сохранён: {html.escape(result)}.\n\n" + goals_qa_service.format_issue(issue), intent="qa_retest_saved")
            else:
                reply = AgentReply("❓ QA-карточка для проверки не найдена.", intent="qa_issue_missing")
            return await _remember(db, session=session, user_text=raw, source=source, reply=reply)

        close_match = re.fullmatch(r"/bug_close\s+(\S+)", raw, flags=re.I)
        if close_match:
            issue = await goals_qa_service.get_issue(db, telegram_user_id=telegram_user_id, issue_ref=close_match.group(1))
            if not issue:
                reply = AgentReply("❓ QA-карточка не найдена.", intent="qa_issue_missing")
            else:
                preview = "<b>Закрыть QA-карточку?</b>\n\n" + goals_qa_service.format_issue(issue) + "\n\nЗакрытие возможно только после вашего подтверждения."
                action = await actions.stage_action(
                    db,
                    telegram_user_id=telegram_user_id,
                    chat_id=chat_id,
                    action_type="close_qa_issue",
                    payload={"issue_id": int(issue.id), "retest_result": issue.retest_result or "Исправлено"},
                    preview_text=preview,
                )
                reply = AgentReply(preview, reply_markup=actions.approval_markup(action.id), intent="qa_close_preview", metadata={"action_id": int(action.id)})
            return await _remember(db, session=session, user_text=raw, source=source, reply=reply)

        goal_text = _goal_capture(raw)
        if goal_text:
            preview = "<b>Добавить цель месяца?</b>\n\n" + html.escape(goal_text)
            action = await actions.stage_action(
                db,
                telegram_user_id=telegram_user_id,
                chat_id=chat_id,
                action_type="create_business_goal",
                payload={"telegram_user_id": telegram_user_id, "title": goal_text, "goal_type": "month"},
                preview_text=preview,
            )
            reply = AgentReply(preview, reply_markup=actions.approval_markup(action.id), intent="goal_create_preview", metadata={"action_id": int(action.id)})
            return await _remember(db, session=session, user_text=raw, source=source, reply=reply)

        if normalized in {"/month_goals", "какие цели стоят на месяц", "что запланировано на месяц", "цели месяца"}:
            reply = AgentReply(goals_qa_service.format_month_goals(await goals_qa_service.list_month_goals(db, telegram_user_id=telegram_user_id)), intent="month_goals")
            return await _remember(db, session=session, user_text=raw, source=source, reply=reply)
        if normalized in {"/month_progress", "что выполнено за месяц", "какой прогресс за месяц", "где мы отстаем", "где мы отстаём"}:
            reply = AgentReply(goals_qa_service.format_month_goals(await goals_qa_service.list_month_goals(db, telegram_user_id=telegram_user_id), progress_view=True), intent="month_progress")
            return await _remember(db, session=session, user_text=raw, source=source, reply=reply)
        if normalized in {"/day_results", "что я сегодня сделал", "какие задачи сегодня выполнены", "что осталось незакрытым"}:
            return await _remember(db, session=session, user_text=raw, source=source, reply=await _day_results(db, telegram_user_id=telegram_user_id))
        if normalized in {"/focus", "на чем мне сейчас сосредоточиться", "на чём мне сейчас сосредоточиться", "что сейчас самое важное", "что делать первым"}:
            return await _remember(db, session=session, user_text=raw, source=source, reply=await _focus_reply(db))
        if normalized in {"/plan_day", "спланируй мой день", "как правильно распределить сегодня время", "сделай расписание на сегодня", "разложи задачи по времени"}:
            return await original_handle(db, chat_id=chat_id, telegram_user_id=telegram_user_id, text="план дня", source=source, allow_conversation_passthrough=allow_conversation_passthrough, active_kommo_lead_id=active_kommo_lead_id)
        if normalized.startswith("/when") or any(phrase in normalized for phrase in ("во сколько мне лучше", "когда лучше позвонить", "когда заняться", "в какое время написать")):
            return await _remember(db, session=session, user_text=raw, source=source, reply=_when_reply(_command_arg(raw, "when") or raw))

        analysis_match = re.match(r"^/(whatsapp_analysis|client_analysis)(?:@\w+)?\s*(.*)$", raw, flags=re.I | re.S)
        natural_analysis = None
        if not analysis_match:
            if any(phrase in normalized for phrase in ("проанализируй whatsapp", "разбери переписку", "какие у клиента сомнения")):
                natural_analysis = ("whatsapp_analysis", re.sub(r"(?i)^.*?(?:whatsapp|переписку|клиента)\s*", "", raw).strip())
            elif normalized.startswith(("дай анализ клиента", "проанализируй клиента")):
                natural_analysis = ("client_analysis", re.sub(r"(?i)^.*?клиента\s*", "", raw).strip())
        if analysis_match or natural_analysis:
            kind = (analysis_match.group(1).lower() if analysis_match else natural_analysis[0])
            query = (analysis_match.group(2).strip() if analysis_match else natural_analysis[1])
            try:
                lead = await _resolve_lead(query, context, kind)
                reply = await _communication_analysis(db, lead=lead, broad=kind == "client_analysis")
            except Exception as exc:
                reply = AgentReply("❓ Не удалось однозначно определить клиента: " + html.escape(str(exc)[:500]), intent="client_clarification")
            return await _remember(db, session=session, user_text=raw, source=source, reply=reply)

        if normalized in {"/pipeline_health", "состояние воронки", "здоровье воронки"}:
            return await _remember(db, session=session, user_text=raw, source=source, reply=await _pipeline_health(db))
        if normalized in {"/capacity", "хватает ли у меня времени", "оценка загрузки"}:
            inbox = await next_action_service.build_inbox(db)
            load = len(inbox.overdue) * 2 + len(inbox.waiting_us) + len(inbox.without_next) + len(inbox.stale)
            verdict = "перегруз" if load >= 12 else "высокая загрузка" if load >= 7 else "управляемая загрузка"
            reply = AgentReply(f"<b>⚖️ Оценка загрузки</b>\n\nИндекс незакрытых действий: {load}. Состояние: <b>{verdict}</b>.\nСначала закрой просроченные обещания и клиентов, которые ждут нас; новые инициативы добавляй только после освобождения блока.", intent="capacity")
            return await _remember(db, session=session, user_text=raw, source=source, reply=reply)
        if normalized in {"/automation_candidates", "что можно автоматизировать", "какие действия автоматизировать"}:
            reply = AgentReply("<b>⚙️ Кандидаты на автоматизацию</b>\n\n1. Повторные follow-up по проектам без ответа.\n2. Контроль обещаний и проектов без следующего шага.\n3. Синхронизация комментариев Kommo → Sheets X после preview.\n4. Классификация и маршрутизация файлов фабрика/клиент.\n5. Еженедельное выявление повторяющихся QA-проблем.\n\nАвтоматическая отправка клиентам не включается без отдельного подтверждения.", intent="automation_candidates")
            return await _remember(db, session=session, user_text=raw, source=source, reply=reply)

        slash_map = {
            "/next_action": "следующий шаг",
            "/risks": "что горит",
            "/waiting_us": "ждут нас",
            "/waiting_client": "мы ждем",
            "/stale": "зависшие",
            "/without_next_action": "без шага",
            "/followups": "кому ответить",
            "/project_summary": "покажи проект",
            "/what_changed": "история",
            "/promises": "история",
            "/prepare_call": "подготовь меня к звонку по проекту",
            "/supplier_analysis": "проанализируй поставщика",
            "/compare_quotes": "сравни предложения фабрик",
            "/document_status": "проверь документы проекта",
            "/priority_clients": "приоритеты",
            "/weekly_focus": "что горит",
            "/delegate": "что можно делегировать",
        }
        for prefix, replacement in slash_map.items():
            if normalized.startswith(prefix):
                arg = raw[len(prefix):].strip()
                mapped = f"{replacement} {arg}".strip()
                return await original_handle(db, chat_id=chat_id, telegram_user_id=telegram_user_id, text=mapped, source=source, allow_conversation_passthrough=allow_conversation_passthrough, active_kommo_lead_id=active_kommo_lead_id)

        return await original_handle(
            db,
            chat_id=chat_id,
            telegram_user_id=telegram_user_id,
            text=text,
            source=source,
            allow_conversation_passthrough=allow_conversation_passthrough,
            active_kommo_lead_id=active_kommo_lead_id,
        )

    agent_service.handle_message = handle_with_goals_qa
    kaizen_runtime.agent_service.handle_message = handle_with_goals_qa
