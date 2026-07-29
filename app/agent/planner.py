from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from openai import AsyncOpenAI
from app.agent.retrying import retry, stop_after_attempt, wait_exponential

from app.agent.contracts import AgentPlan
from app.agent.lead_refs import (
    extract_explicit_kommo_id,
    parse_lead_references,
    user_error_hint,
)
from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

_WRITE_INTENTS = {
    "add_kommo_note",
    "add_kommo_notes_batch",
    "create_kommo_task",
    "create_kommo_tasks_batch",
    "create_calendar_event",
    "update_kommo_lead",
    "save_draft_to_notion",
    "create_gmail_draft",
    "sync_leads_to_notion",
    "create_drive_project",
    "save_file_to_drive_project",
    "link_project_systems",
    "project_update_bundle",
}

_DRAFT_KINDS = {
    "commercial_offer": ("кп", "коммерческ", "предложени"),
    "supplier_inquiry": ("поставщик", "фабрик", "производител", "запрос в китай"),
    "followup_message": (
        "follow-up",
        "follow up",
        "фоллоу",
        "сообщение клиент",
        "напомнить клиент",
        "напиши клиент",
    ),
    "email": ("письмо", "email", "e-mail"),
    "catalog_outline": ("каталог", "прайс", "price list"),
    "technical_brief": ("техзадан", "техническ", "тз "),
}

SYSTEM_PROMPT = """You are the planning layer of the private Telegram AI agent for Buy & Bring Solutions.
The manager speaks Russian and works with B2B sourcing, China suppliers, Kommo CRM, Notion, Gmail and Google Calendar.

Your job is ONLY to classify the manager request and return one JSON object. Do not answer the request itself.

Supported intents:
- general_assistant: advice, explanation, translation, calculations described in prose, brainstorming, business questions
- conversation_analysis: the message is a transcript/report of a client conversation that should enter the call-analysis pipeline
- daily_digest: show what deserves attention today (read-only)
- search_lead: find a Kommo lead by ID, number, client or product fragment (read-only)
- search_project: find a project by internal number, client, phone, company or product (read-only)
- lead_summary: load and explain one Kommo lead (read-only)
- project_snapshot: unified project card from Kommo, Notion and Drive (read-only)
- project_update_bundle: spoken/text project update split into separately confirmed writes
- notion_diagnostics: test the operational Notion schema (read-only)
- integration_errors: show recent integration failures (read-only)
- generate_draft: generate a draft; draft_kind is commercial_offer, supplier_inquiry, followup_message, email, catalog_outline or technical_brief
- add_kommo_note: mutating; requires confirmation
- create_kommo_task: mutating; requires confirmation
- create_calendar_event: mutating; requires confirmation
- update_kommo_lead: mutating; requires confirmation
- save_draft_to_notion: mutating; requires confirmation
- create_gmail_draft: mutating; requires confirmation
- sync_leads_to_notion: mutating; requires confirmation
- help
- reset_memory
- unknown

Rules:
- Never invent a lead ID. If the user says "эта сделка", use CONTEXT.active_kommo_lead_id when available.
- A small internal lead number (for example 135) can be a title fragment, not necessarily a Kommo entity ID. Put it in query unless explicitly marked #ID/Kommo ID or context resolves it.
- All mutating intents must use mode="write".
- Draft generation itself is not an external write, so mode="draft".
- Read-only tools use mode="read".
- A normal answer uses mode="answer".
- If an essential target/date/text is missing, use mode="clarify" and write one concise Russian clarification_question.
- due_at can be a natural Russian phrase such as "завтра в 10:00". Do not invent dates.
- fields for update_kommo_lead may contain only name, price, status_id.
- language should reflect an explicitly requested output language; otherwise use auto.
- confidence is 0..1.

Schema:
{
  "intent": "string",
  "mode": "answer|read|draft|write|conversation|clarify",
  "confidence": 0.0,
  "lead_id": 123456 or null,
  "query": "string or null",
  "draft_kind": "string or null",
  "title": "string or null",
  "body": "string or null",
  "note_text": "string or null",
  "due_at": "string or null",
  "duration_minutes": 30,
  "reminder_minutes": 30,
  "event_type": "call|meeting|message|proposal|other",
  "fields": {},
  "language": "auto|ru|pl|uk|en|de|zh",
  "clarification_question": "Russian string or null",
  "rationale": "short Russian string"
}
"""


def _normalize(text: str) -> str:
    return " ".join(text.strip().casefold().replace("ё", "е").split())


