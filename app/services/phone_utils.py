"""Canonical, reusable phone/email normalization.

Several parts of the codebase historically implemented their own phone
comparator (``lead_matching_service.normalize_phone`` truncates to the last
nine digits, ``kommo_service._normalize_phone`` used to keep raw digits with
no country-code awareness, ``operator_experience_phone_patch.phones_equivalent``
special-cased the Polish 9-vs-11-digit case). That fragmentation is a real
source of missed Kommo matches: a Facebook Lead Ads form usually submits a
local number such as ``728387128`` while the Google Sheets row (or a Kommo
contact created through a different channel) may hold ``+48 728 387 128`` —
digit-for-digit different strings that are the same phone number.

This module is the single source of truth for the new lead-intake engine
(``app.services.lead_intake``). It builds on top of the already-solid
``app.services.contact_resolver.normalize_phone`` implementation instead of
re-implementing normalization a fourth time.
"""

from __future__ import annotations

from urllib.parse import quote

from app.services import contact_resolver

# Buy & Bring Solutions leads processed by this pipeline are Poland-first
# (see ``KOMMO_POLAND_PIPELINE_ID``), so a bare 9-digit local number is
# assumed to be a Polish mobile number unless told otherwise.
DEFAULT_REGION = "pl"


def normalize_phone(value: str | None, *, default_region: str | None = DEFAULT_REGION) -> str | None:
    """Return a consistent, country-code-prefixed digit string, or ``None``.

    Examples::

        normalize_phone("728387128")             -> "48728387128"
        normalize_phone("+48 728 387 128")        -> "48728387128"
        normalize_phone("0048 728-387-128")       -> "48728387128"
        normalize_phone("12345")                  -> None
    """
    region_hint = (default_region or "").strip() or None
    return contact_resolver.normalize_phone(value, default_country=region_hint)


def to_e164(value: str | None, *, default_region: str | None = DEFAULT_REGION) -> str | None:
    """Return an E.164-compatible ``+<digits>`` value, or ``None``."""
    digits = normalize_phone(value, default_region=default_region)
    return f"+{digits}" if digits else None


def display_phone(value: str | None, *, default_region: str | None = DEFAULT_REGION) -> str | None:
    """Human-readable grouped phone (``+48 728 387 128``) for previews/notes."""
    return contact_resolver.format_phone_display(value, default_country=default_region)


def phones_match(
    left: str | None,
    right: str | None,
    *,
    default_region: str | None = DEFAULT_REGION,
) -> bool:
    a = normalize_phone(left, default_region=default_region)
    b = normalize_phone(right, default_region=default_region)
    return bool(a and b and a == b)


def normalize_email(value: str | None) -> str | None:
    """Trim whitespace and lower-case an email address."""
    text = str(value or "").strip().casefold()
    return text or None


def emails_match(left: str | None, right: str | None) -> bool:
    a = normalize_email(left)
    b = normalize_email(right)
    return bool(a and b and a == b)


def whatsapp_link(
    value: str | None,
    message: str | None = None,
    *,
    default_region: str | None = DEFAULT_REGION,
) -> str | None:
    """Build a ``wa.me`` deep link with an optional URL-encoded prefilled message."""
    url = contact_resolver.whatsapp_url(value, default_country=default_region)
    if not url:
        return None
    if message:
        return f"{url}?text={quote(message)}"
    return url
