from __future__ import annotations

import html
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.agent import actions, audit, clarification, digest, generation, memory, notion_gateway, planner, security, tools
from app.agent.contracts import AgentPlan, AgentReply
from app.agent.executor import execute_action
from app.agent.lead_refs import extract_internal_lead_number, user_error_hint
from app.config import get_settings
from app.services import ai_analysis_service, calendar_event_builder, kommo_service, telegram_service

logger = logging.getLogger(__name__)
settings = get_settings()


def _plain_html(value: str) -> str:
    return html.escape(value or "").replace("\n", "\n")


def _help_text() -> str:
    return (
        "<b>🧠 B&BS AI Agent</b>\n\n"
        "Можно писать или говорить обычными словами:\n"
        "• <i>Что мне делать сегодня?</i>\n"
        "• <i>Покажи сделку 135 кормушки</i>\n"
        "• <i>Сделай КП по #123456 на польском</i>\n"
        "• <i>Подготовь запрос поставщику по этой сделке</i>\n"
        "• <i>Добавь примечание в #123456: клиент ждёт цену</i>\n"
        "• <i>Поставь задачу по #123456 завтра в 10:00</i>\n"
        "• <i>Запланируй созвон по этой сделке в пятницу в 15:00</i>\n"
        "• <i>Проверь Notion</i>\n\n"
        "Чтение и подготовка черновиков выполняются сразу. Любая запись в Kommo, "
        "Notion, Gmail или Calendar — только после кнопки подтверждения."
    )


def _error_text(exc: Exception) -> str:
    safe = security.sanitize_text(str(exc), limit=800) or exc.__class__.__name__
    return f"❌ <b>Не удалось выполнить запрос</b>\n\n<code>{html.escape(safe)}</code>"


def _draft_text(lead: dict[str, Any], draft: dict[str, Any]) -> str:
    body = str(draft.get("body") or "")
    preview = body[:3200]
    if len(body) > len(preview):
        preview += "\n\n…текст сокращён в Telegram"
    lines = [
        f"<b>📝 {html.escape(str(draft.get('title') or 'Рабочий черновик'))}</b>",
        f"Сделка: <b>{html.escape(str(lead.get('name') or lead.get('id') or '—'))}</b>",
        "",
    ]
    if draft.get("subject"):
        lines.extend([f"<b>Тема:</b> {html.escape(str(draft['subject']))}", ""])
    lines.append(html.escape(preview))
    missing = draft.get("missing_data") or []
    if missing:
        lines.extend(["", "<b>Нужно уточнить</b>"])
        lines.extend(f"• {html.escape(str(item))}" for item in missing[:12])
    assumptions = draft.get("assumptions") or []
    if assumptions:
        lines.extend(["", "<b>Допущения</b>"])
        lines.extend(f"• {html.escape(str(item))}" for item in assumptions[:8])
    lines.extend(["", "Черновик не отправлен клиенту и не сохранён во внешние сервисы."])
    return "\n".join(lines)


async def _resolve_lead_for_plan(
    db: AsyncSession,
    *,
    plan: AgentPlan,
    context: dict[str, Any],
    session: Any,
) -> dict[str, Any]:
    lead = await tools.resolve_lead(
        lead_id=plan.lead_id,
        query=plan.query,
        context=context,
        plan=plan,
    )
    internal = extract_internal_lead_number(lead)
    await memory.set_active_lead(
        db,
        session=session,
        kommo_lead_id=int(lead["id"]),
        lead_name=str(lead.get("name") or ""),
    )
    context["active_kommo_lead_id"] = int(lead["id"])
    context["active_lead_name"] = lead.get("name")
    if internal:
        await memory.update_context(
            db,
            session=session,
            values={"active_internal_lead_number": internal},
        )
    return lead