def _lead_reference(text: str, context: dict[str, Any]) -> tuple[int | None, str | None]:
    explicit = extract_explicit_kommo_id(text)
    if explicit:
        return explicit, None
    normalized = _normalize(text)
    if any(token in normalized for token in ("эта сделка", "этот лид", "по нему", "по ней")):
        active = context.get("active_kommo_lead_id") or context.get("kommo_lead_id")
        if active:
            return int(active), None
    number_match = re.search(r"\b(?:сделк[аеуы]?|лид[ауе]?)\s*[№#]?\s*(\d{1,12})\b", normalized)
    if number_match:
        value = number_match.group(1)
        if len(value) >= 4 and ("#" in text or "id" in normalized or "ид" in normalized):
            return int(value), None
        return None, value
    return None, None


def _plan_lead_refs(text: str, context: dict[str, Any]) -> list[dict[str, Any]]:
    refs = parse_lead_references(text, context)
    return [
        {
            "raw": ref.raw,
            "ref_type": ref.ref_type.value,
            "internal_lead_number": ref.internal_lead_number,
            "kommo_lead_id": ref.kommo_lead_id,
            "digest_position": ref.digest_position,
            "name_query": ref.name_query,
            "resolved_kommo_lead_id": ref.resolved_kommo_lead_id,
            "confidence": ref.confidence,
        }
        for ref in refs
    ]


def _language_hint(normalized: str) -> str:
    if any(
        token in normalized
        for token in ("на польск", "по-польск", "по польск", "po polsku")
    ):
        return "pl"
    if any(
        token in normalized
        for token in ("на украин", "по-украин", "по украин", "україн")
    ):
        return "uk"
    if any(
        token in normalized
        for token in ("на русск", "по-русск", "по русск", "російськ")
    ):
        return "ru"
    if "на англий" in normalized or "in english" in normalized:
        return "en"
    if "на немец" in normalized or "auf deutsch" in normalized:
        return "de"
    if "на китай" in normalized or "中文" in normalized:
        return "zh"
    return "auto"


