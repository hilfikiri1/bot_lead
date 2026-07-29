from __future__ import annotations

import html
import re
from typing import Any

from app.services import kommo_service


class LeadResolutionError(ValueError):
    def __init__(self, message: str, *, candidates: list[dict[str, Any]] | None = None):
        super().__init__(message)
        self.candidates = candidates or []


def _stem_search_token(token: str) -> str:
    """Reduce common Russian noun endings for tolerant Kommo title search."""
    if len(token) < 6 or not re.fullmatch(r"[а-яё]+", token, flags=re.I):
        return token
    for ending in (
        "иями",
        "ями",
        "ами",
        "ого",
        "ему",
        "ому",
        "ыми",
        "ими",
        "ах",
        "ях",
        "ам",
        "ям",
        "ов",
        "ев",
        "ом",
        "ем",
        "у",
        "ю",
        "а",
        "я",
        "ы",
        "и",
        "е",
    ):
        if token.casefold().endswith(ending) and len(token) - len(ending) >= 4:
            return token[: -len(ending)]
    return token


def _clean_search_query(value: str) -> str:
    text = " ".join((value or "").strip().split())
    text = re.sub(
        r"\b(?:покажи|найди|открой|расскажи|что|по|сделк[аеуы]?|лид[ауе]?|коммо|kommo)\b",
        " ",
        text,
        flags=re.I,
    )
    cleaned = " ".join(text.split()).strip(" #№:—-")
    return " ".join(_stem_search_token(token) for token in cleaned.split())


async def resolve_lead(
    *,
    lead_id: int | None,
    query: str | None,
    context: dict[str, Any],
) -> dict[str, Any]:
    if lead_id:
        return await kommo_service.get_lead_details(int(lead_id))

    clean_query = _clean_search_query(str(query or ""))
    if not clean_query:
        active_lead_id = context.get("active_kommo_lead_id")
        if active_lead_id:
            return await kommo_service.get_lead_details(int(active_lead_id))
    if not clean_query:
        raise LeadResolutionError("Укажи Kommo ID, номер, клиента или часть названия сделки.")
    result = await kommo_service.search_open_leads(clean_query, limit=8)
    leads = result.get("leads") or []
    if not leads:
        raise LeadResolutionError(f"Не нашёл открытую сделку по запросу «{clean_query}».")
    if len(leads) > 1:
        raise LeadResolutionError(
            "Нашёл несколько сделок. Укажи точнее или используй Kommo ID.",
            candidates=leads,
        )
    return await kommo_service.get_lead_details(int(leads[0]["id"]))


def format_candidates(candidates: list[dict[str, Any]]) -> str:
    lines = ["<b>Нашёл несколько сделок</b>", "", "Выбери нужную карточку кнопкой:"]
    for item in candidates[:8]:
        lead_id = item.get("id")
        name = html.escape(str(item.get("name") or lead_id))
        lines.append(f"• <code>{lead_id}</code> — {name}")
    return "\n".join(lines)


def candidates_markup(candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
    rows: list[list[dict[str, str]]] = []
    for item in candidates[:8]:
        lead_id = item.get("id")
        if not isinstance(lead_id, int):
            continue
        raw_name = " ".join(str(item.get("name") or lead_id).split())
        label = raw_name[:42] + ("…" if len(raw_name) > 42 else "")
        rows.append(
            [
                {
                    "text": f"{label} · {lead_id}",
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
    lines = [
        f"<b>📌 {html.escape(str(lead.get('name') or lead.get('id') or 'Сделка'))}</b>",
        "",
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
    if lead.get("url"):
        lines.extend(["", f'<a href="{html.escape(str(lead["url"]), quote=True)}">Открыть в Kommo</a>'])
    return "\n".join(lines)


def lead_summary_for_ai(lead: dict[str, Any]) -> dict[str, Any]:
    contacts = lead.get("contacts") or []
    return {
        "id": lead.get("id"),
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