async def _resolve_leads_for_plan(
    db: AsyncSession,
    *,
    plan: AgentPlan,
    context: dict[str, Any],
    session: Any,
    pre_resolved: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], AgentReply | None]:
    if pre_resolved:
        return pre_resolved, None
    result = await tools.resolve_leads(
        lead_id=plan.lead_id,
        query=plan.query,
        context=context,
        plan=plan,
    )
    if result.unresolved or (not result.resolved and result.error_message):
        pending = clarification.build_pending(
            plan=plan,
            original_text=text,
            source="text",
            requested_refs=result.unresolved,
            resolved_leads=result.resolved,
            unresolved_refs=result.unresolved,
        )
        await clarification.save_pending(db, session, pending)
        context["pending_clarification"] = pending
        if result.candidates:
            return result.resolved, AgentReply(
                tools.format_candidates(result.candidates),
                reply_markup=tools.candidates_markup(result.candidates),
                intent="lead_disambiguation",
            )
        unresolved_labels = []
        for ref in result.unresolved:
            if ref.internal_lead_number:
                unresolved_labels.append(f"№{ref.internal_lead_number}")
            elif ref.digest_position:
                unresolved_labels.append(f"позиция {ref.digest_position}")
            elif ref.name_query:
                unresolved_labels.append(ref.name_query)
        label = ", ".join(unresolved_labels) or "клиентов"
        question = (
            f"Не удалось однозначно найти: {html.escape(label)}. "
            "Выбери сделку кнопкой или напиши название."
        )
        return result.resolved, AgentReply(f"❓ {question}", intent="lead_clarification")
    return result.resolved, None


def _lead_label(lead: dict[str, Any]) -> str:
    internal = extract_internal_lead_number(lead) or lead.get("internal_lead_number")
    name = str(lead.get("name") or lead.get("id") or "—")
    if internal:
        return f"№{internal} — {name}"
    return name


