"""Resolve client contact details from Kommo linked contacts with lead-field fallback."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ResolvedContact:
    contact_id: int | None
    name: str | None
    phone_display: str | None
    phone_normalized: str | None
    email: str | None
    source: str


def normalize_phone(phone: str | None) -> str | None:
    raw = str(phone or "").strip()
    if not raw:
        return None
    digits = re.sub(r"\D", "", raw)
    if digits.startswith("00"):
        digits = digits[2:]
    if not digits:
        return None
    # Keep leading country code; strip a single trunk 0 only when already international.
    if len(digits) > 10 and digits.startswith("0"):
        digits = digits.lstrip("0")
    if not 8 <= len(digits) <= 15:
        return None
    return digits


def format_phone_display(phone: str | None) -> str | None:
    normalized = normalize_phone(phone)
    if not normalized:
        raw = str(phone or "").strip()
        return raw or None
    if normalized.startswith("48") and len(normalized) == 11:
        local = normalized[2:]
        return f"+48 {local[0:3]} {local[3:6]} {local[6:9]}"
    if normalized.startswith("380") and len(normalized) == 12:
        local = normalized[3:]
        return f"+380 {local[0:2]} {local[2:5]} {local[5:7]} {local[7:9]}"
    return f"+{normalized}"


def whatsapp_url(phone: str | None) -> str | None:
    digits = normalize_phone(phone)
    if not digits:
        return None
    return f"https://wa.me/{digits}"


def _first_phone(values: list[Any] | None) -> str | None:
    for value in values or []:
        text = str(value or "").strip()
        if text:
            return text
    return None


def _first_email(values: list[Any] | None) -> str | None:
    for value in values or []:
        text = str(value or "").strip()
        if text and "@" in text:
            return text
    return None


def _lead_field_values(lead: dict[str, Any], *keys: str) -> list[str]:
    found: list[str] = []
    custom = lead.get("custom_fields") or {}
    if isinstance(custom, dict):
        for key in keys:
            value = custom.get(key)
            if value:
                found.append(str(value))
    elif isinstance(custom, list):
        for field in custom:
            marker = f"{field.get('name') or ''} {field.get('code') or ''}".casefold()
            if any(key.casefold() in marker for key in keys):
                value = field.get("value")
                if value:
                    found.append(str(value))
    for key in keys:
        if lead.get(key):
            found.append(str(lead[key]))
    return found


def resolve_contact(lead: dict[str, Any]) -> ResolvedContact:
    """Pick the best contact for WhatsApp/email from linked contacts, then lead fields."""
    contacts = list(lead.get("contacts") or [])

    def _phones_from_contact(contact: dict[str, Any]) -> list[str]:
        phones = list(contact.get("phones") or [])
        # Flattened custom fields from get_lead_details may still hold the number
        # when field_code was missing at hydration time.
        custom = contact.get("custom_fields")
        if isinstance(custom, dict):
            for key, value in custom.items():
                marker = str(key).casefold()
                if any(token in marker for token in ("phone", "телефон", "tel", "mobile")):
                    if value:
                        phones.append(str(value))
        elif isinstance(custom, list):
            for field in custom:
                marker = f"{field.get('name') or ''} {field.get('code') or ''}".casefold()
                if any(token in marker for token in ("phone", "телефон", "tel", "mobile")):
                    if field.get("value"):
                        phones.append(str(field["value"]))
        return phones

    # 1) Primary linked contact with a usable phone or email.
    for contact in contacts:
        phone = _first_phone(_phones_from_contact(contact))
        email = _first_email(contact.get("emails"))
        if phone or email:
            return ResolvedContact(
                contact_id=int(contact["id"]) if contact.get("id") else None,
                name=str(contact.get("name") or "").strip() or None,
                phone_display=format_phone_display(phone) if phone else None,
                phone_normalized=normalize_phone(phone) if phone else None,
                email=email,
                source="kommo_contact",
            )

    # 2) Remaining linked contacts even without channels (name only).
    if contacts:
        contact = contacts[0]
        return ResolvedContact(
            contact_id=int(contact["id"]) if contact.get("id") else None,
            name=str(contact.get("name") or "").strip() or None,
            phone_display=None,
            phone_normalized=None,
            email=_first_email(contact.get("emails")),
            source="kommo_contact_incomplete",
        )

    # 3) Lead fields as fallback.
    phone = _first_phone(_lead_field_values(lead, "phone", "телефон", "PHONE"))
    email = _first_email(_lead_field_values(lead, "email", "EMAIL", "e-mail"))
    if phone or email:
        return ResolvedContact(
            contact_id=None,
            name=str(lead.get("name") or "").strip() or None,
            phone_display=format_phone_display(phone) if phone else None,
            phone_normalized=normalize_phone(phone) if phone else None,
            email=email,
            source="kommo_lead_fields",
        )

    return ResolvedContact(
        contact_id=None,
        name=str(lead.get("name") or "").strip() or None,
        phone_display=None,
        phone_normalized=None,
        email=None,
        source="missing",
    )


def resolve_all_contacts(lead: dict[str, Any]) -> list[ResolvedContact]:
    results: list[ResolvedContact] = []
    for contact in lead.get("contacts") or []:
        phone = _first_phone(contact.get("phones"))
        email = _first_email(contact.get("emails"))
        results.append(
            ResolvedContact(
                contact_id=int(contact["id"]) if contact.get("id") else None,
                name=str(contact.get("name") or "").strip() or None,
                phone_display=format_phone_display(phone) if phone else None,
                phone_normalized=normalize_phone(phone) if phone else None,
                email=email,
                source="kommo_contact",
            )
        )
    if not results:
        primary = resolve_contact(lead)
        if primary.source != "missing" or primary.name:
            results.append(primary)
    return results