def deterministic_plan(text: str, context: dict[str, Any]) -> AgentPlan | None:
    normalized = _normalize(text)
    if not normalized:
        return AgentPlan(intent="unknown", mode="clarify", confidence=1.0, clarification_question="Что нужно сделать?")

    if normalized in {"/help", "/agent", "help", "помощь", "что ты умеешь", "команды"}:
        return AgentPlan(intent="help", mode="read", confidence=1.0)
    if normalized in {"/cancel", "отмена", "отмени", "отменить"}:
        return AgentPlan(intent="cancel_clarification", mode="read", confidence=1.0)
    if normalized in {"забудь контекст", "сбрось память", "reset memory", "/reset_memory"}:
        return AgentPlan(intent="reset_memory", mode="write", confidence=1.0)
    if normalized in {"дайджест", "digest", "что делать сегодня", "задачи дня", "/digest", "/morning", "/today"}:
        return AgentPlan(intent="daily_digest", mode="read", confidence=1.0)
    if normalized in {"/evening", "подведи итоги дня"}:
        return AgentPlan(intent="daily_digest", mode="read", confidence=1.0)
    if normalized in {"/costs", "/cost", "расходы ai", "стоимость ai"}:
        return AgentPlan(intent="ai_costs", mode="read", confidence=1.0)
    if normalized in {"ошибки", "последние ошибки", "журнал ошибок", "/errors"}:
        return AgentPlan(intent="integration_errors", mode="read", confidence=1.0)
    if "проверь notion" in normalized or "тест notion" in normalized or normalized == "/notion_test":
        return AgentPlan(intent="notion_diagnostics", mode="read", confidence=1.0)
    if any(phrase in normalized for phrase in (
        "сохрани черновик в notion",
        "сохрани это в notion",
        "сохрани его в notion",
        "запиши черновик в notion",
    )):
        return AgentPlan(
            intent="save_draft_to_notion",
            mode="write",
            confidence=0.98,
        )
    if any(phrase in normalized for phrase in (
        "создай черновик gmail",
        "сохрани письмо в gmail",
        "создай это в gmail",
        "создай письмо в gmail",
    )):
        return AgentPlan(
            intent="create_gmail_draft",
            mode="write",
            confidence=0.98,
        )

    if normalized == "/sync_leads" or (
        "синхрониз" in normalized
        and any(word in normalized for word in ("notion", "сделк", "лид", "kommo", "коммо"))
    ):
        return AgentPlan(
            intent="sync_leads_to_notion",
            mode="write",
            confidence=0.98,
            rationale="Синхронизация изменит внешнюю базу Notion и требует подтверждения.",
        )

    lead_id, query = _lead_reference(text, context)
    lead_refs = _plan_lead_refs(text, context)
    language = _language_hint(normalized)
    multi_lead = len(lead_refs) > 1 or bool(
        re.search(r"\b\d{1,4}\s*[,/и]\s*\d", normalized)
    )

    if (
        "проект" in normalized
        and any(
            token in normalized
            for token in (
                "поговорил",
                "поговорили",
                "обсудил",
                "созвонил",
                "договорились",
                "обновление",
                "обнови проект",
            )
        )
    ):
        return AgentPlan(
            intent="project_update_bundle",
            mode="write",
            confidence=0.98,
            lead_id=lead_id or context.get("active_kommo_lead_id"),
            query=query,
            lead_refs=lead_refs,
            body=text,
            clarification_question=(
                None
                if lead_id or query or lead_refs or context.get("active_kommo_lead_id")
                else user_error_hint()
            ),
        )

    for kind, hints in _DRAFT_KINDS.items():
        if any(hint in normalized for hint in hints) and any(
            verb in normalized
            for verb in ("сделай", "подготов", "напиши", "создай", "состав", "нужен", "сгенер")
        ):
            if lead_id is None and query is None:
                active = context.get("active_kommo_lead_id") or context.get("kommo_lead_id")
                lead_id = int(active) if active else None
            return AgentPlan(
                intent="generate_draft",
                mode="draft",
                confidence=0.96,
                lead_id=lead_id,
                query=query,
                draft_kind=kind,
                language=language,
                clarification_question=(
                    None
                    if lead_id or query or lead_refs
                    else "Для какой сделки подготовить черновик? "
                    + user_error_hint()
                ),
            )

    if any(hint in normalized for hint in ("добавь примечание", "добавь заметку", "запиши в коммо", "заметка в коммо")):
        note = re.split(r"(?:добавь примечание|добавь заметку|запиши в коммо|заметка в коммо)", text, maxsplit=1, flags=re.I)
        note_text = note[1].strip(" :—-") if len(note) > 1 else ""
        if ":" in note_text:
            note_text = note_text.split(":", 1)[1].strip()
        else:
            note_text = re.sub(
                r"^(?:в|для|по)?\s*(?:сделк[уеаи]?|лид[уеаи]?)?\s*[№#]?\s*\d{1,12}\s*[-—:]?\s*",
                "",
                note_text,
                flags=re.I,
            ).strip()
        intent = "add_kommo_notes_batch" if multi_lead else "add_kommo_note"
        return AgentPlan(
            intent=intent,
            mode="write" if (lead_id or lead_refs or context.get("active_kommo_lead_id")) and note_text else "clarify",
            confidence=0.96,
            lead_id=lead_id or context.get("active_kommo_lead_id"),
            query=query,
            lead_refs=lead_refs,
            note_text=note_text or None,
            clarification_question=(
                "Что именно записать и для каких клиентов?"
                if not note_text or not (lead_id or query or lead_refs or context.get("active_kommo_lead_id"))
                else None
            ),
        )

    if any(hint in normalized for hint in ("поставь задачу", "создай задачу", "поставь задачи", "создай задачи", "напомни", "задачу по", "задачи по")):
        intent = "create_kommo_tasks_batch" if multi_lead else "create_kommo_task"
        return AgentPlan(
            intent=intent,
            mode="write" if (lead_id or query or lead_refs or context.get("active_kommo_lead_id")) else "clarify",
            confidence=0.9,
            lead_id=lead_id or context.get("active_kommo_lead_id"),
            query=query,
            lead_refs=lead_refs,
            title=text.strip(),
            due_at=text.strip(),
            clarification_question=(
                None
                if lead_id or query or lead_refs or context.get("active_kommo_lead_id")
                else user_error_hint()
            ),
        )

    if any(hint in normalized for hint in ("добавь в календар", "создай событие", "создай встреч", "запланируй созвон", "запланируй встреч")):
        event_type = "meeting" if "встреч" in normalized else "call"
        return AgentPlan(
            intent="create_calendar_event",
            mode="write",
            confidence=0.94,
            lead_id=lead_id or context.get("active_kommo_lead_id"),
            query=query,
            title=text.strip(),
            due_at=text.strip(),
            event_type=event_type,
            language=language,
        )

    if any(phrase in normalized for phrase in (
        "создай проект в drive",
        "создай папку",
        "создай папки",
        "организуй документы",
        "создай структуру проекта",
        "создай проект по",
    )):
        return AgentPlan(
            intent="create_drive_project",
            mode="write",
            confidence=0.95,
            lead_id=lead_id or context.get("active_kommo_lead_id"),
            query=query,
            lead_refs=lead_refs,
            clarification_question=None if lead_id or query or lead_refs or context.get("active_kommo_lead_id") else user_error_hint(),
        )

    if any(phrase in normalized for phrase in (
        "свяжи проект",
        "свяжи notion",
        "привяжи notion",
        "свяжи kommo notion drive",
        "link notion",
    )):
        return AgentPlan(
            intent="link_project_systems",
            mode="write",
            confidence=0.94,
            lead_id=lead_id or context.get("active_kommo_lead_id"),
            query=query,
            lead_refs=lead_refs,
            clarification_question=None if lead_id or query or lead_refs or context.get("active_kommo_lead_id") else user_error_hint(),
        )

    if any(
        phrase in normalized
        for phrase in (
            "найди проект",
            "поиск проекта",
            "найди клиента по телефону",
            "найди по компании",
            "найди по товару",
        )
    ):
        project_query = re.sub(
            r"^(?:найди|поиск)\s+(?:проект[а-я]*\s*)?(?:по\s+)?"
            r"(?:телефон[ау]?\s*|компани[ияе]\s*|товар[ау]?\s*)?",
            "",
            text.strip(),
            flags=re.I,
        ).strip(" :—-")
        return AgentPlan(
            intent="search_project",
            mode="read",
            confidence=0.98,
            query=project_query or query or text,
        )

    if any(phrase in normalized for phrase in (
        "что происходит по проекту",
        "полную карточку",
        "покажи проект",
        "что мы уже сделали",
        "какие вопросы остались",
        "покажи документы проекта",
    )):
        return AgentPlan(
            intent="project_snapshot",
            mode="read",
            confidence=0.94,
            lead_id=lead_id or context.get("active_kommo_lead_id"),
            query=query,
            lead_refs=lead_refs,
            clarification_question=None if lead_id or query or lead_refs or context.get("active_kommo_lead_id") else user_error_hint(),
        )

    if normalized.startswith("что по ") and lead_refs:
        return AgentPlan(
            intent="project_snapshot",
            mode="read",
            confidence=0.90,
            lead_id=lead_id or context.get("active_kommo_lead_id"),
            query=query,
            lead_refs=lead_refs,
        )

    if any(hint in normalized for hint in ("покажи сделку", "найди сделку", "найди лид", "открой сделку", "что по сделке", "расскажи по сделке")):
        intent = "lead_summary" if any(h in normalized for h in ("что по", "расскажи", "покажи")) else "search_lead"
        search_query = query or text
        return AgentPlan(
            intent=intent,
            mode="read",
            confidence=0.94,
            lead_id=lead_id,
            query=search_query,
            lead_refs=lead_refs,
            clarification_question=(
                None
                if lead_id or search_query.strip() or lead_refs
                else user_error_hint()
            ),
        )

    return None


