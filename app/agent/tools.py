from __future__ import annotations

import html
import re
from typing import Any

from app.agent.lead_refs import (
    LeadReference,
    LeadRefType,
    LeadResolutionResult,
    enrich_lead,
    extract_internal_lead_number,
    resolve_lead_for_plan,
    user_error_hint,
)
from app.services import kommo_service


class LeadResolutionError(ValueError):
    def __init__(
        self,
        message: str,
        *,
        candidates: list[dict[str, Any]] | None = None,
        unresolved: list[LeadReference] | None = None,
    ):
        super().__init__(message)
        self.candidates = candidates or []
        self.unresolved = unresolved or []


def _clean_search_query(value: str) -> str:
    text = " ".join((value or "").strip().split())
    text = re.sub(
        r"\b(?:покажи|найди|открой|расскажи|что|по|сделке?|лид[ауе]?|коммо|kommo)\b",
        " ",
        text,
        flags=re.I,
    )
    return " ".join(text.split()).strip(" #№:—-")


def lead_refs_from_plan(plan: Any) -> list[LeadReference]:
    refs: list[LeadReference] = []
    for raw in getattr(plan, "lead_refs", None) or []:
        if isinstance(raw, LeadReference):
            refs.append(raw)
            continue
        if not isinstance(raw, dict):
            continue
        refs.append(
            LeadReference(
                raw=str(raw.get("raw") or ""),
                ref_type=LeadRefType(str(raw.get("ref_type") or LeadRefType.RAW)),
                internal_lead_number=raw.get("internal_lead_number"),
                kommo_lead_id=raw.get("kommo_lead_id"),
                digest_position=raw.get("digest_position"),
                name_query=raw.get("name_query"),
                resolved_kommo_lead_id=raw.get("resolved_kommo_lead_id"),
                confidence=float(raw.get("confidence") or 1.0),
            )
        )
    return refs


async def resolve_lead(
    *,
    lead_id: int | None,
    query: str | None,
    context: dict[str, Any],
    lead_refs: list[LeadReference] | None = None,
    plan: Any | None = None,
) -> dict[str, Any]:
    refs = list(lead_refs or [])
    if plan is not None:
        refs = refs or lead_refs_from_plan(plan)
    result = await resolve_lead_for_plan(
        lead_id=lead_id,
        query=query,
        lead_refs=refs,
        context=context,
    )
    if len(result.resolved) == 1 and not result.unresolved:
        return result.resolved[0]
    if result.unresolved or len(result.resolved) > 1:
        if result.candidates or result.unresolved:
            candidates = result.candidates or []
            if not candidates and result.resolved:
                candidates = result.resolved
            raise LeadResolutionError(
                "Нашёл несколько сделок. Выбери нужную карточку кнопкой.",
                candidates=candidates,
                unresolved=result.unresolved,
            )
    if result.error_message:
        raise LeadResolutionError(result.error_message, unresolved=result.unresolved)
    raise LeadResolutionError(user_error_hint())


async def resolve_leads(
    *,
    lead_id: int | None,
    query: str | None,
    context: dict[str, Any],
    lead_refs: list[LeadReference] | None = None,
    plan: Any | None = None,
) -> LeadResolutionResult:
    refs = list(lead_refs or [])
    if plan is not None:
        refs = refs or lead_refs_from_plan(plan)
    return await resolve_lead_for_plan(
        lead_id=lead_id,
        query=query,
        lead_refs=refs,
        context=context,
    )


def format_candidates(candidates: list[dict[str, Any]]) -> str:
    lines = ["<b>Нашёл несколько сделок</b>", "", "Выбери нужную карточку кнопкой:"]
    for item in candidates[:8]:
        lead_id = item.get("id") or item.get("kommo_lead_id")
        internal = extract_internal_lead_number(item) or item.get("internal_lead_number")
        name = html.escape(str(item.get("name") or lead_id))
        prefix = f"№{internal} — " if internal else ""
        lines.append(f"• {prefix}{name}")
        if lead_id:
            lines.append(f"  Kommo ID: <code>{html.escape(str(lead_id))}</code>")
    return "\n".join(lines)


