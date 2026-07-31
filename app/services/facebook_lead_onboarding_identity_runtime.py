"""Non-recursive identity composition for Facebook lead onboarding."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.services import operator_experience_runtime
from app.services.lead_matching_service import lead_contact_snapshot as _base_snapshot

logger = logging.getLogger(__name__)
_INSTALLED = False


def _clean(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()


def _snapshot(details: dict[str, Any]) -> dict[str, Any]:
    base = dict(_base_snapshot(details) or {})
    phones = [_clean(value) for value in base.get("phones") or [] if _clean(value)]
    emails = [_clean(value) for value in base.get("emails") or [] if _clean(value)]
    contact_name = _clean(base.get("contact_name"))
    company = _clean(base.get("company"))

    for entity in list(details.get("contacts") or []) + [details]:
        for marker, raw_value in operator_experience_runtime._custom_values(entity):
            marker_folded = marker.casefold()
            value = _clean(raw_value)
            if not value:
                continue
            if any(
                token in marker_folded
                for token in ("phone", "telefon", "numer", "телефон", "номер")
            ) and value.casefold() not in {item.casefold() for item in phones}:
                phones.append(value)
            if (
                "@" in value
                or any(
                    token in marker_folded
                    for token in ("email", "e-mail", "poczta", "почт")
                )
            ) and "@" in value and value.casefold() not in {
                item.casefold() for item in emails
            }:
                emails.append(value)
            if not contact_name and any(
                token in marker_folded
                for token in (
                    "contact name",
                    "full name",
                    "imię",
                    "imie",
                    "клиент",
                    "имя",
                )
            ):
                contact_name = value
            if not company and any(
                token in marker_folded for token in ("company", "firma", "компания")
            ):
                company = value

    return {
        **base,
        "phones": phones,
        "emails": emails,
        "contact_name": contact_name or None,
        "company": company or None,
    }


async def _discover() -> dict[str, Any]:
    """Use normal enriched contacts first, then Meta lead fields as a fallback."""
    from app.services import facebook_lead_onboarding_runtime as onboarding

    rows, unsorted = await asyncio.gather(
        asyncio.to_thread(
            onboarding.google_sheets_service.get_rows,
            force_refresh=True,
        ),
        onboarding.kommo_service.get_all_unsorted_leads(
            pipeline_id=(
                onboarding.settings.kommo_unreviewed_pipeline_id
                or onboarding.settings.lead_status_sync_pipeline_id
                or None
            )
        ),
    )
    leads = await onboarding.kommo_service.enrich_leads_with_contacts(
        list(unsorted.get("leads") or [])
    )
    used_rows: set[int] = set()
    queue: list[dict[str, Any]] = []
    unmatched: list[dict[str, Any]] = []

    for lead in sorted(leads, key=lambda item: item.get("created_at") or 0):
        if not onboarding._is_facebook(lead):
            continue
        lead_id = int(lead.get("id") or 0)
        if not lead_id:
            continue

        identity = {
            "phones": list(lead.get("phones") or []),
            "emails": list(lead.get("emails") or []),
            "contact_name": lead.get("contact_name"),
            "company": lead.get("company"),
        }
        if not identity["phones"] and not identity["emails"]:
            try:
                details = await onboarding.kommo_service.get_lead_details(lead_id)
                identity = _snapshot(details)
            except Exception as exc:
                logger.warning("Could not read Meta form fields for lead %s: %s", lead_id, exc)

        match = onboarding.match_lead_to_rows(
            phones=identity.get("phones"),
            emails=identity.get("emails"),
            contact_name=identity.get("contact_name"),
            company=identity.get("company"),
            product_hint=lead.get("name"),
            rows=rows,
            require_lead_number=False,
        )
        candidate = match.single
        if (
            candidate is None
            or candidate.score < 60
            or candidate.row.row_number in used_rows
        ):
            unmatched.append(
                {
                    "lead_id": lead_id,
                    "name": lead.get("name"),
                    "reason": (
                        "sheet_row_already_used"
                        if candidate is not None
                        and candidate.row.row_number in used_rows
                        else "ambiguous_or_no_exact_match"
                    ),
                    "candidate_rows": [
                        item.row.row_number for item in match.candidates
                    ],
                }
            )
            continue
        used_rows.add(candidate.row.row_number)
        queue.append(
            {
                "lead_id": lead_id,
                "row_number": candidate.row.row_number,
                "matched_by": candidate.matched_by,
            }
        )
    return {"items": queue, "unmatched": unmatched}


def install_facebook_lead_onboarding_identity_runtime() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True

    from app.services import facebook_lead_onboarding_hardening_runtime as hardening
    from app.services import facebook_lead_onboarding_runtime as onboarding

    hardening._identity_snapshot = _snapshot
    onboarding.lead_contact_snapshot = _snapshot
    onboarding.discover = _discover
    logger.info("Facebook lead onboarding identity runtime installed")
