from __future__ import annotations

import asyncio
import html
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.agent import actions, audit, clarification, digest, generation, memory, notion_gateway, planner, project_drive, project_linking, project_snapshot, project_updates, security, tools
from app.agent.contracts import AgentPlan, AgentReply
from app.agent.executor import execute_action
from app.agent.lead_refs import extract_internal_lead_number, user_error_hint
from app.config import get_settings
from app.services import (
    ai_analysis_service,
    ai_usage_service,
    calendar_event_builder,
    calendar_policy,
    client_language_service,
    client_message_service,
    conversation_analysis_service,
    drive_diagnostics,
    identity_service,
    kommo_service,
    lead_assessment_service,
    next_action_service,
    outbox_service,
    project_artifact_service,
    project_timeline_service,
    sheets_analytics_service,
    storage_service,
    telegram_service,
    unified_project_service,
)
from app.services.google_drive_service import sanitize_filename

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
        "• <i>Покажи проект 134</i> или <i>что по Maciej Walasek?</i>\n"
        "• <i>Найди проект по телефону, компании или товару</i>\n"
        "• <i>Сделай КП по #123456 на польском</i>\n"
        "• <i>Подготовь запрос поставщику по этой сделке</i>\n"
        "• <i>Добавь примечание в #123456: клиент ждёт цену</i>\n"
        "• <i>Поставь задачу по #123456 завтра в 10:00</i>\n"
        "• <i>Запланируй созвон по этой сделке в пятницу в 15:00</i>\n"
        "• <i>Проверь Notion</i>\n\n"
        "Файл можно отправить с подписью: <i>предложение производителя для проекта 134</i>.\n"
        "Голосом можно дать пакетное обновление проекта — каждое изменение подтверждается отдельно.\n\n"
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
    if draft.get("language"):
        lines.extend(
            [
                f"<b>Язык клиента:</b> {html.escape(str(draft['language']).upper())}",
                "",
            ]
        )
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
    original_text: str,
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
            original_text=original_text,
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

    plan: AgentPlan | None = None
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
            next_intent = (
                plan.intent
                if plan
                and plan.intent
                in {
                    "create_drive_project",
                    "project_snapshot",
                    "project_update_bundle",
                }
                else None
            )
            reply = AgentReply(
                tools.format_candidates(exc.candidates),
                reply_markup=tools.candidates_markup(
                    exc.candidates,
                    next_intent=next_intent,
                ),
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
    actor = identity_service.current_user()
    if (
        actor is not None
        and plan.mode in {"draft", "write", "conversation"}
        and not identity_service.can_write(actor)
    ):
        return AgentReply(
            "🔒 Роль Viewer позволяет только просматривать данные.",
            intent="permission_denied",
        )
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
        result = await digest.build_digest(
            db=db, telegram_user_id=telegram_user_id
        )
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

    if plan.intent == "ai_costs":
        summary = await ai_usage_service.usage_summary(db)
        return AgentReply(ai_usage_service.format_costs_report(summary), intent=plan.intent)

    if plan.intent == "drive_status":
        status = await drive_diagnostics.run_drive_status()
        return AgentReply(drive_diagnostics.format_drive_status(status), intent=plan.intent)

    if plan.intent in {
        "daily_plan",
        "project_inbox",
        "overdue_actions",
        "without_next_action",
        "waiting_client",
        "waiting_us",
        "stale_projects",
    }:
        inbox = await next_action_service.build_inbox(db)
        if plan.intent == "daily_plan":
            return AgentReply(
                next_action_service.format_plan(inbox),
                reply_markup=next_action_service.inbox_markup(inbox),
                intent=plan.intent,
            )
        if plan.intent == "project_inbox":
            return AgentReply(
                next_action_service.format_inbox(inbox),
                reply_markup=next_action_service.inbox_markup(inbox),
                intent=plan.intent,
            )
        section_map = {
            "overdue_actions": inbox.overdue,
            "without_next_action": inbox.without_next,
            "waiting_client": inbox.waiting_client,
            "waiting_us": inbox.waiting_us,
            "stale_projects": inbox.stale,
        }
        section = section_map[plan.intent]
        mini = next_action_service.InboxResult()
        setattr(
            mini,
            {
                "overdue_actions": "overdue",
                "without_next_action": "without_next",
                "waiting_client": "waiting_client",
                "waiting_us": "waiting_us",
                "stale_projects": "stale",
            }[plan.intent],
            section,
        )
        return AgentReply(
            next_action_service.format_inbox(mini),
            reply_markup=next_action_service.inbox_markup(mini),
            intent=plan.intent,
        )

    if plan.intent in {"integration_status", "failed_actions"}:
        ops = await outbox_service.list_failed(db)
        return AgentReply(outbox_service.format_integration_status(ops), intent=plan.intent)

    if plan.intent == "sheets_sync_preview":
        open_result = await kommo_service.get_all_open_leads()
        leads = (open_result.get("leads") or [])[:50]
        sheet_rows: list[dict] = []
        try:
            from app.services import google_sheets_service

            if hasattr(google_sheets_service, "list_registry_rows"):
                sheet_rows = await google_sheets_service.list_registry_rows()  # type: ignore[attr-defined]
            elif hasattr(google_sheets_service, "get_cached_rows"):
                sheet_rows = list(google_sheets_service.get_cached_rows() or [])
        except Exception as exc:
            logger.info("Sheets preview without live rows: %s", exc.__class__.__name__)
        preview = sheets_analytics_service.build_sheets_sync_preview(
            leads=leads, sheet_rows=sheet_rows
        )
        return AgentReply(
            sheets_analytics_service.format_sheets_preview(preview),
            intent=plan.intent,
            metadata={"assignments": len(preview.number_assignments)},
        )

    if plan.intent == "project_snapshot":
        lead = await _resolve_lead_for_plan(
            db, plan=plan, context=context, session=session
        )
        unified = await unified_project_service.build_unified_project(db, lead=lead)
        # Keep v4.2 snapshot markup compatibility for Notion/Drive buttons.
        snap = await project_snapshot.build_snapshot(db, lead=lead, context=context)
        return AgentReply(
            unified_project_service.format_unified_project(unified),
            reply_markup=project_snapshot.project_actions_markup(snap),
            intent=plan.intent,
            metadata={
                "lead_id": int(lead["id"]),
                "contact_source": (unified.primary_contact.source if unified.primary_contact else None),
                "phone_normalized": (
                    unified.primary_contact.phone_normalized if unified.primary_contact else None
                ),
            },
        )

    if plan.intent == "project_history":
        lead = await _resolve_lead_for_plan(
            db, plan=plan, context=context, session=session
        )
        events = await project_timeline_service.list_events(
            db, kommo_lead_id=int(lead["id"]), limit=5
        )
        internal = extract_internal_lead_number(lead)
        return AgentReply(
            project_timeline_service.format_history(
                events,
                kommo_lead_id=int(lead["id"]),
                internal_number=internal,
            ),
            reply_markup=project_timeline_service.history_markup(kommo_lead_id=int(lead["id"])),
            intent=plan.intent,
        )

    if plan.intent == "lead_assessment":
        lead = await _resolve_lead_for_plan(
            db, plan=plan, context=context, session=session
        )
        result = lead_assessment_service.assess_lead(lead)
        try:
            await lead_assessment_service.save_assessment(
                db, kommo_lead_id=int(lead["id"]), result=result
            )
        except Exception:
            await db.rollback()
        return AgentReply(
            lead_assessment_service.format_assessment(result, title=str(lead.get("name") or "")),
            intent=plan.intent,
        )

    if plan.intent == "search_project":
        search = await kommo_service.search_projects(str(plan.query or ""), limit=8)
        candidates = list(search.get("leads") or [])
        if not candidates:
            return AgentReply(
                "❓ Проект не найден. Укажи внутренний номер, имя, телефон, "
                "компанию или товар точнее.",
                intent=plan.intent,
            )
        if len(candidates) > 1:
            return AgentReply(
                tools.format_candidates(candidates),
                reply_markup=tools.candidates_markup(
                    candidates, next_intent="project_snapshot"
                ),
                intent="project_search_candidates",
                metadata={"query": plan.query, "count": len(candidates)},
            )
        lead = await kommo_service.get_lead_details(int(candidates[0]["id"]))
        await memory.set_active_lead(
            db,
            session=session,
            kommo_lead_id=int(lead["id"]),
            lead_name=str(lead.get("name") or ""),
        )
        snap = await project_snapshot.build_snapshot(db, lead=lead, context=context)
        return AgentReply(
            project_snapshot.format_snapshot(snap),
            reply_markup=project_snapshot.project_actions_markup(snap),
            intent="project_snapshot",
            metadata={"lead_id": int(lead["id"]), "query": plan.query},
        )

    if plan.intent == "project_update_bundle":
        lead = await _resolve_lead_for_plan(
            db, plan=plan, context=context, session=session
        )
        proposal = project_updates.analyse_update(text)
        followup_draft = None
        language_source = None
        client_id = None
        if proposal.should_prepare_followup:
            try:
                language = await client_language_service.resolve_communication_language(
                    db,
                    lead=lead,
                    explicit_language=(
                        plan.language if plan.language and plan.language != "auto" else None
                    ),
                )
                followup_draft = await generation.generate_draft(
                    kind="followup_message",
                    lead=tools.lead_summary_for_ai(lead),
                    language=language.language,
                    manager_request=(
                        "Подготовь короткий follow-up по этому обновлению проекта:\n"
                        + proposal.summary
                    ),
                )
                language_source = language.source
                client_id = language.client_id
            except Exception as exc:
                logger.info("Project update follow-up draft skipped: %s", exc)
        staged, group_id = await project_updates.stage_bundle(
            db,
            chat_id=chat_id,
            telegram_user_id=telegram_user_id,
            lead=lead,
            proposal=proposal,
            followup_draft=followup_draft,
            language_source=language_source,
            client_id=client_id,
        )
        return AgentReply(
            project_updates.format_bundle(
                lead=lead,
                proposal=proposal,
                actions_list=staged,
                followup_draft=followup_draft,
            ),
            reply_markup=project_updates.bundle_markup(staged, group_id),
            intent=plan.intent,
            metadata={
                "lead_id": int(lead["id"]),
                "batch_group_id": group_id,
                "action_ids": [int(action.id) for action in staged],
            },
        )

    if plan.intent == "create_drive_project":
        lead = await _resolve_lead_for_plan(
            db, plan=plan, context=context, session=session
        )
        preview_data = await project_drive.build_drive_project_preview(db, lead=lead)
        preview_text = project_drive.format_drive_project_preview(preview_data)
        action = await actions.stage_action(
            db,
            telegram_user_id=telegram_user_id,
            chat_id=chat_id,
            action_type="create_drive_project",
            payload=preview_data,
            preview_text=preview_text,
        )
        return AgentReply(
            preview_text,
            reply_markup=actions.approval_markup(action.id),
            intent=plan.intent,
            metadata={"action_id": action.id},
        )

    if plan.intent == "link_project_systems":
        lead = await _resolve_lead_for_plan(
            db, plan=plan, context=context, session=session
        )
        preview_data = await project_linking.build_link_preview(db, lead=lead)
        preview_text = project_linking.format_link_preview(preview_data)
        action = await actions.stage_action(
            db,
            telegram_user_id=telegram_user_id,
            chat_id=chat_id,
            action_type="link_project_systems",
            payload=preview_data,
            preview_text=preview_text,
        )
        return AgentReply(
            preview_text,
            reply_markup=actions.approval_markup(action.id),
            intent=plan.intent,
            metadata={"action_id": action.id},
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
        client_facing = str(plan.draft_kind or "") in {
            "commercial_offer",
            "followup_message",
            "email",
        }
        language_resolution = None
        resolved_language = plan.language
        if client_facing:
            language_resolution = (
                await client_language_service.resolve_communication_language(
                    db,
                    lead=lead,
                    explicit_language=plan.language,
                )
            )
            resolved_language = language_resolution.language
        draft = await generation.generate_draft(
            kind=str(plan.draft_kind or "followup_message"),
            lead=tools.lead_summary_for_ai(lead),
            language=resolved_language,
            manager_request=text,
        )
        if plan.draft_kind == "followup_message" and language_resolution is not None:
            message_record = await client_message_service.create_client_message_draft(
                db,
                telegram_user_id=telegram_user_id,
                lead=lead,
                draft=draft,
                language_source=language_resolution.source,
                client_id=language_resolution.client_id,
                channel="whatsapp",
            )
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
                    "last_client_message_draft_id": message_record.id,
                    "last_draft_created_at": datetime.now(timezone.utc).isoformat(),
                },
            )
            return AgentReply(
                client_message_service.format_client_message_draft(message_record),
                reply_markup=client_message_service.message_draft_markup(message_record),
                intent=plan.intent,
                metadata={
                    "lead_id": lead.get("id"),
                    "draft_kind": plan.draft_kind,
                    "client_message_draft_id": message_record.id,
                    "communication_language": message_record.communication_language,
                    "language_source": message_record.language_source,
                },
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
        structured = conversation_analysis_service.analyze_conversation_text(text)
        analysis = await ai_analysis_service.analyse_transcript(text)
        formatted = telegram_service.format_report(analysis, text)
        header = conversation_analysis_service.format_conversation_analysis(structured)
        return AgentReply(header + "\n\n" + formatted, intent=plan.intent)

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
        "create_drive_project",
        "save_file_to_drive_project",
        "link_project_systems",
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
            db,
            plan=plan,
            context=context,
            session=session,
            original_text=text,
            pre_resolved=pre_resolved_leads,
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
        if not calendar_policy.requires_calendar(
            event_type=plan.event_type, due_at=plan.due_at, title=plan.title
        ):
            # Ordinary follow-up without precise time → Kommo task, not Calendar.
            plan = AgentPlan(
                intent="create_kommo_task",
                mode="write",
                lead_id=plan.lead_id or (int(lead["id"]) if lead else None),
                query=plan.query,
                title=plan.title or "Follow-up",
                due_at=plan.due_at,
                clarification_question=None,
            )
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
                f"Задача: {html.escape(task_text)}\n\n"
                f"<i>{html.escape(calendar_policy.calendar_policy_reason(event_type=plan.event_type, due_at=plan.due_at))}</i>"
            )
            action_type = "create_kommo_task"
            payload = {
                "lead_id": int(lead["id"]),
                "task_text": task_text,
                "due_at": plan.due_at,
            }
        else:
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
                + f"\n\n<i>{html.escape(calendar_policy.calendar_policy_reason(event_type=plan.event_type, due_at=plan.due_at))}</i>"
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
    chat_id: int | None = None,
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
            lead_id = int(parts[2])
            next_intent = parts[3] if len(parts) >= 4 else None
            lead = await kommo_service.get_lead_details(lead_id)
            internal = extract_internal_lead_number(lead)
            await memory.set_active_lead(
                db,
                session=session,
                kommo_lead_id=lead_id,
                lead_name=str(lead.get("name") or ""),
            )
            if internal:
                await memory.update_context(
                    db, session=session, values={"active_internal_lead_number": internal}
                )
            if next_intent == "create_drive_project":
                if chat_id is None:
                    return AgentReply(
                        "❌ Не удалось продолжить создание проекта: отсутствует Telegram chat ID.",
                        intent="callback_error",
                    )
                return await _execute_plan(
                    db,
                    plan=AgentPlan(
                        intent="create_drive_project",
                        mode="write",
                        lead_id=lead_id,
                    ),
                    text="Создай проект в Drive",
                    chat_id=chat_id,
                    telegram_user_id=telegram_user_id,
                    source="callback",
                    context=context,
                    session=session,
                )
            if next_intent == "project_snapshot":
                unified = await unified_project_service.build_unified_project(db, lead=lead)
                snap = await project_snapshot.build_snapshot(
                    db, lead=lead, context=context
                )
                return AgentReply(
                    unified_project_service.format_unified_project(unified),
                    reply_markup=project_snapshot.project_actions_markup(snap),
                    intent="project_snapshot",
                    metadata={"lead_id": lead_id},
                )
            if next_intent == "project_update_bundle":
                return AgentReply(
                    "✅ Проект выбран. Повтори голосовое или текстовое обновление — "
                    "теперь бот применит его к этой карточке.",
                    intent="project_update_target_selected",
                    metadata={"lead_id": lead_id},
                )
            return AgentReply(
                tools.format_lead_summary(lead),
                reply_markup=tools.lead_card_actions_markup(lead),
                intent="lead_selected",
                metadata={"lead_id": lead_id},
            )
        except Exception as exc:
            logger.exception("Could not select Kommo lead from agent callback")
            return AgentReply(_error_text(exc), intent="lead_selection_failed")

    if command == "hist" and len(parts) >= 3 and parts[2].isdigit():
        lead_id = int(parts[2])
        event_filter = parts[3] if len(parts) >= 4 else "all"
        offset = int(parts[4]) if len(parts) >= 5 and parts[4].isdigit() else 0
        try:
            lead = await kommo_service.get_lead_details(lead_id)
            events = await project_timeline_service.list_events(
                db,
                kommo_lead_id=lead_id,
                event_filter=event_filter,
                limit=5,
                offset=offset,
            )
            return AgentReply(
                project_timeline_service.format_history(
                    events,
                    kommo_lead_id=lead_id,
                    internal_number=extract_internal_lead_number(lead),
                    event_filter=event_filter,
                    offset=offset,
                ),
                reply_markup=project_timeline_service.history_markup(
                    kommo_lead_id=lead_id, offset=offset
                ),
                intent="project_history",
            )
        except Exception as exc:
            return AgentReply(_error_text(exc), intent="history_failed")

    if command == "missing" and parts[2].isdigit():
        lead = await kommo_service.get_lead_details(int(parts[2]))
        unified = await unified_project_service.build_unified_project(db, lead=lead)
        lines = ["<b>⚠️ Что отсутствует</b>", ""]
        if unified.missing_information:
            lines.extend(f"• {html.escape(x)}" for x in unified.missing_information[:12])
        else:
            lines.append("Критических пробелов не найдено.")
        return AgentReply("\n".join(lines), intent="missing_information")

    if command == "next" and len(parts) >= 4 and parts[2].isdigit():
        lead_id = int(parts[2])
        choice = parts[3]
        label = dict(next_action_service.NEXT_ACTION_OPTIONS).get(choice, choice)
        return AgentReply(
            f"❓ Следующее действие: <b>{html.escape(label)}</b>\n"
            "Напиши срок, например: <i>завтра в 10:00</i>",
            intent="next_action_clarify",
            metadata={"lead_id": lead_id, "next_choice": choice},
        )

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

    if command == "project" and len(parts) >= 4 and parts[3].isdigit():
        project_command = parts[2]
        lead_id = int(parts[3])
        lead = await kommo_service.get_lead_details(lead_id)
        await memory.set_active_lead(
            db,
            session=session,
            kommo_lead_id=lead_id,
            lead_name=str(lead.get("name") or ""),
        )
        if project_command == "upload":
            return AgentReply(
                "📎 Отправь PDF, Excel, документ или фотографии одним сообщением.\n\n"
                "В подписи можно написать, например: "
                "<i>предложение производителя для проекта 134</i>. "
                "Бот предложит тип, имя и папку перед загрузкой.",
                intent="project_upload_prompt",
                metadata={"lead_id": lead_id},
            )
        if project_command == "history":
            return AgentReply(
                await project_snapshot.build_history(db, lead=lead),
                intent="project_history",
                metadata={"lead_id": lead_id},
            )
        if project_command == "status":
            statuses = await kommo_service.get_pipeline_statuses(
                int(lead.get("pipeline_id") or 0)
            )
            rows: list[list[dict[str, str]]] = []
            for status in statuses[:20]:
                status_id = status.get("id")
                if not isinstance(status_id, int):
                    continue
                name = str(status.get("name") or status_id)
                rows.append(
                    [
                        {
                            "text": name[:64],
                            "callback_data": f"agent:projectstatus:{lead_id}:{status_id}",
                        }
                    ]
                )
            if not rows:
                return AgentReply(
                    "❌ Не удалось получить этапы этой воронки.",
                    intent="project_status_unavailable",
                )
            return AgentReply(
                f"<b>Обновить статус проекта</b>\n\n"
                f"Сделка: {html.escape(str(lead.get('name') or lead_id))}\n"
                f"Текущий этап: {html.escape(str(lead.get('status_name') or '—'))}",
                reply_markup={"inline_keyboard": rows},
                intent="project_status_select",
                metadata={"lead_id": lead_id},
            )

    if (
        command == "projectstatus"
        and len(parts) >= 4
        and parts[2].isdigit()
        and parts[3].isdigit()
    ):
        lead_id = int(parts[2])
        status_id = int(parts[3])
        lead = await kommo_service.get_lead_details(lead_id)
        statuses = await kommo_service.get_pipeline_statuses(
            int(lead.get("pipeline_id") or 0)
        )
        target = next(
            (item for item in statuses if int(item.get("id") or 0) == status_id),
            None,
        )
        if target is None:
            return AgentReply(
                "❌ Такой этап не найден в воронке сделки.",
                intent="project_status_invalid",
            )
        preview = (
            "<b>Подтвердить новый статус?</b>\n\n"
            f"Сделка: {html.escape(str(lead.get('name') or lead_id))}\n"
            f"Было: {html.escape(str(lead.get('status_name') or '—'))}\n"
            f"Станет: <b>{html.escape(str(target.get('name') or status_id))}</b>"
        )
        action = await actions.stage_action(
            db,
            telegram_user_id=telegram_user_id,
            chat_id=int(chat_id or 0),
            action_type="update_kommo_lead",
            payload={
                "lead_id": lead_id,
                "kommo_lead_id": lead_id,
                "fields": {"status_id": status_id},
            },
            preview_text=preview,
        )
        return AgentReply(
            preview,
            reply_markup=actions.approval_markup(action.id),
            intent="project_status_confirmation",
            metadata={"lead_id": lead_id, "action_id": int(action.id)},
        )

    if command == "bundle" and len(parts) >= 4:
        group_id = parts[2]
        choice = parts[3]
        actor = identity_service.current_user()
        if actor is not None and not identity_service.can_write(actor):
            return AgentReply(
                "🔒 Роль Viewer не позволяет подтверждать изменения.",
                intent="permission_denied",
            )
        batch = await actions.get_batch_actions(
            db,
            batch_group_id=group_id,
            telegram_user_id=telegram_user_id,
        )
        if not batch:
            return AgentReply(
                "⌛ Пакет не найден или принадлежит другому пользователю.",
                intent="bundle_missing",
            )
        if choice == "no":
            for action in batch:
                await actions.reject_action(
                    db,
                    action=action,
                    telegram_user_id=telegram_user_id,
                )
            return AgentReply(
                "❌ Пакет отменён. Внешние сервисы не изменены.",
                intent="bundle_rejected",
            )
        if choice == "all":
            lines = ["<b>Результат пакетного обновления</b>", ""]
            success = 0
            failed = 0
            for action in batch:
                try:
                    result_text = await execute_action(
                        db,
                        action=action,
                        telegram_user_id=telegram_user_id,
                    )
                    if result_text.startswith("❌"):
                        failed += 1
                    else:
                        success += 1
                    lines.append(
                        f"{'✅' if not result_text.startswith('❌') else '❌'} "
                        f"{html.escape(action.action_type)}"
                    )
                except Exception as exc:
                    failed += 1
                    lines.append(
                        f"❌ {html.escape(action.action_type)} — "
                        f"{html.escape(str(exc)[:180])}"
                    )
            lines.extend(["", f"Выполнено: {success} · Ошибок: {failed}"])
            reply_markup = None
            followup_action = next(
                (
                    item
                    for item in batch
                    if item.action_type == "prepare_client_followup"
                    and isinstance(item.result, dict)
                    and item.result.get("client_message_draft_id")
                ),
                None,
            )
            if followup_action is not None:
                record = await client_message_service.get_draft(
                    db, int(followup_action.result["client_message_draft_id"])
                )
                if record is not None:
                    lines.extend(
                        [
                            "",
                            "Follow-up подготовлен. Открой WhatsApp кнопкой ниже.",
                        ]
                    )
                    reply_markup = client_message_service.message_draft_markup(record)
            return AgentReply(
                "\n".join(lines),
                reply_markup=reply_markup,
                intent="bundle_executed" if not failed else "bundle_partial",
                metadata={"batch_group_id": group_id, "success": success, "failed": failed},
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
            if action.action_type == "save_file_to_drive_project":
                artifact_id = (action.payload or {}).get("artifact_id")
                if artifact_id:
                    artifact = await project_artifact_service.get_artifact(
                        db, int(artifact_id)
                    )
                    if artifact is not None and artifact.status == "pending":
                        storage_path = artifact.storage_path
                        await project_artifact_service.mark_cancelled(
                            db, artifact=artifact
                        )
                        if storage_path:
                            try:
                                await asyncio.to_thread(
                                    storage_service.delete_project_file,
                                    str(storage_path),
                                )
                            except Exception as exc:
                                logger.warning(
                                    "Could not clean rejected project file %s: %s",
                                    artifact.id,
                                    exc,
                                )
            return AgentReply("❌ Действие отменено. Внешние сервисы не изменены.", intent="action_rejected")
        actor = identity_service.current_user()
        if actor is not None and not identity_service.can_write(actor):
            return AgentReply(
                "🔒 Роль Viewer не позволяет подтверждать изменения.",
                intent="permission_denied",
            )
        try:
            text = await execute_action(
                db, action=action, telegram_user_id=telegram_user_id
            )
            if (
                action.action_type == "prepare_client_followup"
                and isinstance(action.result, dict)
                and action.result.get("client_message_draft_id")
            ):
                record = await client_message_service.get_draft(
                    db, int(action.result["client_message_draft_id"])
                )
                if record is not None:
                    return AgentReply(
                        client_message_service.format_client_message_draft(record),
                        reply_markup=client_message_service.message_draft_markup(record),
                        intent="action_executed",
                    )
            return AgentReply(text, intent="action_executed")
        except Exception as exc:
            logger.exception("Confirmed agent action failed")
            return AgentReply(_error_text(exc), intent="action_failed")

    return AgentReply("❌ Некорректная команда агента.", intent="callback_error")


async def handle_project_file_upload(
    db: AsyncSession,
    *,
    chat_id: int,
    telegram_user_id: int,
    telegram_message_id: int | None = None,
    filename: str,
    mime_type: str,
    content: bytes,
    caption: str | None = None,
    kind: str | None = None,
) -> AgentReply:
    if telegram_message_id is not None:
        existing = await project_artifact_service.get_by_telegram_message(
            db,
            telegram_user_id=telegram_user_id,
            telegram_message_id=telegram_message_id,
        )
        if existing is not None:
            if existing.status in {"uploaded", "uploaded_with_warnings"}:
                link = (
                    f'\n<a href="{html.escape(str(existing.drive_file_url), quote=True)}">'
                    "Открыть файл</a>"
                    if existing.drive_file_url
                    else ""
                )
                return AgentReply(
                    "ℹ️ Этот Telegram-файл уже обработан.\n\n"
                    f"Статус: <b>{html.escape(existing.status)}</b>"
                    + link,
                    intent="file_upload_duplicate",
                    metadata={"artifact_id": int(existing.id)},
                )
            if existing.status != "pending" or not existing.storage_path:
                return AgentReply(
                    "ℹ️ Этот Telegram-файл уже был обработан со статусом "
                    f"<b>{html.escape(existing.status)}</b>. "
                    "Если нужен новый запуск, отправь файл ещё раз.",
                    intent="file_upload_duplicate",
                    metadata={"artifact_id": int(existing.id)},
                )
            payload = {
                "kommo_lead_id": int(existing.kommo_lead_id),
                "project_key": (
                    existing.metadata_json or {}
                ).get("project_key"),
                "artifact_id": int(existing.id),
                "filename": existing.suggested_filename,
                "mime_type": existing.mime_type,
                "storage_path": existing.storage_path,
                "subfolder_name": existing.subfolder_name,
                "artifact_type": existing.artifact_type,
                "artifact_type_label": existing.artifact_type_label,
            }
            action = await actions.stage_action(
                db,
                telegram_user_id=telegram_user_id,
                chat_id=chat_id,
                action_type="save_file_to_drive_project",
                payload=payload,
                preview_text=existing.preview_text or "Подтвердить загрузку файла?",
            )
            return AgentReply(
                existing.preview_text or "Подтвердить загрузку файла?",
                reply_markup=actions.approval_markup(action.id),
                intent="file_upload_duplicate_pending",
                metadata={
                    "action_id": int(action.id),
                    "artifact_id": int(existing.id),
                },
            )

    session = await memory.get_or_create_session(db, telegram_user_id=telegram_user_id)
    context = await memory.build_context(
        db, telegram_user_id=telegram_user_id, session=session
    )
    active_id = context.get("active_kommo_lead_id")
    explicit_project = re.search(
        r"\bпроект[ауе]?\s*[№#]?\s*(\d{1,4})\b",
        caption or "",
        flags=re.I,
    )
    if explicit_project:
        try:
            resolved = await tools.resolve_lead(
                lead_id=None,
                query=explicit_project.group(1),
                context=context,
            )
            active_id = int(resolved["id"])
        except tools.LeadResolutionError as exc:
            return AgentReply(
                f"❓ Не удалось определить проект из подписи: {html.escape(str(exc))}",
                reply_markup=(
                    tools.candidates_markup(
                        exc.candidates, next_intent="project_snapshot"
                    )
                    if exc.candidates
                    else None
                ),
                intent="file_upload_project_clarification",
            )
    if not active_id:
        return AgentReply(
            f"❓ Для какого проекта сохранить файл? {user_error_hint()}",
            intent="file_upload_clarification",
        )
    lead = await kommo_service.get_lead_details(int(active_id))
    from app.services import project_link_service

    link = await project_link_service.get_by_kommo_lead_id(db, int(active_id))
    if not link or not link.drive_folder_id:
        return AgentReply(
            "❓ Сначала создай проект в Google Drive для этой сделки.\n"
            "Например: <i>создай проект в drive по этой сделке</i>",
            intent="file_upload_missing_project",
        )
    classification = project_artifact_service.classify_artifact(
        filename=filename,
        mime_type=mime_type,
        caption=caption,
        kind=kind,
    )
    safe_name = project_artifact_service.suggested_filename(
        project_key=str(link.project_key),
        classification=classification,
        original_filename=filename,
    )
    storage_path = await storage_service.save_project_file(
        content, safe_name, mime_type
    )
    preview = (
        "<b>📎 Умная загрузка файла</b>\n\n"
        f"Проект: <code>{html.escape(str(link.project_key))}</code>\n"
        f"Сделка: <b>{html.escape(str(lead.get('name') or '—'))}</b>\n"
        f"Определённый тип: <b>{html.escape(classification.label)}</b>\n"
        f"Исходное имя: {html.escape(sanitize_filename(filename))}\n"
        f"Новое имя: <b>{html.escape(safe_name)}</b>\n"
        f"Папка: {html.escape(classification.subfolder_name)}\n"
        f"Размер: {len(content) / 1024:.1f} КБ\n\n"
        "После подтверждения бот загрузит файл, добавит запись в Notion, "
        "заметку в Kommo и сохранит аудит."
    )
    try:
        artifact = await project_artifact_service.create_pending(
            db,
            link=link,
            telegram_user_id=telegram_user_id,
            telegram_message_id=telegram_message_id,
            original_filename=filename,
            suggested_name=safe_name,
            mime_type=mime_type,
            file_size=len(content),
            classification=classification,
            caption=caption,
            preview_text=preview,
            storage_path=storage_path,
            metadata={"telegram_kind": kind, "project_key": link.project_key},
        )
    except Exception:
        try:
            await asyncio.to_thread(
                storage_service.delete_project_file,
                str(storage_path),
            )
        except Exception as cleanup_exc:
            logger.warning(
                "Could not clean project file after audit staging failure: %s",
                cleanup_exc,
            )
        raise
    payload = {
        "kommo_lead_id": int(active_id),
        "project_key": link.project_key,
        "artifact_id": int(artifact.id),
        "filename": safe_name,
        "mime_type": mime_type,
        "storage_path": storage_path,
        "subfolder_name": classification.subfolder_name,
        "artifact_type": classification.artifact_type,
        "artifact_type_label": classification.label,
    }
    action = await actions.stage_action(
        db,
        telegram_user_id=telegram_user_id,
        chat_id=chat_id,
        action_type="save_file_to_drive_project",
        payload=payload,
        preview_text=preview,
    )
    return AgentReply(
        preview,
        reply_markup=actions.approval_markup(action.id),
        intent="save_file_to_drive_project",
        metadata={"action_id": action.id, "artifact_id": int(artifact.id)},
    )
