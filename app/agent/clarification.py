"""Pending clarification state for incomplete multi-lead agent requests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from app.agent.contracts import AgentPlan
from app.agent.lead_refs import (
    LeadReference,
    LeadRefType,
    normalize_text,
    parse_lead_references,
    resolve_references,
)
from app.config import get_settings

settings = get_settings()

_CANCEL_TOKENS = {
    "/cancel",
    "отмена",
    "отмени",
    "отменить",
    "стоп",
    "cancel",
}


def clarification_ttl_minutes() -> int:
    return max(5, min(settings.agent_action_ttl_minutes, 24 * 60))


def is_cancel_command(text: str) -> bool:
    return normalize_text(text) in _CANCEL_TOKENS


def is_menu_command(text: str) -> bool:
    normalized = normalize_text(text)
    return normalized in {"/menu", "/start", "меню", "главное меню"}


def get_pending(context: dict[str, Any]) -> dict[str, Any] | None:
    pending = context.get("pending_clarification")
    if not isinstance(pending, dict):
        return None
    expires_at = pending.get("expires_at")
    if expires_at:
        try:
            expiry = datetime.fromisoformat(str(expires_at))
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)
            if expiry < datetime.now(timezone.utc):
                return None
        except ValueError:
            return None
    return pending


def _serialize_ref(ref: LeadReference) -> dict[str, Any]:
    return {
        "raw": ref.raw,
        "ref_type": ref.ref_type.value,
        "internal_lead_number": ref.internal_lead_number,
        "kommo_lead_id": ref.kommo_lead_id,
        "digest_position": ref.digest_position,
        "name_query": ref.name_query,
        "resolved_kommo_lead_id": ref.resolved_kommo_lead_id,
        "confidence": ref.confidence,
    }


def build_pending(
    *,
    plan: AgentPlan,
    original_text: str,
    source: str,
    requested_refs: list[LeadReference],
    resolved_leads: list[dict[str, Any]],
    unresolved_refs: list[LeadReference],
) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    return {
        "original_intent": plan.intent,
        "original_text": original_text[:10_000],
        "source": source,
        "requested_lead_references": [_serialize_ref(ref) for ref in requested_refs],
        "resolved_leads": [
            {
                "kommo_lead_id": int(lead.get("id") or lead.get("kommo_lead_id")),
                "internal_lead_number": lead.get("internal_lead_number"),
                "name": lead.get("name"),
                "url": lead.get("url"),
            }
            for lead in resolved_leads
        ],
        "unresolved_references": [_serialize_ref(ref) for ref in unresolved_refs],
        "task_title": plan.title,
        "note_text": plan.note_text,
        "draft_kind": plan.draft_kind,
        "due_at": plan.due_at,
        "language": plan.language,
        "duration_minutes": plan.duration_minutes,
        "reminder_minutes": plan.reminder_minutes,
        "event_type": plan.event_type,
        "created_at": now.isoformat(),
        "expires_at": (now + timedelta(minutes=clarification_ttl_minutes())).isoformat(),
        "metadata": {},
    }


def refs_from_pending(pending: dict[str, Any]) -> list[LeadReference]:
    refs: list[LeadReference] = []
    for raw in pending.get("unresolved_references") or []:
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


def resolved_leads_from_pending(pending: dict[str, Any]) -> list[dict[str, Any]]:
    return list(pending.get("resolved_leads") or [])


def looks_like_due_at(text: str) -> bool:
    normalized = normalize_text(text)
    if not normalized:
        return False
    if any(
        token in normalized
        for token in (
            "завтра",
            "сегодня",
            "послезавтра",
            "в ",
            ":",
            "час",
            "утра",
            "вечера",
            "январ",
            "феврал",
            "март",
            "апрел",
            "мая",
            "июн",
            "июл",
            "август",
            "сентябр",
            "октябр",
            "ноябр",
            "декабр",
        )
    ):
        return True
    return bool(__import__("re").search(r"\d{1,2}[./]\d{1,2}", normalized))


async def continue_pending(
    pending: dict[str, Any],
    text: str,
    context: dict[str, Any],
) -> tuple[AgentPlan | None, list[dict[str, Any]], list[LeadReference], str | None]:
    """Merge a short follow-up message into pending clarification."""
    plan = AgentPlan(
        intent=str(pending.get("original_intent") or "unknown"),
        mode="write",
        title=pending.get("task_title"),
        note_text=pending.get("note_text"),
        draft_kind=pending.get("draft_kind"),
        due_at=pending.get("due_at"),
        language=str(pending.get("language") or "auto"),
        duration_minutes=int(pending.get("duration_minutes") or 30),
        reminder_minutes=int(pending.get("reminder_minutes") or 30),
        event_type=str(pending.get("event_type") or "call"),
    )

    resolved = resolved_leads_from_pending(pending)
    unresolved = refs_from_pending(pending)

    if not plan.due_at and looks_like_due_at(text):
        plan.due_at = text.strip()
    elif not plan.note_text and plan.intent == "add_kommo_note" and unresolved:
        plan.note_text = text.strip()

    new_refs = parse_lead_references(text, context)
    if new_refs:
        result = await resolve_references(new_refs, context)
        resolved.extend(result.resolved)
        unresolved = result.unresolved
        if result.candidates and not result.resolved:
            return None, resolved, unresolved, "ambiguous"

    if unresolved:
        return plan, resolved, unresolved, None
    return plan, resolved, [], None


async def save_pending(
    db: AsyncSession,
    session: Any,
    pending: dict[str, Any],
) -> None:
    from app.agent.memory import update_context

    await update_context(db, session=session, values={"pending_clarification": pending})


async def clear_pending(db: AsyncSession, session: Any) -> None:
    from app.agent.memory import update_context

    await update_context(db, session=session, values={"pending_clarification": None})