async def handle_message(
    db: AsyncSession,
    *,
    chat_id: int,
    telegram_user_id: int,
    text: str,
    source: str = "text",
    allow_conversation_passthrough: bool = False,
    active_kommo_lead_id: int | None = None,
) -> AgentReply:
    session = await memory.get_or_create_session(
        db, telegram_user_id=telegram_user_id
    )
    if active_kommo_lead_id is not None:
        await memory.set_active_lead(
            db,
            session=session,
            kommo_lead_id=int(active_kommo_lead_id),
        )
    context = await memory.build_context(
        db, telegram_user_id=telegram_user_id, session=session
    )
    await memory.remember_message(
        db,
        session=session,
        role="user",
        content=text,
        source=source,
    )

    if clarification.is_menu_command(text):
        await clarification.clear_pending(db, session)
        context.pop("pending_clarification", None)

    pending = clarification.get_pending(context)
    pre_resolved: list[dict[str, Any]] | None = None
    pending_plan: AgentPlan | None = None

    if pending and not clarification.is_cancel_command(text):
        pending_plan, pre_resolved, unresolved, ambiguous = await clarification.continue_pending(
            pending, text, context
        )
        if ambiguous == "ambiguous":
            candidates = await tools.resolve_leads(
                lead_id=None,
                query=text,
                context=context,
                plan=pending_plan,
            )
            reply = AgentReply(
                tools.format_candidates(candidates.candidates),
                reply_markup=tools.candidates_markup(candidates.candidates),
                intent="lead_disambiguation",
            )
            if reply.handled and reply.text:
                await memory.remember_message(
                    db, session=session, role="assistant", content=reply.text,
                    source="agent", intent=reply.intent,
                )
            return reply
        if unresolved:
            pending = clarification.build_pending(
                plan=pending_plan or AgentPlan(intent=str(pending.get("original_intent"))),
                original_text=str(pending.get("original_text") or text),
                source=str(pending.get("source") or source),
                requested_refs=unresolved,
                resolved_leads=pre_resolved or [],
                unresolved_refs=unresolved,
            )
            pending["task_title"] = (pending_plan or AgentPlan()).title or pending.get("task_title")
            pending["due_at"] = (pending_plan or AgentPlan()).due_at or pending.get("due_at")
            pending["note_text"] = (pending_plan or AgentPlan()).note_text or pending.get("note_text")
            await clarification.save_pending(db, session, pending)
            labels = [
                f"№{ref.internal_lead_number}" if ref.internal_lead_number else ref.name_query or ref.raw
                for ref in unresolved
            ]
            reply = AgentReply(
                f"❓ Не удалось однозначно найти: {html.escape(', '.join(labels))}. "
                "Выбери сделку кнопкой или напиши название.",
                intent="lead_clarification",
            )
            if reply.handled and reply.text:
                await memory.remember_message(
                    db, session=session, role="assistant", content=reply.text,
                    source="agent", intent=reply.intent,
                )
            return reply
        if pending_plan and pre_resolved:
            await clarification.clear_pending(db, session)
            context.pop("pending_clarification", None)
            plan = pending_plan
            try:
                reply = await _execute_plan(
                    db,
                    plan=plan,
                    text=str(pending.get("original_text") or text),
                    chat_id=chat_id,
                    telegram_user_id=telegram_user_id,
                    source=source,
                    context=context,
                    session=session,
                    pre_resolved_leads=pre_resolved,
                )
            except tools.LeadResolutionError as exc:
                reply = AgentReply(
                    f"❓ {html.escape(str(exc))}", intent="lead_clarification"
                )
            except Exception as exc:
                logger.exception("Unified agent pending continuation failed")
                reply = AgentReply(_error_text(exc), intent="error")
            if reply.handled and reply.text:
                await memory.remember_message(
                    db, session=session, role="assistant", content=reply.text,
                    source="agent", intent=reply.intent, metadata=reply.metadata,
                )
            return reply

    try:
        plan = await planner.plan_message(text, context=context)
        if plan.intent == "cancel_clarification" or clarification.is_cancel_command(text):
            await clarification.clear_pending(db, session)
            return AgentReply(
                "❌ Незавершённый запрос отменён. Внешние сервисы не изменены.",
                intent="cancel_clarification",
            )
        session.last_intent = plan.intent
        await db.commit()
        if plan.mode == "conversation" and allow_conversation_passthrough:
            return AgentReply(
                text="",
                handled=False,
                intent=plan.intent,
                metadata={"plan": plan.model_dump()},
            )
        reply = await _execute_plan(
            db,
            plan=plan,
            text=text,
            chat_id=chat_id,
            telegram_user_id=telegram_user_id,
            source=source,
            context=context,
            session=session,
        )
    except tools.LeadResolutionError as exc:
        if exc.candidates:
            reply = AgentReply(
                tools.format_candidates(exc.candidates),
                reply_markup=tools.candidates_markup(exc.candidates),
                intent="lead_disambiguation",
            )
        else:
            reply = AgentReply(
                f"❓ {html.escape(str(exc))}", intent="lead_clarification"
            )
    except Exception as exc:
        logger.exception("Unified agent request failed")
        await audit.record_event(
            db,
            service="agent",
            operation="handle_message",
            status="error",
            telegram_user_id=telegram_user_id,
            payload={"source": source, "text": text[:2000]},
            error_message=str(exc),
        )
        reply = AgentReply(_error_text(exc), intent="error")

    if reply.handled and reply.text:
        await memory.remember_message(
            db,
            session=session,
            role="assistant",
            content=reply.text,
            source="agent",
            intent=reply.intent,
            metadata=reply.metadata,
        )
        try:
            count = await memory.message_count(
                db, telegram_user_id=telegram_user_id
            )
            every = max(10, settings.agent_memory_compact_every)
            if count and count % every == 0:
                history = await memory.recent_messages(
                    db, telegram_user_id=telegram_user_id, limit=30
                )
                session.memory_summary = await generation.summarize_memory(
                    current_summary=session.memory_summary, messages=history
                )
                await db.commit()
        except Exception as exc:
            logger.info("Agent memory compaction skipped: %s", exc)
    return reply


