"""Build the communication context used by B&BS client-facing AI drafts."""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

KNOWLEDGE_VERSION = "2026-07-29-v1"
_MAX_TEXT = 4000
_ROOT = Path(__file__).resolve().parents[2]
_PLAYBOOK_PATH = _ROOT / "data" / "bbs_knowledge" / "communication_playbook.json"


def _clean(value: Any, *, limit: int = _MAX_TEXT) -> str:
    text = " ".join(str(value or "").replace("\x00", " ").split())
    return text[:limit]


def _direction(value: Any) -> str | None:
    raw = str(value or "").strip().casefold()
    if raw in {"incoming", "in", "client", "customer"}:
        return "incoming"
    if raw in {"outgoing", "out", "manager", "us", "operator"}:
        return "outgoing"
    return None


def _normalized_message(item: dict[str, Any], *, default_origin: str | None = None) -> dict[str, Any] | None:
    text = _clean(item.get("text") or item.get("body") or item.get("message"), limit=3000)
    if not text:
        return None
    direction = _direction(item.get("direction") or item.get("type"))
    if direction is None:
        author_type = str(item.get("author_type") or "").casefold()
        direction = "incoming" if author_type in {"client", "contact"} else None
    return {
        "id": item.get("id"),
        "direction": direction,
        "text": text,
        "created_at": item.get("created_at"),
        "origin": _clean(item.get("origin") or default_origin or "chat", limit=64),
        "author_name": _clean(item.get("author_name"), limit=120) or None,
    }


def _outgoing_note_message(note: dict[str, Any]) -> dict[str, Any] | None:
    """Use only notes that clearly contain an actually sent client message."""
    text = str(note.get("text") or "").strip()
    if not text:
        return None
    marker = "[BBS-MSG-" in text
    correspondence_prefix = bool(
        re.search(r"(?im)^(?:whatsapp|facebook|instagram|email|e-mail)\s*[:—-]", text)
    )
    if not marker and not correspondence_prefix:
        return None
    if marker and "Текст:" in text:
        text = text.split("Текст:", 1)[1].strip()
    return {
        "id": note.get("id"),
        "direction": "outgoing",
        "text": _clean(text, limit=3000),
        "created_at": note.get("created_at"),
        "origin": "kommo_note",
        "author_name": None,
    }


def _question_sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[?？])\s+|\n+", text)
    return [part.strip()[:500] for part in parts if "?" in part or "？" in part][:8]


_PROMISE_MARKERS = (
    "wrócę",
    "prześlemy",
    "przygotujemy",
    "sprawdzimy",
    "skontaktuję",
    "wyślę",
    "уточню",
    "вернусь",
    "подготовим",
    "отправлю",
    "проверим",
    "надішлю",
    "перевіримо",
    "підготуємо",
    "i will",
    "we will",
)


def _promise_sentences(text: str) -> list[str]:
    sentences = re.split(r"(?<=[.!?])\s+|\n+", text)
    result: list[str] = []
    for sentence in sentences:
        folded = sentence.casefold()
        if any(marker in folded for marker in _PROMISE_MARKERS):
            result.append(sentence.strip()[:500])
    return result[:8]


def _client_tone(messages: list[dict[str, Any]]) -> str:
    incoming = " ".join(
        str(item.get("text") or "") for item in messages[-8:] if item.get("direction") == "incoming"
    ).casefold()
    if not incoming:
        return "unknown"
    if any(token in incoming for token in ("proszę tylko", "nie traćmy", "nie zajmuj", "natychmiast", "pilne", "срочно")):
        return "direct_or_impatient"
    if incoming.count("?") >= 4:
        return "inquisitive"
    if any(token in incoming for token in ("dziękuję", "proszę", "pozdrawiam", "дякую", "спасибо")):
        return "polite"
    return "neutral"


def _confirmed_requirements(lead: dict[str, Any]) -> list[str]:
    result: list[str] = []
    for key in ("name", "price", "pipeline_name", "status_name"):
        value = _clean(lead.get(key), limit=500)
        if value:
            result.append(f"{key}: {value}")
    fields = lead.get("custom_fields") or {}
    if isinstance(fields, dict):
        iterable = fields.items()
    elif isinstance(fields, list):
        iterable = (
            (
                item.get("name") or item.get("code") or item.get("field_name") or "field",
                item.get("value"),
            )
            for item in fields
            if isinstance(item, dict)
        )
    else:
        iterable = ()
    for key, value in iterable:
        clean_value = _clean(value, limit=800)
        if clean_value:
            result.append(f"{_clean(key, limit=120)}: {clean_value}")
        if len(result) >= 18:
            break
    return result


