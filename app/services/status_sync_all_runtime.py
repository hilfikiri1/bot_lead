"""Status-sync behavior for processing the full pending Sheets backlog.

The legacy manual onboarding flow intentionally capped empty-Y rows to a small
batch. For the owner workflow we want one confirmation to process every reliably
matched spreadsheet row, while still preserving the existing safety checks:
only empty Y cells are written, Kommo deals are never created, and ambiguous
matches remain untouched.

Contact export is also decoupled from later Kommo note/task success. Once a row
and a Kommo lead were reliably paired, its phone is already trusted enough for
the iPhone vCard bundle even if a subsequent non-critical Kommo write fails.
"""
from __future__ import annotations

from typing import Any

from app.services import lead_status_sync_service

_INSTALLED = False


def _clean(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()


def all_pending_rows(rows: list[Any]) -> list[Any]:
    """Return every product row whose internal number Y is still empty."""
    pending = [
        row
        for row in rows
        if _clean(getattr(row, "product", None))
        and not _clean(getattr(row, "lead_number", None))
    ]
    pending.sort(key=lambda item: int(getattr(item, "row_number", 0) or 0), reverse=True)
    return pending


def _contact_key(card: dict[str, Any]) -> tuple[str, str, str]:
    return (
        _clean(card.get("lead_number")),
        _clean(card.get("kommo_lead_id")),
        _clean(card.get("phone")),
    )


def _all_matched_contact_cards(result: dict[str, Any]) -> list[dict[str, Any]]:
    """Build the vCard source from all reliably matched actions with phones."""
    cards: dict[tuple[str, str, str], dict[str, Any]] = {}

    for raw in result.get("contact_cards") or []:
        card = dict(raw or {})
        if not _clean(card.get("phone")):
            continue
        cards[_contact_key(card)] = card

    report = dict(result.get("report") or {})
    for action in report.get("onboarding_actions") or []:
        card = dict((action or {}).get("contact_card") or {})
        if not _clean(card.get("phone")):
            continue
        cards.setdefault(_contact_key(card), card)

    return list(cards.values())


def install_status_sync_all_runtime() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True

    # Remove the historical 5-row cap. Ambiguous/unmatched rows are still left
    # untouched by the existing matcher and confirmation flow.
    lead_status_sync_service._newest_new_rows = all_pending_rows  # noqa: SLF001

    original_apply = lead_status_sync_service.apply_confirmed_report

    async def apply_confirmed_report_all_contacts(
        *,
        expected_digest: str,
        expected_updates_count: int,
    ) -> dict[str, Any]:
        result = await original_apply(
            expected_digest=expected_digest,
            expected_updates_count=expected_updates_count,
        )
        if result.get("stale"):
            return result

        result["contact_cards"] = _all_matched_contact_cards(result)
        result["contact_cards_count"] = len(result["contact_cards"])
        return result

    lead_status_sync_service.apply_confirmed_report = apply_confirmed_report_all_contacts
