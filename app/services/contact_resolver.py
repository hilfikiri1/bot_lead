"""Resolve client contact details from Kommo linked contacts with lead-field fallback."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


PHONE_FIELD_TOKENS = (
    "phone",
    "телефон",
    "телефону",
    "мобил",
    "tel.",
    "tel ",
    "telefon",
    "mobile",
    "whatsapp",
    "номер телефона",
    "номер телефону",
    "numer telefon",
    "numer tel",
    "swój numer",
    "swoj numer",
    "proszę podać sw",
    "prosze podac sw",
)

# Shorter tokens applied only when the value itself looks like a phone number,
# so we do not treat every Polish form answer containing "numer" as a phone.
PHONE_VALUE_HINT_TOKENS = ("numer", "тел", "phone", "tel", "mobile", "whatsapp")


@dataclass(frozen=True)
class ResolvedContact:
    contact_id: int | None
    name: str | None
    phone_display: str | None
    phone_normalized: str | None
    email: str | None
    source: str


def _looks_like_phone_value(value: str | None) -> bool:
    raw = str(value or "").strip()
    if not raw or "@" in raw:
        return False
    digits = re.sub(r"\D", "", raw)
    if not 8 <= len(digits) <= 15:
        return False
    # Reject long free-text answers that happen to contain some digits.
    non_digit = re.sub(r"[\d\s+\-().]", "", raw)
    return len(non_digit) <= 3


def default_country_for_lead(lead: dict[str, Any] | None) -> str | None:
    """Best-effort ISO-ish country hint for local mobile numbers (pl/uk)."""
    if not lead:
        return None
    haystack = " ".join(
        (
            str(lead.get("pipeline_name") or ""),
            str(lead.get("name") or ""),
            str(lead.get("language") or ""),
            str(lead.get("communication_language") or ""),
        )
    ).casefold()
    if any(
        token in haystack
        for token in (
            "польш",
            "польск",
            "polsk",
            "poland",
            "polska",
            "warszaw",
            "kraków",
            "krakow",
        )
    ):
        return "pl"
    if any(
        token in haystack
        for token in ("украин", "україн", "ukrain", "киев", "київ", "львів", "одес")
    ):
        return "uk"

    try:
        from app.services.client_language_service import infer_direction_language

        direction = infer_direction_language(lead)
        if direction and direction[0] in {"pl", "uk"}:
            return direction[0]
    except Exception:
        pass
    return None


def normalize_phone(
    phone: str | None,
    *,
    default_country: str | None = None,
) -> str | None:
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
    # Local PL/UA mobiles often arrive without country code from form fields.
    if len(digits) == 9:
        country = (default_country or "").casefold()
        if country in {"pl", "48", "poland", "polska"}:
            digits = "48" + digits
        elif country in {"uk", "ua", "380", "ukraine"}:
            digits = "380" + digits
    if not 8 <= len(digits) <= 15:
        return None
    return digits


def format_phone_display(
    phone: str | None,
    *,
    default_country: str | None = None,
) -> str | None:
    normalized = normalize_phone(phone, default_country=default_country)
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


def whatsapp_url(
    phone: str | None,
    *,
    default_country: str | None = None,
) -> str | None:
    digits = normalize_phone(phone, default_country=default_country)
    if not digits:
        return None
    return f"https://wa.me/{digits}"


def _first_phone(values: list[Any] | None) -> str | None:
    for value in values or []:
        text = str(value or "").strip()
        if text and _looks_like_phone_value(text):
            return text
        if text and any(ch.isdigit() for ch in text):
            # Keep slightly looser fallback for already-validated channel values.
            digits = re.sub(r"\D", "", text)
            if 8 <= len(digits) <= 15:
                return text
    return None


def _first_email(values: list[Any] | None) -> str | None:
    for value in values or []:
        text = str(value or "").strip()
        if text and "@" in text:
            return text
    return None


def _field_marker(name: Any = None, code: Any = None) -> str:
    return f"{name or ''} {code or ''}".casefold()


def _marker_is_phone_field(marker: str, *, value: str | None = None) -> bool:
    if any(token in marker for token in PHONE_FIELD_TOKENS):
        return True
    if value and _looks_like_phone_value(value):
        return any(token in marker for token in PHONE_VALUE_HINT_TOKENS)
    return False


def _phones_from_custom(custom: Any) -> list[str]:
    phones: list[str] = []
    if isinstance(custom, dict):
        for key, value in custom.items():
            text = str(value or "").strip()
            if not text:
                continue
            marker = str(key).casefold()
            if _marker_is_phone_field(marker, value=text):
                phones.append(text)
    elif isinstance(custom, list):
        for field in custom:
            text = str(field.get("value") or "").strip()
            if not text:
                continue
            marker = _field_marker(field.get("name"), field.get("code"))
            if _marker_is_phone_field(marker, value=text):
                phones.append(text)
    return phones


def _lead_field_values(lead: dict[str, Any], *keys: str) -> list[str]:
    found: list[str] = []
    custom = lead.get("custom_fields") or {}
    if isinstance(custom, dict):
        for key in keys:
            value = custom.get(key)
            if value:
                found.append(str(value))
        # Also scan dict keys for phone-like labels (form questions).
        found.extend(_phones_from_custom(custom))
    elif isinstance(custom, list):
        for field in custom:
            marker = _field_marker(field.get("name"), field.get("code"))
            value = field.get("value")
            if value and (
                any(key.casefold() in marker for key in keys)
                or _marker_is_phone_field(marker, value=str(value))
            ):
                found.append(str(value))
    for key in keys:
        if lead.get(key):
            found.append(str(lead[key]))
    # Deduplicate while preserving order.
    seen: set[str] = set()
    unique: list[str] = []
    for item in found:
        key = item.casefold()
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique


def _phones_from_contact(contact: dict[str, Any]) -> list[str]:
    phones = [str(p) for p in (contact.get("phones") or []) if str(p or "").strip()]
    phones.extend(_phones_from_custom(contact.get("custom_fields")))
    return phones


def _lead_phones(lead: dict[str, Any]) -> list[str]:
    return _lead_field_values(
        lead,
        "phone",
        "телефон",
        "PHONE",
        "telefon",
        "numer",
        "Номер телефона",
    )


def resolve_contact(lead: dict[str, Any]) -> ResolvedContact:
    """Pick the best contact for WhatsApp/email from linked contacts, then lead fields.

    Form-captured numbers often live on the lead (e.g. Polish "Proszę podać swój
    numer…") while the Messenger contact has an empty PHONE field. Prefer the
    linked contact identity, but fill the phone from lead custom fields when needed.
    """
    contacts = list(lead.get("contacts") or [])
    country = default_country_for_lead(lead)
    lead_phone = _first_phone(_lead_phones(lead))
    lead_email = _first_email(_lead_field_values(lead, "email", "EMAIL", "e-mail"))

    # 1) Primary linked contact with a usable phone or email.
    for contact in contacts:
        phone = _first_phone(_phones_from_contact(contact)) or lead_phone
        email = _first_email(contact.get("emails")) or lead_email
        if phone or email:
            source = "kommo_contact"
            if phone and phone == lead_phone and not _first_phone(_phones_from_contact(contact)):
                source = "kommo_contact_lead_phone"
            return ResolvedContact(
                contact_id=int(contact["id"]) if contact.get("id") else None,
                name=str(contact.get("name") or "").strip() or None,
                phone_display=format_phone_display(phone, default_country=country)
                if phone
                else None,
                phone_normalized=normalize_phone(phone, default_country=country)
                if phone
                else None,
                email=email,
                source=source,
            )

    # 2) Remaining linked contacts even without channels (name only).
    if contacts:
        contact = contacts[0]
        return ResolvedContact(
            contact_id=int(contact["id"]) if contact.get("id") else None,
            name=str(contact.get("name") or "").strip() or None,
            phone_display=None,
            phone_normalized=None,
            email=_first_email(contact.get("emails")) or lead_email,
            source="kommo_contact_incomplete",
        )

    # 3) Lead fields as fallback when no contacts are linked.
    if lead_phone or lead_email:
        return ResolvedContact(
            contact_id=None,
            name=str(lead.get("name") or "").strip() or None,
            phone_display=format_phone_display(lead_phone, default_country=country)
            if lead_phone
            else None,
            phone_normalized=normalize_phone(lead_phone, default_country=country)
            if lead_phone
            else None,
            email=lead_email,
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
    country = default_country_for_lead(lead)
    lead_phone = _first_phone(_lead_phones(lead))
    for contact in lead.get("contacts") or []:
        phone = _first_phone(_phones_from_contact(contact)) or lead_phone
        email = _first_email(contact.get("emails"))
        results.append(
            ResolvedContact(
                contact_id=int(contact["id"]) if contact.get("id") else None,
                name=str(contact.get("name") or "").strip() or None,
                phone_display=format_phone_display(phone, default_country=country)
                if phone
                else None,
                phone_normalized=normalize_phone(phone, default_country=country)
                if phone
                else None,
                email=email,
                source="kommo_contact",
            )
        )
    if not results:
        primary = resolve_contact(lead)
        if primary.source != "missing" or primary.name:
            results.append(primary)
    return results