async def _execute_plan(
    db: AsyncSession,
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
    if plan.mode == "clarify" or plan.clarification_question:
        return AgentReply(
            f"❓ {html.escape(plan.clarification_question or 'Уточни запрос, пожалуйста.')}",
            intent=plan.intent,
        )

    if plan.intent == "help":
        return AgentReply(_help_text(), intent=plan.intent)

    if plan.intent == "reset_memory":
        await memory.reset_session(db, telegram_user_id=telegram_user_id)
        return AgentReply(
            "✅ Контекст агента очищен. История CRM и внешние сервисы не изменены.",
            intent=plan.intent,
        )

    if plan.intent == "daily_digest":
        result = await digest.build_digest()
        last_digest = digest.build_last_digest_context(result)
        await memory.update_context(db, session=session, values={"last_digest": last_digest})
        context["last_digest"] = last_digest
        markup = digest.digest_markup(result.get("digest_map") or [])
        return AgentReply(
            digest.format_digest(result),
            reply_markup=markup,
            intent=plan.intent,
            metadata=result,
        )

    if plan.intent == "notion_diagnostics":
        result = await notion_gateway.validate_schema(include_optional=True)
        return AgentReply(
            notion_gateway.format_schema_report(result), intent=plan.intent, metadata=result
        )

    if plan.intent == "integration_errors":
        errors = await audit.recent_errors(db, limit=10)
        if not errors:
            return AgentReply("✅ В журнале агента нет последних ошибок.", intent=plan.intent)
        lines = ["<b>⚠️ Последние ошибки интеграций</b>", ""]
        for event in errors:
            lines.append(
                f"• {html.escape(event.service)} / {html.escape(event.operation)} — "
                f"{html.escape(str(event.error_message or 'ошибка')[:300])}"
            )
        return AgentReply("\n".join(lines), intent=plan.intent)

    if plan.intent in {"search_lead", "lead_summary"}:
        lead = await _resolve_lead_for_plan(
            db, plan=plan, context=context, session=session
        )
        return AgentReply(
            tools.format_lead_summary(lead),
            intent=plan.intent,
            metadata={"lead_id": lead.get("id")},
        )

    if plan.intent == "generate_draft":
        lead = await _resolve_lead_for_plan(
            db, plan=plan, context=context, session=session
        )
        draft = await generation.generate_draft(
            kind=str(plan.draft_kind or "followup_message"),
            lead=tools.lead_summary_for_ai(lead),
            language=plan.language,
            manager_request=text,
        )
        base_text = _draft_text(lead, draft)
        await memory.update_context(
            db,
            session=session,
            values={
                "last_draft": draft,
                "last_draft_lead": {
                    "id": lead.get("id"),
                    "name": lead.get("name"),
                    "url": lead.get("url"),
                    "updated_at": lead.get("updated_at"),
                },
                "last_draft_created_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        context["last_draft"] = draft
        context["last_draft_lead"] = (session.context or {}).get("last_draft_lead")
        rows: list[list[dict[str, str]]] = []
        if settings.notion_projects_data_source_id and settings.notion_tasks_data_source_id:
            notion_action = await actions.stage_action(
                db,
                telegram_user_id=telegram_user_id,
                chat_id=chat_id,
                action_type="save_draft_to_notion",
                payload={
                    "lead": {
                        "id": lead.get("id"),
                        "name": lead.get("name"),
                        "url": lead.get("url"),
                        "updated_at": lead.get("updated_at"),
                    },
                    "draft": draft,
                },
                preview_text=base_text,
            )
            rows.append(
                [
                    {
                        "text": "📓 Сохранить в Notion",
                        "callback_data": f"agent:ok:{notion_action.id}",
                    }
                ]
            )
        if plan.draft_kind == "email":
            contacts = lead.get("contacts") or []
            recipient = ((contacts[0].get("emails") or [None])[0] if contacts else None)
            if recipient:
                gmail_action = await actions.stage_action(
                    db,
                    telegram_user_id=telegram_user_id,
                    chat_id=chat_id,
                    action_type="create_gmail_draft",
                    payload={
                        "to": recipient,
                        "subject": draft.get("subject") or draft.get("title") or "",
                        "body": draft.get("body") or "",
                    },
                    preview_text=base_text,
                )
                rows.append(
                    [
                        {
                            "text": "✉️ Создать черновик Gmail",
                            "callback_data": f"agent:ok:{gmail_action.id}",
                        }
                    ]
                )
        return AgentReply(
            base_text,
            reply_markup={"inline_keyboard": rows} if rows else None,
            intent=plan.intent,
            metadata={"lead_id": lead.get("id"), "draft_kind": plan.draft_kind},
        )

    if plan.intent == "conversation_analysis":
        analysis = await ai_analysis_service.analyse_transcript(text)
        formatted = telegram_service.format_report(analysis, text)
        return AgentReply(formatted, intent=plan.intent)

    if plan.intent == "sync_leads_to_notion":
        open_result = await kommo_service.get_all_open_leads()
        leads = [
            {
                "id": item.get("id"),
                "name": item.get("name"),
                "url": item.get("url"),
                "updated_at": item.get("updated_at"),
            }
            for item in (open_result.get("leads") or [])[: settings.agent_sync_max_leads]
            if item.get("id")
        ]
        preview = (
            "<b>Подтвердить синхронизацию Kommo → Notion?</b>\n\n"
            f"Будет создано или обновлено до <b>{len(leads)}</b> проектов.\n"
            "Kommo изменяться не будет."
        )
        action = await actions.stage_action(
            db,
            telegram_user_id=telegram_user_id,
            chat_id=chat_id,
            action_type="sync_leads_to_notion",
            payload={"leads": leads, "limit": len(leads)},
            preview_text=preview,
        )
        return AgentReply(
            preview,
            reply_markup=actions.approval_markup(action.id),
            intent=plan.intent,
        )

    if plan.intent in {
        "add_kommo_note",
        "add_kommo_notes_batch",
        "create_kommo_task",
        "create_kommo_tasks_batch",
        "create_calendar_event",
        "update_kommo_lead",
        "create_gmail_draft",
        "save_draft_to_notion",
    }:
        return await _stage_write(
            db,
            plan=plan,
            text=text,
            chat_id=chat_id,
            telegram_user_id=telegram_user_id,
            context=context,
            session=session,
            pre_resolved_leads=pre_resolved_leads,
        )

    active_lead = None
    active_id = context.get("active_kommo_lead_id")
    if active_id:
        try:
            active_lead = tools.lead_summary_for_ai(
                await kommo_service.get_lead_details(int(active_id))
            )
        except Exception as exc:
            logger.info("Could not load active lead for general answer: %s", exc)
    answer = await generation.answer_manager(
        message=text,
        context=context,
        active_lead=active_lead,
    )
    return AgentReply(_plain_html(answer), intent=plan.intent or "general_assistant")


async def _stage_write(
    db: AsyncSession,
    *,
    plan: AgentPlan,
    text: str,
    chat_id: int,
    telegram_user_id: int,
    context: dict[str, Any],
    session: Any,
    pre_resolved_leads: list[dict[str, Any]] | None = None,
) -> AgentReply:
    batch_intents = {"create_kommo_tasks_batch", "add_kommo_notes_batch"}
    if plan.intent in batch_intents:
        leads, clarify_reply = await _resolve_leads_for_plan(
            db, plan=plan, context=context, session=session, pre_resolved=pre_resolved_leads
        )
        if clarify_reply:
            return clarify_reply
        if not leads:
            return AgentReply(f"❓ {html.escape(user_error_hint())}", intent=plan.intent)
        if plan.intent == "create_kommo_tasks_batch" and not plan.due_at:
            pending = clarification.build_pending(
                plan=plan,
                original_text=text,
                source="text",
                requested_refs=[],
                resolved_leads=leads,
                unresolved_refs=[],
            )
            await clarification.save_pending(db, session, pending)
            return AgentReply("❓ Когда выполнить задачи?", intent=plan.intent)
        return await _stage_batch_write(
            db,
            plan=plan,
            text=text,
            chat_id=chat_id,
            telegram_user_id=telegram_user_id,
            leads=leads,
        )

    lead: dict[str, Any] | None = None
    if plan.intent != "create_calendar_event" or plan.lead_id or plan.query or plan.lead_refs or context.get("active_kommo_lead_id"):
        if pre_resolved_leads:
            lead = pre_resolved_leads[0]
        else:
            lead = await _resolve_lead_for_plan(
                db, plan=plan, context=context, session=session
            )

    if plan.intent == "add_kommo_note":
        note = str(plan.note_text or plan.body or "").strip()
        if not note:
            return AgentReply("❓ Что именно добавить в примечание?", intent=plan.intent)
        preview = (
            "<b>Добавить примечание в Kommo?</b>\n\n"
            f"Сделка: <b>{html.escape(str(lead.get('name') if lead else '—'))}</b>\n\n"
            f"{html.escape(note)}"
        )
        action_type = "add_kommo_note"
        payload = {"lead_id": int(lead["id"]), "note_text": note}

    elif plan.intent == "create_kommo_task":
        if not plan.due_at:
            return AgentReply("❓ Когда должна быть выполнена задача?", intent=plan.intent)
        try:
            start_at, _ = calendar_event_builder.parse_natural_datetime(plan.due_at)
        except ValueError as exc:
            return AgentReply(f"❓ {html.escape(str(exc))}", intent=plan.intent)
        task_text = str(plan.title or text).strip()
        preview = (
            "<b>Создать задачу в Kommo?</b>\n\n"
            f"Сделка: <b>{html.escape(str(lead.get('name') if lead else '—'))}</b>\n"
            f"Срок: <b>{html.escape(calendar_event_builder.format_time_range(start_at, start_at + timedelta(minutes=1)).split('–')[0])}</b>\n"
            f"Задача: {html.escape(task_text)}"
        )
        action_type = "create_kommo_task"
        payload = {
            "lead_id": int(lead["id"]),
            "task_text": task_text,
            "due_at": plan.due_at,
        }

    elif plan.intent == "create_calendar_event":
        if not plan.due_at:
            return AgentReply("❓ На какую дату и время запланировать событие?", intent=plan.intent)
        try:
            start_at, duration = calendar_event_builder.parse_natural_datetime(
                plan.due_at, duration_minutes=plan.duration_minutes
            )
        except ValueError as exc:
            return AgentReply(f"❓ {html.escape(str(exc))}", intent=plan.intent)
        title = plan.title or calendar_event_builder.build_event_title(
            plan.event_type, str(lead.get("name") if lead else "") or None
        )
        preview = (
            "<b>Добавить событие?</b>\n\n"
            f"{html.escape(str(title)[:300])}\n"
            f"Когда: <b>{html.escape(calendar_event_builder.format_time_range(start_at, start_at + timedelta(minutes=duration)))}</b>\n"
            f"Длительность: {duration} мин.\n"
            + (f"Сделка: {html.escape(str(lead.get('name')))}" if lead else "Без привязки к сделке")
        )
        action_type = "create_calendar_event"
        payload = {
            "lead_id": int(lead["id"]) if lead else None,
            "title": title,
            "due_at": plan.due_at,
            "duration_minutes": duration,
            "reminder_minutes": plan.reminder_minutes,
            "event_type": plan.event_type,
        }

    elif plan.intent == "update_kommo_lead":
        fields = {key: value for key, value in plan.fields.items() if key in {"name", "price", "status_id"} and value is not None}
        if not fields:
            return AgentReply("❓ Что изменить: название, бюджет или этап?", intent=plan.intent)
        preview = (
            "<b>Обновить сделку Kommo?</b>\n\n"
            f"Сделка: <b>{html.escape(str(lead.get('name') if lead else '—'))}</b>\n"
            + "\n".join(f"• {html.escape(k)}: {html.escape(str(v))}" for k, v in fields.items())
        )
        action_type = "update_kommo_lead"
        payload = {"lead_id": int(lead["id"]), "fields": fields}

    elif plan.intent == "save_draft_to_notion":
        draft = context.get("last_draft")
        if not isinstance(draft, dict) or not draft.get("body"):
            return AgentReply(
                "❓ Сначала попроси меня подготовить КП, письмо, follow-up или другой черновик.",
                intent=plan.intent,
            )
        if lead is None:
            saved_lead = context.get("last_draft_lead") or {}
            if saved_lead.get("id"):
                lead = await tools.resolve_lead(
                    lead_id=int(saved_lead["id"]), query=None, context=context
                )
        preview = (
            "<b>Сохранить последний черновик в Notion?</b>\n\n"
            f"Сделка: <b>{html.escape(str(lead.get('name') if lead else '—'))}</b>\n"
            f"Документ: {html.escape(str(draft.get('title') or 'Черновик'))}"
        )
        action_type = "save_draft_to_notion"
        payload = {
            "lead": {
                "id": lead.get("id"),
                "name": lead.get("name"),
                "url": lead.get("url"),
                "updated_at": lead.get("updated_at"),
            },
            "draft": draft,
        }

    elif plan.intent == "create_gmail_draft":
        draft = context.get("last_draft")
        if not isinstance(draft, dict) or not draft.get("body"):
            return AgentReply(
                "❓ Сначала попроси меня подготовить письмо или другой текст.",
                intent=plan.intent,
            )
        contacts = (lead or {}).get("contacts") or []
        recipient = ((contacts[0].get("emails") or [None])[0] if contacts else None)
        if not recipient:
            return AgentReply(
                "❓ В сделке нет email получателя. Добавь email в Kommo или попроси подготовить текст без Gmail.",
                intent=plan.intent,
            )
        preview = (
            "<b>Создать черновик Gmail?</b>\n\n"
            f"Кому: <code>{html.escape(str(recipient))}</code>\n"
            f"Тема: {html.escape(str(draft.get('subject') or draft.get('title') or ''))}"
        )
        action_type = "create_gmail_draft"
        payload = {
            "to": recipient,
            "subject": draft.get("subject") or draft.get("title") or "",
            "body": draft.get("body") or "",
        }

    else:
        return AgentReply("❓ Сначала подготовь черновик, затем выбери действие кнопкой.", intent=plan.intent)

    action = await actions.stage_action(
        db,
        telegram_user_id=telegram_user_id,
        chat_id=chat_id,
        action_type=action_type,
        payload=payload,
        preview_text=preview,
    )
    return AgentReply(
        preview,
        reply_markup=actions.approval_markup(action.id),
        intent=plan.intent,
        metadata={"action_id": action.id, "action_type": action_type},
    )


async def _stage_batch_write(
    db: AsyncSession,
    *,
    plan: AgentPlan,
    text: str,
    chat_id: int,
    telegram_user_id: int,
    leads: list[dict[str, Any]],
) -> AgentReply:
    items = [
        {
            "lead_id": int(lead["id"]),
            "internal_lead_number": extract_internal_lead_number(lead) or lead.get("internal_lead_number"),
            "name": lead.get("name"),
            "url": lead.get("url"),
        }
        for lead in leads
    ]
    if plan.intent == "create_kommo_tasks_batch":
        if not plan.due_at:
            return AgentReply("❓ Когда выполнить задачи?", intent=plan.intent)
        try:
            start_at, _ = calendar_event_builder.parse_natural_datetime(plan.due_at)
        except ValueError as exc:
            return AgentReply(f"❓ {html.escape(str(exc))}", intent=plan.intent)
        task_text = str(plan.title or text).strip()
        # Strip lead numbers from task text for display
        task_text = re.sub(
            r"^(?:поставь|создай)\s+задач[аиу]?\s+",
            "",
            task_text,
            flags=re.I,
        ).strip()
        if not task_text or len(task_text) > 200:
            task_text = "Связаться с клиентом"
        lines = [
            f"<b>Создать {len(items)} задач в Kommo?</b>",
            "",
        ]
        for index, item in enumerate(items, 1):
            lines.append(f"{index}. {html.escape(_lead_label(leads[index - 1]))}")
        lines.extend(
            [
                "",
                f"Задача: {html.escape(task_text)}",
                f"Срок: {html.escape(calendar_event_builder.format_time_range(start_at, start_at + timedelta(minutes=1)).split('–')[0])}",
            ]
        )
        preview = "\n".join(lines)
        action_type = "create_kommo_tasks_batch"
        payload = {
            "items": items,
            "task_text": task_text,
            "due_at": plan.due_at,
            "item_results": {},
        }
        confirm_label = f"✅ Создать {len(items)} задач"
    else:
        note = str(plan.note_text or plan.body or "").strip()
        if not note:
            return AgentReply("❓ Что именно добавить в примечание?", intent=plan.intent)
        lines = [
            f"<b>Добавить примечание в {len(items)} сделок Kommo?</b>",
            "",
        ]
        for index, item in enumerate(items, 1):
            lines.append(f"{index}. {html.escape(_lead_label(leads[index - 1]))}")
        lines.extend(["", html.escape(note)])
        preview = "\n".join(lines)
        action_type = "add_kommo_notes_batch"
        payload = {
            "items": items,
            "note_text": note,
            "item_results": {},
        }
        confirm_label = f"✅ Добавить в {len(items)} сделок"

    action = await actions.stage_action(
        db,
        telegram_user_id=telegram_user_id,
        chat_id=chat_id,
        action_type=action_type,
        payload=payload,
        preview_text=preview,
    )
    markup = actions.approval_markup(action.id)
    markup["inline_keyboard"][0][0]["text"] = confirm_label
    return AgentReply(
        preview,
        reply_markup=markup,
        intent=plan.intent,
        metadata={"action_id": action.id, "action_type": action_type},
    )


async def handle_callback(
    db: AsyncSession,
    *,
    callback_data: str,
    telegram_user_id: int,
) -> AgentReply | None:
    if not callback_data.startswith("agent:"):
        return None
    parts = callback_data.split(":")
    if len(parts) < 3:
        return AgentReply("❌ Некорректная команда агента.", intent="callback_error")
    command = parts[1]

    session = await memory.get_or_create_session(
        db, telegram_user_id=telegram_user_id
    )
    context = await memory.build_context(
        db, telegram_user_id=telegram_user_id, session=session
    )

    if command == "lead" and parts[2].isdigit():
        try:
            lead = await kommo_service.get_lead_details(int(parts[2]))
            internal = extract_internal_lead_number(lead)
            await memory.set_active_lead(
                db,
                session=session,
                kommo_lead_id=int(parts[2]),
                lead_name=str(lead.get("name") or ""),
            )
            if internal:
                await memory.update_context(
                    db, session=session, values={"active_internal_lead_number": internal}
                )
            return AgentReply(
                tools.format_lead_summary(lead),
                reply_markup=tools.lead_card_actions_markup(lead),
                intent="lead_selected",
                metadata={"lead_id": int(parts[2])},
            )
        except Exception as exc:
            logger.exception("Could not select Kommo lead from agent callback")
            return AgentReply(_error_text(exc), intent="lead_selection_failed")

    if command == "digest" and parts[2].isdigit():
        position = int(parts[2])
        last_digest = context.get("last_digest") or {}
        item = next(
            (x for x in (last_digest.get("items") or []) if int(x.get("position") or 0) == position),
            None,
        )
        if not item or not item.get("kommo_lead_id"):
            return AgentReply(
                "❓ Позиция не найдена в последнем дайджесте. Запроси /digest.",
                intent="digest_selection_failed",
            )
        lead = await kommo_service.get_lead_details(int(item["kommo_lead_id"]))
        internal = item.get("internal_lead_number") or extract_internal_lead_number(lead)
        await memory.set_active_lead(
            db,
            session=session,
            kommo_lead_id=int(item["kommo_lead_id"]),
            lead_name=str(lead.get("name") or ""),
        )
        if internal:
            await memory.update_context(
                db, session=session, values={"active_internal_lead_number": internal}
            )
        return AgentReply(
            tools.format_lead_summary(lead),
            reply_markup=tools.lead_card_actions_markup(lead),
            intent="digest_lead_selected",
            metadata={"lead_id": int(item["kommo_lead_id"]), "position": position},
        )

    if command == "prep" and len(parts) >= 4 and parts[3].isdigit():
        kind = parts[2]
        lead_id = int(parts[3])
        lead = await kommo_service.get_lead_details(lead_id)
        await memory.set_active_lead(
            db,
            session=session,
            kommo_lead_id=lead_id,
            lead_name=str(lead.get("name") or ""),
        )
        if kind == "task":
            return AgentReply(
                f"📞 Для {_lead_label(lead)} напиши срок задачи.\n"
                "Например: <i>завтра в 10:00</i>",
                intent="prep_task",
            )
        if kind == "note":
            return AgentReply(
                f"📝 Для {_lead_label(lead)} напиши текст примечания.",
                intent="prep_note",
            )
        if kind == "draft":
            plan = AgentPlan(
                intent="generate_draft",
                mode="draft",
                lead_id=lead_id,
                draft_kind="followup_message",
            )
            return await _execute_plan(
                db,
                plan=plan,
                text="Подготовь follow-up",
                chat_id=0,
                telegram_user_id=telegram_user_id,
                source="callback",
                context=context,
                session=session,
            )

    if command in {"ok", "no"} and parts[2].isdigit():
        action_id = int(parts[2])
        action = await actions.get_action(db, action_id)
        if not action:
            return AgentReply("⌛ Действие не найдено или уже удалено.", intent="callback_missing")
        if command == "no":
            await actions.reject_action(
                db, action=action, telegram_user_id=telegram_user_id
            )
            return AgentReply("❌ Действие отменено. Внешние сервисы не изменены.", intent="action_rejected")
        try:
            text = await execute_action(
                db, action=action, telegram_user_id=telegram_user_id
            )
            return AgentReply(text, intent="action_executed")
        except Exception as exc:
            logger.exception("Confirmed agent action failed")
            return AgentReply(_error_text(exc), intent="action_failed")

    return AgentReply("❌ Некорректная команда агента.", intent="callback_error")