def candidates_markup(candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
    rows: list[list[dict[str, str]]] = []
    for item in candidates[:8]:
        lead_id = item.get("id") or item.get("kommo_lead_id")
        if not isinstance(lead_id, int):
            continue
        internal = extract_internal_lead_number(item) or item.get("internal_lead_number")
        raw_name = " ".join(str(item.get("name") or lead_id).split())
        label = raw_name[:36] + ("…" if len(raw_name) > 36 else "")
        if internal:
            label = f"№{internal} · {label}"
        rows.append(
            [
                {
                    "text": label[:64],
                    "callback_data": f"agent:lead:{lead_id}",
                }
            ]
        )
    return {"inline_keyboard": rows} if rows else None


def format_lead_summary(lead: dict[str, Any]) -> str:
    contacts = lead.get("contacts") or []
    contact = contacts[0] if contacts else {}
    phones = ", ".join(str(x) for x in (contact.get("phones") or [])) or "—"
    emails = ", ".join(str(x) for x in (contact.get("emails") or [])) or "—"
    notes = lead.get("notes") or []
    last_note = str(notes[0].get("text") or "")[:1000] if notes else "—"
    internal = extract_internal_lead_number(lead) or lead.get("internal_lead_number")
    lines = [
        f"<b>📌 {html.escape(str(lead.get('name') or lead.get('id') or 'Сделка'))}</b>",
        "",
    ]
    if internal:
        lines.append(f"Внутренний номер: <b>№{html.escape(str(internal))}</b>")
    else:
        lines.append("Внутренний номер: не указан")
    lines.extend(
        [
            f"Kommo ID: <code>{html.escape(str(lead.get('id') or '—'))}</code>",
            f"Воронка: {html.escape(str(lead.get('pipeline_name') or '—'))}",
            f"Этап: {html.escape(str(lead.get('status_name') or '—'))}",
            f"Бюджет: {html.escape(str(lead.get('price') or '—'))}",
            f"Клиент: {html.escape(str(contact.get('name') or '—'))}",
            f"Телефон: {html.escape(phones)}",
            f"Email: {html.escape(emails)}",
            "",
            "<b>Последнее примечание</b>",
            html.escape(last_note),
        ]
    )
    if lead.get("url"):
        lines.extend(
            ["", f'<a href="{html.escape(str(lead["url"]), quote=True)}">Открыть в Kommo</a>']
        )
    return "\n".join(lines)


def lead_card_actions_markup(lead: dict[str, Any]) -> dict[str, Any]:
    lead_id = int(lead.get("id") or lead.get("kommo_lead_id") or 0)
    rows = [
        [
            {"text": "📞 Поставить задачу", "callback_data": f"agent:prep:task:{lead_id}"},
            {"text": "📝 Добавить заметку", "callback_data": f"agent:prep:note:{lead_id}"},
        ],
        [
            {"text": "✍️ Подготовить follow-up", "callback_data": f"agent:prep:draft:{lead_id}"},
        ],
    ]
    if lead.get("url"):
        rows.append(
            [{"text": "🔗 Открыть Kommo", "url": str(lead.get("url"))}]
        )
    return {"inline_keyboard": rows}


def lead_summary_for_ai(lead: dict[str, Any]) -> dict[str, Any]:
    contacts = lead.get("contacts") or []
    return {
        "id": lead.get("id"),
        "internal_lead_number": extract_internal_lead_number(lead),
        "name": lead.get("name"),
        "price": lead.get("price"),
        "pipeline_name": lead.get("pipeline_name"),
        "status_name": lead.get("status_name"),
        "created_at": lead.get("created_at"),
        "updated_at": lead.get("updated_at"),
        "closest_task_at": lead.get("closest_task_at"),
        "contacts": contacts[:3],
        "custom_fields": lead.get("custom_fields") or {},
        "notes": (lead.get("notes") or [])[:5],
        "url": lead.get("url"),
    }
