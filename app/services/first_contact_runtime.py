"""Dedicated first-contact WhatsApp workflow for Kommo lead cards.

The existing Follow-up action intentionally remains conversation-aware.  This
runtime adds a separate first-contact action that uses the lead card as its source
of truth and must never imply that the manager has already spoken with the client.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any

from openai import AsyncOpenAI

from app.agent import generation, memory, project_snapshot, service as agent_service, tools
from app.agent.contracts import AgentReply
from app.agent.retrying import retry, stop_after_attempt, wait_exponential
from app.config import get_settings
from app.services import (
    client_language_service,
    client_message_service,
    communication_context_service,
    communication_example_service,
    kommo_service,
    message_review_service,
    telegram_service,
    unified_project_service,
)

logger = logging.getLogger(__name__)
settings = get_settings()
FIRST_CONTACT_KIND = "first_contact_message"
_INSTALLED = False

_PRIOR_CONTACT_PATTERNS = (
    r"\bdziękuj(?:ę|emy) za (?:dzisiejszą |ostatnią )?(?:rozmowę|kontakt)\b",
    r"\bjak (?:ustaliliśmy|rozmawialiśmy|omawialiśmy)\b",
    r"\bw nawiązaniu do (?:naszej|ostatniej|wcześniejszej)\b",
    r"\bwracam do (?:tematu|rozmowy|wiadomości)\b",
    r"\bponownie (?:piszę|kontaktuję się|wracam)\b",
    r"\bprzypominam się\b",
    r"\bfollow[- ]?up\b",
    r"\bспасибо за (?:сегодняшний |предыдущий )?(?:разговор|созвон|общение)\b",
    r"\bкак (?:мы )?(?:обсуждали|договаривались|согласовали)\b",
    r"\bвозвращаюсь к (?:нашему )?(?:разговору|вопросу|теме)\b",
    r"\bнапоминаю о\b",
    r"\bдякую за (?:сьогоднішню |попередню )?(?:розмову|спілкування)\b",
    r"\bяк (?:ми )?(?:обговорювали|домовлялися|узгодили)\b",
    r"\bповертаюся до (?:нашої )?(?:розмови|питання|теми)\b",
    r"\bthank you for (?:today(?:'s)? |our )?(?:call|conversation)\b",
    r"\bas (?:we )?(?:discussed|agreed)\b",
    r"\bfollowing up on\b",
)


def _first_contact_issues(body: str) -> list[str]:
    clean = str(body or "").strip()
    issues = list(
        message_review_service.deterministic_review(
            body=clean,
            kind=FIRST_CONTACT_KIND,
            language="auto",
            playbook=None,
        ).get("issues")
        or []
    )
    lowered = clean.casefold()
    if any(re.search(pattern, lowered, flags=re.IGNORECASE) for pattern in _PRIOR_CONTACT_PATTERNS):
        issues.append("Текст ошибочно подразумевает предыдущий контакт с клиентом.")
    if len(clean) > 1800:
        issues.append("Первое WhatsApp-сообщение слишком длинное.")
    question_count = clean.count("?") + clean.count("？")
    if question_count > 6:
        issues.append("В первом сообщении задано больше шести вопросов.")
    return list(dict.fromkeys(str(item) for item in issues if str(item).strip()))


def _first_contact_context(lead: dict[str, Any], manager_request: str) -> dict[str, Any]:
    context = communication_context_service.build_communication_context(
        lead,
        manager_request=manager_request,
        max_messages=30,
    )
    # The action has explicit first-contact semantics.  Historical notes may still
    # be present in Kommo, but they must not make the generated message sound like
    # a follow-up.
    context["conversation"] = {
        "available": False,
        "origin": None,
        "last_messages": [],
        "last_client_message": "",
        "last_manager_message": "",
        "waiting_on": None,
        "client_tone": "unknown",
        "open_questions": [],
        "promises_made": [],
        "next_expected_action": None,
        "source_summary": None,
        "ignored_for_first_contact": True,
    }
    context["interaction_mode"] = "first_contact"
    return context


def _first_contact_playbook(language: str) -> dict[str, Any]:
    playbook = communication_context_service.playbook_for_prompt(
        kind=FIRST_CONTACT_KIND,
        language=language,
        channel="whatsapp",
    )
    playbook = copy.deepcopy(playbook)
    existing_rules = [
        str(rule)
        for rule in (playbook.get("global_rules") or [])
        if not any(
            token in str(rule).casefold()
            for token in ("continue the existing", "latest conversation", "starting again")
        )
    ]
    playbook["global_rules"] = [
        "This is the first outbound contact. Never imply a prior call, chat, agreement or reminder.",
        "Use only confirmed Kommo/card facts and the manager request.",
        "Keep WhatsApp concise, natural and focused on establishing contact and qualifying the request.",
        *existing_rules,
    ]
    playbook["kind_rules"] = [
        "Address the client by name when it is available.",
        "Briefly identify Buy & Bring Solutions and why the message is relevant to the submitted inquiry.",
        "Mention the product/category from the lead form without broadening or inventing it.",
        "Ask three to six concise qualification points: exact product/photo/link/specification, quantity, timing, budget or current purchase context, and delivery location when still unknown.",
        "Do not say thank you for a conversation, as agreed, following up, returning to the topic, again or reminder.",
        "Do not use placeholders. Do not claim that sourcing, checks, prices or factory work have already started.",
    ]
    return playbook


def _reviewer_enabled() -> bool:
    return os.getenv("AGENT_MESSAGE_REVIEWER_ENABLED", "true").strip().casefold() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _reviewer_model() -> str:
    return (
        os.getenv("AGENT_REVIEWER_MODEL", "").strip()
        or settings.agent_planner_model.strip()
        or settings.agent_writer_model.strip()
        or settings.openai_model
    )


async def _review_first_contact(
    *,
    body: str,
    language: str,
    communication_context: dict[str, Any],
    playbook: dict[str, Any],
) -> dict[str, Any]:
    baseline_issues = _first_contact_issues(body)
    if not _reviewer_enabled() or not settings.openai_api_key.strip():
        return {
            "approved": not baseline_issues,
            "issues": baseline_issues,
            "corrected_body": str(body or "").strip(),
            "model": "deterministic-first-contact",
        }

    client = AsyncOpenAI(api_key=settings.openai_api_key)
    model = _reviewer_model()
    try:
        response = await client.chat.completions.create(
            model=model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You review the very first outbound WhatsApp message from Buy & Bring Solutions "
                        "to a new B2B lead. There has been no previous call or correspondence. Correct the "
                        "draft when it thanks the client for a conversation, says as agreed/discussed, "
                        "mentions a reminder, follow-up, returning to a topic or otherwise implies prior "
                        "contact. Use only confirmed CRM facts. Keep the requested language natural, concise, "
                        "friendly and professional. Do not add prices, certifications, guarantees, supplier "
                        "availability or completed work. Return JSON only."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "language": language,
                            "draft": body,
                            "deterministic_issues": baseline_issues,
                            "lead_context": communication_context,
                            "bbs_rules": playbook,
                            "schema": {
                                "approved": "boolean",
                                "issues": ["string"],
                                "corrected_body": "string",
                            },
                        },
                        ensure_ascii=False,
                        default=str,
                    ),
                },
            ],
            response_format={"type": "json_object"},
            temperature=0.03,
        )
        data = json.loads(response.choices[0].message.content or "{}")
        corrected = str(data.get("corrected_body") or body).strip()
        final_issues = _first_contact_issues(corrected)
        reported = [
            str(value).strip()
            for value in (data.get("issues") or [])
            if str(value).strip()
        ]
        issues = list(dict.fromkeys(reported + final_issues))
        return {
            "approved": bool(data.get("approved")) and not final_issues,
            "issues": issues,
            "corrected_body": corrected,
            "model": model,
        }
    except Exception as exc:
        logger.warning("First-contact reviewer unavailable: %s", exc.__class__.__name__)
        return {
            "approved": not baseline_issues,
            "issues": baseline_issues + [
                f"AI Reviewer временно недоступен: {exc.__class__.__name__}."
            ],
            "corrected_body": str(body or "").strip(),
            "model": f"{model}:fallback",
        }


@retry(stop=stop_after_attempt(2), wait=wait_exponential(multiplier=1, min=1, max=6))
async def generate_first_contact_draft(
    *,
    lead: dict[str, Any],
    language: str = "auto",
    manager_request: str = "",
) -> dict[str, Any]:
    if not settings.openai_api_key.strip():
        raise RuntimeError("OPENAI_API_KEY не настроен")
    if language == "auto":
        language = "ru"

    communication_context = _first_contact_context(lead, manager_request)
    playbook = _first_contact_playbook(language)
    query = communication_example_service.example_search_query(
        lead=lead,
        communication_context=communication_context,
        manager_request=manager_request,
    )
    examples = communication_example_service.find_similar_examples(
        kind=FIRST_CONTACT_KIND,
        language=language,
        channel="whatsapp",
        query=query,
        limit=4,
    )
    model = settings.agent_writer_model or settings.openai_model
    client = AsyncOpenAI(api_key=settings.openai_api_key)
    response = await client.chat.completions.create(
        model=model,
        messages=[
            {
                "role": "system",
                "content": (
                    "You are the senior B2B sourcing communication writer of Buy & Bring Solutions. "
                    "Write the very first outbound WhatsApp message to a new lead. There was no earlier "
                    "conversation. Never write thanks for the call/conversation, as agreed/discussed, "
                    "following up, returning to the topic, again, reminder or any equivalent phrase. "
                    "Use confirmed CRM facts only. Address the client naturally by name when available, "
                    "briefly introduce Buy & Bring Solutions, connect the message to the exact submitted "
                    "product/request, and ask three to six concise qualification points needed to understand "
                    "the purchase. The message must be friendly, professional, concise and suitable for "
                    "WhatsApp. Do not invent facts, prices, certificates, supplier availability, completed "
                    "checks or promises. Do not use placeholders. Return JSON only."
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "task": "Prepare the first WhatsApp contact for this new B2B lead.",
                        "output_language": generation._language_name(language),
                        "channel": "whatsapp",
                        "manager_request": manager_request,
                        "lead": lead,
                        "lead_context": communication_context,
                        "bbs_playbook": playbook,
                        "approved_similar_examples": examples,
                        "schema": {
                            "title": "string",
                            "subject": "string|null",
                            "body": "string",
                            "missing_data": ["string"],
                            "assumptions": ["string"],
                            "next_action": "string",
                            "language": "string",
                        },
                    },
                    ensure_ascii=False,
                    default=str,
                ),
            },
        ],
        response_format={"type": "json_object"},
        temperature=0.1,
    )
    data = json.loads(response.choices[0].message.content or "{}")
    original_body = str(data.get("body") or "").strip()
    if not original_body:
        raise ValueError("AI вернул пустое первое сообщение")

    review = await _review_first_contact(
        body=original_body,
        language=language,
        communication_context=communication_context,
        playbook=playbook,
    )
    reviewed_body = str(review.get("corrected_body") or original_body).strip()
    fatal_issues = _first_contact_issues(reviewed_body)
    if fatal_issues:
        raise ValueError("Первое сообщение не прошло проверку: " + "; ".join(fatal_issues))

    issues = [str(value) for value in (review.get("issues") or []) if str(value).strip()]
    assumptions = [
        str(value) for value in (data.get("assumptions") or []) if str(value).strip()
    ]
    assumptions.extend(f"Reviewer: {issue}" for issue in issues[:8])
    return {
        "title": str(data.get("title") or "Первый контакт")[:500],
        "subject": str(data.get("subject"))[:500] if data.get("subject") else None,
        "body": reviewed_body,
        "ai_original_body": original_body,
        "reviewed_body": reviewed_body,
        "review_approved": bool(review.get("approved")) and not fatal_issues,
        "review_issues": issues[:20],
        "missing_data": [
            str(value) for value in (data.get("missing_data") or []) if str(value).strip()
        ][:30],
        "assumptions": assumptions[:30],
        "next_action": str(
            data.get("next_action") or "Получить ответ клиента и перейти к сбору информации"
        )[:1000],
        "language": language,
        "kind": FIRST_CONTACT_KIND,
        "knowledge_version": communication_context_service.KNOWLEDGE_VERSION,
        "writer_model": model,
        "reviewer_model": review.get("model"),
        "generation_context": {
            "interaction_mode": "first_contact",
            "conversation_history_used": False,
            "example_ids": [item.get("id") for item in examples],
            "playbook_version": playbook.get("version"),
        },
    }


def _add_first_contact_button(
    markup: dict[str, Any] | None,
    *,
    lead_id: int,
) -> dict[str, Any] | None:
    if not markup or not lead_id:
        return markup
    result = copy.deepcopy(markup)
    rows = list(result.get("inline_keyboard") or [])
    callback = f"agent:prep:first:{lead_id}"
    if any(
        button.get("callback_data") == callback
        for row in rows
        for button in row
        if isinstance(button, dict)
    ):
        return result

    first_button = {"text": "👋 Первый контакт", "callback_data": callback}
    follow_index = None
    follow_button = None
    remaining_row: list[dict[str, str]] = []
    for index, row in enumerate(rows):
        for button in row:
            if str(button.get("callback_data") or "") == f"agent:prep:draft:{lead_id}":
                follow_index = index
                follow_button = dict(button)
                remaining_row = [dict(item) for item in row if item is not button]
                break
        if follow_index is not None:
            break

    if follow_index is None:
        rows.insert(0, [first_button])
    else:
        replacement: list[list[dict[str, str]]] = [
            [first_button, follow_button or {
                "text": "✍️ Follow-up",
                "callback_data": f"agent:prep:draft:{lead_id}",
            }]
        ]
        if remaining_row:
            replacement.append(remaining_row)
        rows[follow_index : follow_index + 1] = replacement
    result["inline_keyboard"] = rows
    return result


async def _prepare_first_contact_reply(
    db: Any,
    *,
    lead_id: int,
    telegram_user_id: int,
) -> AgentReply:
    session = await memory.get_or_create_session(db, telegram_user_id=telegram_user_id)
    context = await memory.build_context(
        db, telegram_user_id=telegram_user_id, session=session
    )
    lead = await kommo_service.get_lead_details(int(lead_id))
    await memory.set_active_lead(
        db,
        session=session,
        kommo_lead_id=int(lead_id),
        lead_name=str(lead.get("name") or ""),
    )
    language_resolution = await client_language_service.resolve_communication_language(
        db,
        lead=lead,
        explicit_language=None,
    )
    draft = await generation.generate_draft(
        kind=FIRST_CONTACT_KIND,
        lead=tools.lead_summary_for_ai(lead),
        language=language_resolution.language,
        manager_request=(
            "Подготовь первое WhatsApp-сообщение новому лиду. Мы ещё не общались. "
            "Используй только данные карточки и задай короткие вопросы для первичной квалификации."
        ),
    )
    record = await client_message_service.create_client_message_draft(
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
            "last_client_message_draft_id": record.id,
            "last_draft_created_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    return AgentReply(
        client_message_service.format_client_message_draft(record),
        reply_markup=client_message_service.message_draft_markup(record),
        intent="generate_first_contact",
        metadata={
            "lead_id": lead.get("id"),
            "draft_kind": FIRST_CONTACT_KIND,
            "client_message_draft_id": record.id,
            "communication_language": record.communication_language,
            "language_source": record.language_source,
        },
    )


def install_first_contact_runtime() -> None:
    """Install the separate first-contact action after all other UI runtimes."""
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True

    original_generate_draft = generation.generate_draft

    async def generate_draft_with_first_contact(
        *,
        kind: str,
        lead: dict[str, Any],
        language: str = "auto",
        manager_request: str = "",
    ) -> dict[str, Any]:
        if kind == FIRST_CONTACT_KIND:
            return await generate_first_contact_draft(
                lead=lead,
                language=language,
                manager_request=manager_request,
            )
        return await original_generate_draft(
            kind=kind,
            lead=lead,
            language=language,
            manager_request=manager_request,
        )

    generation.generate_draft = generate_draft_with_first_contact

    original_project_markup = project_snapshot.project_actions_markup

    def project_markup_with_first_contact(snapshot: Any) -> dict[str, Any]:
        lead_id = int(snapshot.identity.get("kommo_lead_id") or 0)
        return _add_first_contact_button(
            original_project_markup(snapshot), lead_id=lead_id
        ) or {"inline_keyboard": []}

    project_snapshot.project_actions_markup = project_markup_with_first_contact

    original_unified_markup = unified_project_service.project_actions_markup

    def unified_markup_with_first_contact(project: Any) -> dict[str, Any]:
        lead_id = int(project.kommo_lead_id or 0)
        return _add_first_contact_button(
            original_unified_markup(project), lead_id=lead_id
        ) or {"inline_keyboard": []}

    unified_project_service.project_actions_markup = unified_markup_with_first_contact

    original_lead_markup = tools.lead_card_actions_markup

    def lead_markup_with_first_contact(lead: dict[str, Any]) -> dict[str, Any]:
        lead_id = int(lead.get("id") or lead.get("kommo_lead_id") or 0)
        return _add_first_contact_button(
            original_lead_markup(lead), lead_id=lead_id
        ) or {"inline_keyboard": []}

    tools.lead_card_actions_markup = lead_markup_with_first_contact

    original_handle_callback = agent_service.handle_callback

    async def handle_callback_with_first_contact(
        db: Any,
        *,
        callback_data: str,
        telegram_user_id: int,
        chat_id: int | None = None,
    ) -> AgentReply | None:
        parts = callback_data.split(":")
        if (
            len(parts) == 4
            and parts[:3] == ["agent", "prep", "first"]
            and parts[3].isdigit()
        ):
            try:
                return await _prepare_first_contact_reply(
                    db,
                    lead_id=int(parts[3]),
                    telegram_user_id=telegram_user_id,
                )
            except Exception as exc:
                logger.exception("Could not prepare first-contact message")
                return AgentReply(
                    agent_service._error_text(exc),
                    intent="first_contact_failed",
                    metadata={"lead_id": int(parts[3])},
                )
        return await original_handle_callback(
            db,
            callback_data=callback_data,
            telegram_user_id=telegram_user_id,
            chat_id=chat_id,
        )

    agent_service.handle_callback = handle_callback_with_first_contact

    original_format_draft = client_message_service.format_client_message_draft

    def format_draft_with_first_contact(record: Any) -> str:
        text = original_format_draft(record)
        kind = str((record.metadata_json or {}).get("draft_kind") or "")
        if kind != FIRST_CONTACT_KIND:
            return text
        text = text.replace(
            "повторите follow-up",
            "повторите создание первого сообщения",
        )
        return "<b>👋 Первый контакт — черновик</b>\n\n" + text

    client_message_service.format_client_message_draft = format_draft_with_first_contact

    original_draft_markup = client_message_service.message_draft_markup

    def draft_markup_with_first_contact(record: Any) -> dict[str, Any]:
        markup = copy.deepcopy(original_draft_markup(record))
        kind = str((record.metadata_json or {}).get("draft_kind") or "")
        if kind == FIRST_CONTACT_KIND:
            for row in markup.get("inline_keyboard") or []:
                for button in row:
                    if button.get("url") and "WhatsApp" in str(button.get("text") or ""):
                        button["text"] = "👋 Открыть WhatsApp"
                        return markup
        return markup

    client_message_service.message_draft_markup = draft_markup_with_first_contact

    # Language buttons must preserve first-contact semantics instead of invoking
    # the old follow-up generator.
    from app.api import telegram as telegram_api

    original_client_message_callback = telegram_api._handle_client_message_callback

    async def client_message_callback_with_first_contact(
        *,
        callback_data: str,
        chat_id: int,
        user_id: int,
        db: Any,
    ) -> bool:
        parts = callback_data.split(":")
        if (
            len(parts) == 4
            and parts[:2] == ["clientmsg", "lang"]
            and parts[3].isdigit()
        ):
            record = await client_message_service.get_draft(db, int(parts[3]))
            kind = str(((record.metadata_json if record else {}) or {}).get("draft_kind") or "")
            if record is not None and kind == FIRST_CONTACT_KIND:
                actor = telegram_api.identity_service.current_user()
                if not telegram_api.identity_service.can_write(actor):
                    raise PermissionError("Роль Viewer позволяет только просматривать данные.")
                language = parts[2]
                lead = await kommo_service.get_lead_details(int(record.kommo_lead_id))
                generated = await generation.generate_draft(
                    kind=FIRST_CONTACT_KIND,
                    lead=tools.lead_summary_for_ai(lead),
                    language=language,
                    manager_request=(
                        "Адаптируй существующее первое сообщение на выбранный язык. "
                        "Не добавляй намёков на предыдущий разговор и не меняй факты.\n\n"
                        + record.body
                    ),
                )
                updated = await client_message_service.update_language_and_body(
                    db,
                    draft_id=int(record.id),
                    telegram_user_id=user_id,
                    lead=lead,
                    language=language,
                    generated=generated,
                )
                await telegram_service.send_message(
                    chat_id,
                    client_message_service.format_client_message_draft(updated),
                    reply_markup=client_message_service.message_draft_markup(updated),
                )
                return True
        return await original_client_message_callback(
            callback_data=callback_data,
            chat_id=chat_id,
            user_id=user_id,
            db=db,
        )

    telegram_api._handle_client_message_callback = (
        client_message_callback_with_first_contact
    )