@lru_cache(maxsize=1)
def load_communication_playbook() -> dict[str, Any]:
    try:
        data = json.loads(_PLAYBOOK_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {
            "version": KNOWLEDGE_VERSION,
            "priority_order": [
                "latest_client_conversation",
                "confirmed_kommo_facts",
                "manager_request",
                "approved_examples",
                "general_bbs_rules",
            ],
            "global_rules": [
                "Continue the existing conversation instead of starting again.",
                "Never invent prices, availability, certificates, deadlines or completed checks.",
                "Use one clear objective and one next action.",
            ],
        }
    if not isinstance(data, dict):
        return {"version": KNOWLEDGE_VERSION}
    return data


def playbook_for_prompt(*, kind: str, language: str, channel: str) -> dict[str, Any]:
    data = load_communication_playbook()
    return {
        "version": data.get("version") or KNOWLEDGE_VERSION,
        "company_role": data.get("company_role") or {},
        "priority_order": data.get("priority_order") or [],
        "global_rules": data.get("global_rules") or [],
        "channel_rules": (data.get("channel_rules") or {}).get(channel)
        or (data.get("channel_rules") or {}).get(kind)
        or [],
        "language_rules": (data.get("language_rules") or {}).get(language) or [],
        "kind_rules": (data.get("kind_rules") or {}).get(kind) or [],
        "legal_boundaries": data.get("legal_boundaries") or [],
        "reviewer_checks": data.get("reviewer_checks") or [],
    }


def build_communication_context(
    lead: dict[str, Any],
    *,
    manager_request: str = "",
    max_messages: int = 30,
) -> dict[str, Any]:
    max_messages = max(5, min(int(max_messages), 30))
    chat = dict(lead.get("chat_context") or {})
    messages: list[dict[str, Any]] = []
    for item in chat.get("messages") or []:
        if isinstance(item, dict):
            normalized = _normalized_message(item, default_origin=chat.get("origin"))
            if normalized:
                messages.append(normalized)

    for item in lead.get("conversation") or []:
        if isinstance(item, dict):
            normalized = _normalized_message(item)
            if normalized:
                messages.append(normalized)

    for note in lead.get("notes") or []:
        if isinstance(note, dict):
            normalized = _outgoing_note_message(note)
            if normalized:
                messages.append(normalized)

    def sort_key(item: dict[str, Any]) -> tuple[int, str]:
        created = item.get("created_at")
        try:
            return (int(created or 0), str(item.get("id") or ""))
        except (TypeError, ValueError):
            return (0, str(item.get("id") or ""))

    messages.sort(key=sort_key)
    messages = messages[-max_messages:]

    incoming = [item for item in messages if item.get("direction") == "incoming"]
    outgoing = [item for item in messages if item.get("direction") == "outgoing"]
    last = messages[-1] if messages else None
    analysis = dict(chat.get("analysis") or {})

    waiting_on = analysis.get("waiting_on")
    if waiting_on not in {"us", "client"} and last:
        if last.get("direction") == "incoming":
            waiting_on = "us"
        elif last.get("direction") == "outgoing":
            waiting_on = "client"

    open_questions: list[str] = []
    if incoming:
        open_questions = _question_sentences(str(incoming[-1].get("text") or ""))

    promises: list[str] = []
    for item in outgoing[-6:]:
        promises.extend(_promise_sentences(str(item.get("text") or "")))
        if len(promises) >= 8:
            break

    context = {
        "knowledge_version": KNOWLEDGE_VERSION,
        "lead": {
            "id": lead.get("id"),
            "internal_lead_number": lead.get("internal_lead_number"),
            "name": _clean(lead.get("name"), limit=500),
            "pipeline_name": _clean(lead.get("pipeline_name"), limit=200),
            "status_name": _clean(lead.get("status_name"), limit=200),
            "price": lead.get("price"),
        },
        "conversation": {
            "available": bool(messages),
            "origin": chat.get("origin"),
            "last_messages": messages,
            "last_client_message": incoming[-1]["text"] if incoming else "",
            "last_manager_message": outgoing[-1]["text"] if outgoing else "",
            "waiting_on": waiting_on,
            "client_tone": _client_tone(messages),
            "open_questions": open_questions,
            "promises_made": promises,
            "next_expected_action": analysis.get("recommended_action"),
            "source_summary": analysis.get("summary"),
        },
        "confirmed_requirements": _confirmed_requirements(lead),
        "manager_request": _clean(manager_request, limit=4000),
    }
    return context