def _compact_context(context: dict[str, Any]) -> dict[str, Any]:
    return {
        "active_kommo_lead_id": context.get("active_kommo_lead_id"),
        "active_local_lead_id": context.get("active_local_lead_id"),
        "active_lead_name": context.get("active_lead_name"),
        "last_intent": context.get("last_intent"),
        "memory_summary": context.get("memory_summary"),
        "recent_messages": (context.get("recent_messages") or [])[-8:],
    }


def _manager_now() -> str:
    try:
        tz = ZoneInfo(settings.manager_timezone or "Europe/Warsaw")
    except Exception:
        tz = ZoneInfo("UTC")
    return datetime.now(tz=tz).isoformat()


@retry(stop=stop_after_attempt(2), wait=wait_exponential(multiplier=1, min=1, max=5))
async def _ai_plan(text: str, context: dict[str, Any]) -> AgentPlan:
    if not settings.openai_api_key.strip():
        return AgentPlan(
            intent="general_assistant",
            mode="answer",
            confidence=0.4,
            query=text,
            rationale="OPENAI_API_KEY не настроен; использован безопасный резервный режим.",
        )
    client = AsyncOpenAI(api_key=settings.openai_api_key)
    response = await client.chat.completions.create(
        model=settings.agent_planner_model or settings.openai_model,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "now": _manager_now(),
                        "timezone": settings.manager_timezone,
                        "context": _compact_context(context),
                        "message": text,
                    },
                    ensure_ascii=False,
                    default=str,
                ),
            },
        ],
        response_format={"type": "json_object"},
        temperature=0.05,
    )
    raw = json.loads(response.choices[0].message.content or "{}")
    plan = AgentPlan.model_validate(raw)
    if plan.intent in _WRITE_INTENTS:
        plan.mode = "write"
    if plan.intent == "conversation_analysis":
        plan.mode = "conversation"
    return plan


async def plan_message(text: str, *, context: dict[str, Any]) -> AgentPlan:
    deterministic = deterministic_plan(text, context)
    if deterministic is not None:
        if deterministic.mode == "clarify" and not deterministic.clarification_question:
            deterministic.clarification_question = "Уточни, пожалуйста, что именно нужно сделать."
        return deterministic
    try:
        return await _ai_plan(text, context)
    except Exception as exc:
        logger.warning("Agent planner failed: %s", exc)
        return AgentPlan(
            intent="general_assistant",
            mode="answer",
            confidence=0.35,
            query=text,
            rationale=f"Planner fallback: {exc.__class__.__name__}",
        )
