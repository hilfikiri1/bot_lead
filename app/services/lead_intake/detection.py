"""Step 1: find new Facebook leads and capture their permanent identity.

Uses the existing ``kommo_service.get_all_unreviewed_leads()`` — the
function that actually reads Kommo's Incoming/Unsorted inbox
(``/api/v4/leads/unsorted``) — instead of the pipeline-scoped
``get_all_leads_for_status_sync()`` that the legacy ``/status_sync`` report
uses. New Facebook Lead Ads submissions land in that inbox, not in a normal
pipeline, which is why the legacy report could report "Надёжно найдено в
Kommo: 0" even for leads that genuinely exist in Kommo.

Once a lead is detected, every fact from this module is written to
``LeadProcessingJob`` immediately. The mutable ``Facebook #...`` title is
never used again after this point — every later step addresses the lead
exclusively by its permanent ``kommo_lead_id``.
"""

from __future__ import annotations

import re
from typing import Any

from app.services import contact_resolver, kommo_service
from app.services.lead_intake.matching import LeadSnapshot

FACEBOOK_TITLE_RE = re.compile(r"^\s*Facebook\s*[#№]", re.IGNORECASE)
FACEBOOK_ID_IN_TITLE_RE = re.compile(r"[#№]\s*([0-9]{4,})")

_PRODUCT_KEYWORDS = (
    "product", "produkt", "товар", "запрос", "zapytanie", "interesuje",
    "co chcesz", "czego szuka", "jakiego produktu",
)
_BUDGET_KEYWORDS = ("budget", "бюджет", "budżet", "budzet")
_REGION_KEYWORDS = (
    "region", "регион", "województwo", "wojewodztwo", "miasto", "city",
    "lokalizacja", "область",
)
_CHANNEL_KEYWORDS = (
    "channel", "канал", "kontakt", "preferowany kontakt", "contact method",
    "sposób kontaktu", "forma kontaktu",
)
_FACEBOOK_ID_KEYWORDS = ("facebook", "fb_lead", "leadgen", "lead_id", "id leada", "id zgłoszenia")


def is_new_facebook_lead_title(title: str | None) -> bool:
    return bool(FACEBOOK_TITLE_RE.match(str(title or "")))


async def find_candidate_leads() -> list[dict[str, Any]]:
    """Return raw Kommo unsorted/incoming entries whose title is a Facebook lead."""
    result = await kommo_service.get_all_unreviewed_leads()
    candidates = [
        lead
        for lead in result.get("leads") or []
        if is_new_facebook_lead_title(lead.get("name"))
    ]
    # Newest Facebook submissions first — managers should see fresh leads
    # before older unsorted backlog entries.
    candidates.sort(key=lambda item: item.get("created_at") or 0, reverse=True)
    return candidates


def _field_matches(field: dict[str, str], keywords: tuple[str, ...]) -> bool:
    haystack = f"{field.get('name', '')} {field.get('code', '')}".casefold()
    return any(keyword in haystack for keyword in keywords)


def _extract_field(custom_fields: list[dict[str, str]], keywords: tuple[str, ...]) -> str | None:
    for field in custom_fields:
        if _field_matches(field, keywords):
            value = str(field.get("value") or "").strip()
            if value:
                return value
    return None


def extract_facebook_lead_id(
    *,
    metadata: dict[str, Any] | None,
    custom_fields: list[dict[str, str]],
    title: str | None,
) -> str | None:
    metadata = metadata or {}
    for key in ("lead_id", "leadgen_id", "facebook_lead_id", "form_id"):
        value = metadata.get(key)
        if value not in (None, ""):
            return str(value)
    for field in custom_fields:
        if _field_matches(field, _FACEBOOK_ID_KEYWORDS):
            value = str(field.get("value") or "").strip()
            if value:
                return value
    match = FACEBOOK_ID_IN_TITLE_RE.search(str(title or ""))
    return match.group(1) if match else None


def extract_facebook_technical_tag(metadata: dict[str, Any] | None, unsorted_source_name: str | None) -> str | None:
    metadata = metadata or {}
    parts = [
        str(metadata.get(key)) for key in ("form_name", "form_id", "page_id", "ad_id", "campaign_name")
        if metadata.get(key) not in (None, "")
    ]
    if unsorted_source_name:
        parts.append(str(unsorted_source_name))
    return "; ".join(parts) or None


async def build_snapshot(
    kommo_lead_id: int,
    *,
    unsorted_metadata: dict[str, Any] | None = None,
    unsorted_source_name: str | None = None,
) -> tuple[dict[str, Any], LeadSnapshot]:
    """Fetch the full Kommo lead and build the durable Step-1 snapshot.

    Returns ``(raw_details, snapshot)``; ``raw_details`` is stored verbatim
    in ``LeadProcessingJob.raw_snapshot_json`` for audit/debugging.
    """
    details = await kommo_service.get_lead_details(kommo_lead_id)
    resolved = contact_resolver.resolve_contact(details)
    custom_fields = list(details.get("custom_fields") or [])

    facebook_lead_id = extract_facebook_lead_id(
        metadata=unsorted_metadata, custom_fields=custom_fields, title=details.get("name")
    )
    technical_tag = extract_facebook_technical_tag(unsorted_metadata, unsorted_source_name)

    snapshot = LeadSnapshot(
        facebook_lead_id=facebook_lead_id,
        phone=resolved.phone_display or resolved.phone_normalized,
        email=resolved.email,
        name=resolved.name or details.get("name"),
        product=_extract_field(custom_fields, _PRODUCT_KEYWORDS),
        region=_extract_field(custom_fields, _REGION_KEYWORDS),
        created_at=details.get("created_at"),
    )

    snapshot_dict = {
        "kommo_lead_id": kommo_lead_id,
        "original_title": details.get("name"),
        "facebook_lead_id": facebook_lead_id,
        "facebook_technical_tag": technical_tag,
        "source": "facebook_lead_ads",
        "name": snapshot.name,
        "phone": snapshot.phone,
        "email": snapshot.email,
        "product": snapshot.product,
        "budget": _extract_field(custom_fields, _BUDGET_KEYWORDS),
        "contact_channel": _extract_field(custom_fields, _CHANNEL_KEYWORDS),
        "region": snapshot.region,
        "created_at": snapshot.created_at,
        "custom_fields": custom_fields,
        "unsorted_metadata": unsorted_metadata or {},
    }
    return snapshot_dict, snapshot
