"""Production smoke-test fixes for the B&BS operator experience.

This module intentionally keeps the fixes isolated and installable at startup:
- activate the row-number lead registry policy and add lead-field fallback matching;
- rank real obligations above projects where we are simply waiting for a client;
- return one consistent full project card from all lead/project entry points;
- detect natural spoken project updates such as "я поговорил с клиентом по кормушкам";
- correct stale Russian language profiles for clearly Polish leads;
- generate useful iPhone contact names with client + product/project context.
"""
from __future__ import annotations

import html
import re
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select

from app.agent import digest, planner, project_snapshot, service as agent_service
from app.agent.contracts import AgentPlan, AgentReply
from app.database import AsyncSessionLocal
from app.models.agent_v5 import NextActionState
from app.models.client import Client
from app.services import (
    client_language_service,
    client_message_service,
    contact_resolver,
    kommo_service,
    lead_registry_runtime,
    runtime_extensions,
    telegram_service,
)

_INSTALLED = False


def _clean(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()


def _digits(value: Any) -> str:
    return re.sub(r"\D", "", str(value or ""))


def _email(value: Any) -> str:
    return str(value or "").strip().casefold()


def _custom_values(entity: dict[str, Any]) -> list[tuple[str, str]]:
    values: list[tuple[str, str]] = []
    fields = entity.get("custom_fields") or entity.get("custom_fields_values") or []
    if isinstance(fields, dict):
        for key, value in fields.items():
            values.append((str(key), str(value or "")))
        return values
    for field in fields:
        marker = " ".join(
            str(field.get(key) or "")
            for key in ("name", "code", "field_name", "field_code")
        ).casefold()
        raw_values = field.get("values")
        if isinstance(raw_values, list):
            for item in raw_values:
                values.append((marker, str((item or {}).get("value") or "")))
        elif field.get("value") not in (None, ""):
            values.append((marker, str(field.get("value") or "")))
    return values


def _lead_exactly_matches_row(details: dict[str, Any], row: Any) -> bool:
    wanted_phone = _digits(getattr(row, "phone", None))
    wanted_email = _email(getattr(row, "email", None))
    phones: set[str] = set()
    emails: set[str] = set()

    for contact in details.get("contacts") or []:
        phones.update(_digits(value) for value in contact.get("phones") or [] if _digits(value))
        emails.update(_email(value) for value in contact.get("emails") or [] if _email(value))
        for marker, value in _custom_values(contact):
            if "phone" in marker or "telefon" in marker or "numer" in marker or "телефон" in marker:
                if _digits(value):
                    phones.add(_digits(value))
            if "email" in marker or "e-mail" in marker or "почт" in marker:
                if _email(value):
                    emails.add(_email(value))

    for marker, value in _custom_values(details):
        marker_folded = marker.casefold()
        if any(token in marker_folded for token in ("phone", "telefon", "numer", "телефон", "номер")):
            if _digits(value):
                phones.add(_digits(value))
        if any(token in marker_folded for token in ("email", "e-mail", "почт")):
            if _email(value):
                emails.add(_email(value))

    return bool((wanted_phone and wanted_phone in phones) or (wanted_email and wanted_email in emails))


async def _find_leads_by_lead_fields(row: Any) -> list[dict[str, Any]]:
    """Fallback for Meta forms that store phone/email on the lead, not contact."""
    found: dict[int, dict[str, Any]] = {}
    for query in (getattr(row, "phone", None), getattr(row, "email", None)):
        query = _clean(query)
        if not query:
            continue
        try:
            data = await kommo_service._request(
                "GET",
                "/api/v4/leads",
                params={"query": query, "with": "contacts", "limit": 50},
            )
        except Exception:
            continue
        for item in kommo_service._extract_embedded_items(data, "leads"):
            lead_id = item.get("id")
            if not isinstance(lead_id, int) or lead_id in found:
                continue
            try:
                details = await kommo_service.get_lead_details(lead_id)
            except Exception:
                continue
            if _lead_exactly_matches_row(details, row):
                found[lead_id] = lead_registry_runtime._lead_from_details(details)
    return list(found.values())


def _conversation_update_query(text: str) -> str | None:
    normalized = planner._normalize(text)
    if not any(
        token in normalized
        for token in ("поговорил", "поговорили", "обсудил", "созвонил", "договорились")
    ):
        return None
    if not any(token in normalized for token in ("клиент", "заказчик", "покупатель")):
        return None
    match = re.search(
        r"(?:клиент(?:ом|у|а)?|заказчик(?:ом|у|а)?|покупатель(?:ем|ю|я)?)\s+по\s+([^.,;!?\n]{3,100})",
        normalized,
    )
    if not match:
        return None
    query = match.group(1).strip(" -—")
    query = re.sub(r"\b(?:он|она|они)\b.*$", "", query).strip()
    return query or None


def _state_priority(
    item: dict[str, Any], state: NextActionState | None, *, now: datetime
) -> dict[str, Any]:
    result = dict(item)
    if state is None:
        if str(result.get("reason") or "") == "нет следующей задачи":
            result.update(
                score=35,
                priority="Низкий",
                reason="следующий шаг не зафиксирован",
                next_step="Уточнить статус при следующей работе с проектом",
                actionable=False,
            )
        else:
            result["actionable"] = int(result.get("score") or 0) >= 55
        return result

    due = state.due_at
    if due is not None and due.tzinfo is None:
        due = due.replace(tzinfo=timezone.utc)
    waiting_on = str(state.waiting_on or "").casefold()
    action_text = _clean(state.action_text)

    if waiting_on == "client":
        if due is not None and due <= now:
            result.update(
                score=72,
                priority="Средний",
                reason="наступил срок проверить ответ клиента",
                next_step=action_text or "Проверить ответ и при необходимости подготовить follow-up",
                actionable=True,
            )
        else:
            result.update(
                score=8,
                priority="Низкий",
                reason="ждём ответ клиента",
                next_step=(
                    action_text
                    or (
                        f"Не беспокоить до {due.astimezone().strftime('%d.%m %H:%M')}"
                        if due is not None
                        else "Контролировать только после согласованного срока"
                    )
                ),
                actionable=False,
            )
        return result

    if waiting_on == "us":
        result.update(
            score=110,
            priority="Высокий",
            reason="клиент ждёт наш ответ",
            next_step=action_text or "Ответить клиенту сегодня",
            actionable=True,
        )
        return result

    if action_text:
        if due is not None and due <= now:
            result.update(
                score=105,
                priority="Высокий",
                reason="просрочено обещанное действие",
                next_step=action_text,
                actionable=True,
            )
        elif due is not None:
            hours = max(0, int((due - now).total_seconds() // 3600))
            result.update(
                score=75 if hours <= 24 else 48,
                priority="Средний" if hours <= 24 else "Низкий",
                reason="есть зафиксированное следующее действие",
                next_step=action_text,
                actionable=hours <= 24,
            )
        else:
            result.update(
                score=45,
                priority="Низкий",
                reason="следующее действие зафиксировано без срока",
                next_step=action_text,
                actionable=False,
            )
    return result


def _friendly_discrepancy(value: Any) -> str:
    text = _clean(value)
    text = text.replace("нет ProjectLink", "нет связки проекта с Notion/Drive")
    text = re.sub(r"нет Drive/Notion", "не подключены Drive и Notion", text)
    text = re.sub(r"нет Notion/Drive", "не подключены Notion и Drive", text)
    text = re.sub(r"нет Drive", "не подключён Drive", text)
    text = re.sub(r"нет Notion", "не подключён Notion", text)
    return text


async def _full_project_reply(
    db: Any, lead_id: int, *, intent: str, metadata: dict[str, Any] | None = None
) -> AgentReply:
    lead = await kommo_service.get_lead_details(int(lead_id))
    snapshot = await project_snapshot.build_snapshot(db, lead=lead, context={})
    payload = dict(metadata or {})
    payload["lead_id"] = int(lead_id)
    return AgentReply(
        project_snapshot.format_snapshot(snapshot),
        reply_markup=project_snapshot.project_actions_markup(snapshot),
        intent=intent,
        metadata=payload,
    )


def _project_product(lead: dict[str, Any]) -> str:
    name = _clean(lead.get("name"))
    return re.sub(r"^\s*\d+\s*(?:[-–—]\s*|\s+)", "", name).strip() or "проект"


def _enhanced_vcard(record: Any, lead: dict[str, Any]) -> tuple[str, bytes]:
    resolved = contact_resolver.resolve_contact(lead)
    name = _clean(resolved.name or getattr(record, "client_name", None) or "Клиент")
    product = _project_product(lead)
    internal = re.match(r"^\s*(\d+)", _clean(lead.get("name")))
    project_label = f"№{internal.group(1)}" if internal else f"Kommo {lead.get('id')}"
    display_name = f"{name} — {product}"[:180]
    content = client_message_service.build_vcard(
        name=display_name,
        company=getattr(record, "company", None),
        phone=getattr(record, "recipient", None) or resolved.phone_normalized,
        email=resolved.email,
        language=getattr(record, "communication_language", None),
    ).decode("utf-8")
    note = client_message_service._vcard_escape(
        f"B&BS: {project_label}; товар: {product}; Kommo: {lead.get('url') or ''}"
    )
    content = content.replace(
        "NOTE:Contact prepared by Buy & Bring Solutions",
        f"NOTE:{note}",
    )
    return display_name, content.encode("utf-8")


def install_operator_experience_runtime() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True

    original_direct_lookup = lead_registry_runtime._find_exact_contact_leads

    async def direct_lookup_with_lead_fields(row: Any) -> list[dict[str, Any]]:
        candidates = await original_direct_lookup(row)
        by_id = {
            int(item.get("id") or 0): item
            for item in candidates
            if int(item.get("id") or 0) > 0
        }
        for item in await _find_leads_by_lead_fields(row):
            lead_id = int(item.get("id") or 0)
            if lead_id:
                by_id[lead_id] = item
        return list(by_id.values())

    lead_registry_runtime._find_exact_contact_leads = direct_lookup_with_lead_fields

    original_deterministic_plan = planner.deterministic_plan

    def deterministic_plan_with_natural_updates(
        text: str, context: dict[str, Any]
    ) -> AgentPlan | None:
        query = _conversation_update_query(text)
        if query:
            lead_id, explicit_query = planner._lead_reference(text, context)
            return AgentPlan(
                intent="project_update_bundle",
                mode="write",
                confidence=0.99,
                lead_id=lead_id or context.get("active_kommo_lead_id"),
                query=explicit_query or query,
                lead_refs=planner._plan_lead_refs(text, context),
                body=text,
            )
        return original_deterministic_plan(text, context)

    planner.deterministic_plan = deterministic_plan_with_natural_updates

    original_resolve_language = client_language_service.resolve_communication_language
    original_read_language = client_language_service.read_communication_language

    async def resolve_language_with_market_guard(*args: Any, **kwargs: Any) -> Any:
        resolution = await original_resolve_language(*args, **kwargs)
        lead = kwargs.get("lead") or (args[1] if len(args) > 1 else {})
        market = client_language_service.infer_direction_language(lead or {})
        protected = {"explicit_request", "manager_selected", "kommo_client_card"}
        if (
            market
            and market[0] == "pl"
            and resolution.language == "ru"
            and resolution.source not in protected
        ):
            db = kwargs.get("db") or (args[0] if args else None)
            if db is not None and resolution.client_id:
                client = await db.get(Client, int(resolution.client_id))
                if client is not None:
                    client.communication_language = "pl"
                    client.communication_language_source = "market_correction"
                    client.communication_language_confidence = market[1]
                    client.communication_language_updated_at = datetime.now(timezone.utc)
                    await db.commit()
            return client_language_service.LanguageResolution(
                "pl", "market_correction", market[1], resolution.client_id
            )
        return resolution

    async def read_language_with_market_guard(*args: Any, **kwargs: Any) -> Any:
        resolution = await original_read_language(*args, **kwargs)
        lead = kwargs.get("lead") or (args[1] if len(args) > 1 else {})
        market = client_language_service.infer_direction_language(lead or {})
        if market and market[0] == "pl" and resolution.language == "ru":
            return client_language_service.LanguageResolution(
                "pl", "market_correction", market[1], resolution.client_id
            )
        return resolution

    client_language_service.resolve_communication_language = resolve_language_with_market_guard
    client_language_service.read_communication_language = read_language_with_market_guard

    original_build_digest = digest.build_digest

    async def build_digest_with_real_obligations(*args: Any, **kwargs: Any) -> dict[str, Any]:
        result = await original_build_digest(*args, **kwargs)
        db = kwargs.get("db")
        if db is None and args:
            db = args[0]
        if db is None:
            return result
        digest_map = [dict(item) for item in result.get("digest_map") or []]
        lead_ids = [
            int(item.get("kommo_lead_id") or 0)
            for item in digest_map
            if int(item.get("kommo_lead_id") or 0) > 0
        ]
        states: dict[int, NextActionState] = {}
        if lead_ids:
            rows = (
                await db.execute(
                    select(NextActionState).where(
                        NextActionState.kommo_lead_id.in_(lead_ids)
                    )
                )
            ).scalars().all()
            states = {int(row.kommo_lead_id): row for row in rows}
        now = datetime.now(timezone.utc)
        reranked = [
            _state_priority(
                item,
                states.get(int(item.get("kommo_lead_id") or 0)),
                now=now,
            )
            for item in digest_map
        ]
        reranked.sort(
            key=lambda item: (
                -int(item.get("score") or 0),
                str(item.get("name") or ""),
            )
        )
        for index, item in enumerate(reranked, 1):
            item["position"] = index
        actionable = [item for item in reranked if item.get("actionable")]
        result["digest_map"] = reranked
        result["sections"] = digest.group_digest_sections(reranked)
        result["top_actions"] = (actionable[:5] or reranked[:5])
        health = dict(result.get("health") or {})
        health["discrepancy_examples"] = [
            _friendly_discrepancy(item)
            for item in health.get("discrepancy_examples") or []
        ]
        waiting_count = sum(
            1
            for state in states.values()
            if str(state.waiting_on or "").casefold() == "client"
            or _clean(state.action_text)
        )
        health["without_next_step"] = max(
            0, int(health.get("without_next_step") or 0) - waiting_count
        )
        result["health"] = health
        return result

    digest.build_digest = build_digest_with_real_obligations

    original_handle_message = agent_service.handle_message
    original_handle_callback = agent_service.handle_callback

    async def handle_message_with_one_project_card(*args: Any, **kwargs: Any) -> AgentReply:
        reply = await original_handle_message(*args, **kwargs)
        lead_id = int((reply.metadata or {}).get("lead_id") or 0)
        if lead_id and reply.intent in {
            "search_lead",
            "lead_summary",
            "project_snapshot",
            "project_search",
        }:
            db = kwargs.get("db") or (args[0] if args else None)
            return await _full_project_reply(
                db, lead_id, intent="project_snapshot", metadata=reply.metadata
            )
        return reply

    async def handle_callback_with_one_project_card(*args: Any, **kwargs: Any) -> AgentReply | None:
        reply = await original_handle_callback(*args, **kwargs)
        if reply is None:
            return None
        lead_id = int((reply.metadata or {}).get("lead_id") or 0)
        if lead_id and reply.intent in {
            "lead_selected",
            "digest_lead_selected",
            "project_snapshot",
        }:
            db = kwargs.get("db") or (args[0] if args else None)
            return await _full_project_reply(
                db, lead_id, intent="project_snapshot", metadata=reply.metadata
            )
        return reply

    agent_service.handle_message = handle_message_with_one_project_card
    agent_service.handle_callback = handle_callback_with_one_project_card

    from app.api import telegram as telegram_api

    async def show_lead_details_as_project(
        chat_id: int, lead_id: int, *, return_page: int = 1
    ) -> None:
        async with AsyncSessionLocal() as db:
            reply = await _full_project_reply(
                db,
                int(lead_id),
                intent="project_snapshot",
                metadata={"return_page": return_page},
            )
            await telegram_service.send_message(
                chat_id, reply.text, reply_markup=reply.reply_markup
            )

    telegram_api._show_lead_details = show_lead_details_as_project

    original_client_callback = telegram_api._handle_client_message_callback

    async def client_callback_with_descriptive_vcard(**kwargs: Any) -> bool:
        callback_data = str(kwargs.get("callback_data") or "")
        if not callback_data.startswith("clientmsg:vcf:"):
            return await original_client_callback(**kwargs)
        draft_id = int(callback_data.rsplit(":", 1)[1])
        db = kwargs["db"]
        record = await client_message_service.get_draft(db, draft_id)
        if record is None:
            raise ValueError("Черновик не найден.")
        lead = await kommo_service.get_lead_details(int(record.kommo_lead_id))
        display_name, content = _enhanced_vcard(record, lead)
        await telegram_service.send_document(
            int(kwargs["chat_id"]),
            filename=client_message_service.vcard_filename(display_name),
            content=content,
            caption=(
                "👤 Контакт для iPhone: "
                f"<b>{html.escape(display_name)}</b>. Номер проекта и ссылка Kommo сохранены в заметке контакта."
            ),
            mime_type="text/vcard",
        )
        return True

    telegram_api._handle_client_message_callback = client_callback_with_descriptive_vcard

    def clearer_chat_section(chat: dict[str, Any]) -> str:
        if chat and not chat.get("available") and chat.get("reason") == "external_chat_history_scope_required":
            return (
                "<b>Переписка Kommo:</b> текущий API-токен не имеет доступа к raw chat history "
                "(<code>External chat history</code>). Заметки Kommo и сообщения прямого WhatsApp API "
                "продолжают отображаться."
            )
        return runtime_extensions_original_chat_section(chat)

    runtime_extensions_original_chat_section = runtime_extensions._chat_section
    runtime_extensions._chat_section = clearer_chat_section
