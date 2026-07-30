"""Match a Kommo lead to its Google Sheets row, without ever guessing.

Matching priority, per the lead-intake contract:

1. Exact Facebook Lead ID match.
2. Exact normalized phone AND normalized email match.
3. Exact, unique normalized phone match.
4. Exact, unique normalized email match.
5. Name / creation date / product / region are supporting evidence only —
   they can be shown to the manager but never auto-select a row.

The product name is never a unique matching key: it is intentionally never
compared here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from app.services import phone_utils
from app.services.google_sheets_service import SpreadsheetRow

MatchStatus = Literal["matched", "ambiguous", "not_found"]

# Mirrors the manual-match reasons required by the spec.
REASON_DUPLICATE_PHONE = "duplicate_phone"
REASON_DUPLICATE_EMAIL = "duplicate_email"
REASON_NO_MATCHING_ROW = "no_matching_row"
REASON_CONFLICTING_FACEBOOK_ID = "conflicting_facebook_id"
REASON_ASSIGNED_NUMBER_CONFLICT = "assigned_number_conflict"
REASON_MISSING_REQUIRED_FIELDS = "missing_required_fields"


@dataclass(frozen=True)
class LeadSnapshot:
    """Immutable identity/contact facts captured at Step 1 (detection time)."""

    facebook_lead_id: str | None
    phone: str | None
    email: str | None
    name: str | None = None
    product: str | None = None
    region: str | None = None
    created_at: str | None = None


@dataclass
class MatchOutcome:
    status: MatchStatus
    method: str | None = None
    score: int = 0
    row: SpreadsheetRow | None = None
    candidates: list[SpreadsheetRow] = field(default_factory=list)
    reason: str | None = None


def _norm_phone(value: str | None) -> str | None:
    return phone_utils.normalize_phone(value)


def _norm_email(value: str | None) -> str | None:
    return phone_utils.normalize_email(value)


def match_lead(snapshot: LeadSnapshot, rows: list[SpreadsheetRow]) -> MatchOutcome:
    fb_id = str(snapshot.facebook_lead_id or "").strip()
    phone = _norm_phone(snapshot.phone)
    email = _norm_email(snapshot.email)

    # Priority 1: exact Facebook Lead ID.
    if fb_id:
        fb_matches = [
            row
            for row in rows
            if str(getattr(row, "facebook_lead_id", "") or "").strip() == fb_id
        ]
        if len(fb_matches) == 1:
            return MatchOutcome(
                status="matched", method="facebook_lead_id", score=100, row=fb_matches[0]
            )
        if len(fb_matches) > 1:
            return MatchOutcome(
                status="ambiguous",
                method="facebook_lead_id",
                candidates=fb_matches,
                reason=REASON_CONFLICTING_FACEBOOK_ID,
            )

    # Priority 2: exact phone AND email.
    if phone and email:
        both = [
            row
            for row in rows
            if _norm_phone(row.phone) == phone and _norm_email(row.email) == email
        ]
        if len(both) == 1:
            return MatchOutcome(
                status="matched", method="phone_and_email", score=95, row=both[0]
            )
        if len(both) > 1:
            return MatchOutcome(
                status="ambiguous",
                method="phone_and_email",
                candidates=both,
                reason=REASON_DUPLICATE_PHONE,
            )

    # Priority 3: exact, unique phone.
    if phone:
        phone_rows = [row for row in rows if _norm_phone(row.phone) == phone]
        if len(phone_rows) == 1:
            return MatchOutcome(status="matched", method="phone", score=90, row=phone_rows[0])
        if len(phone_rows) > 1:
            return MatchOutcome(
                status="ambiguous",
                method="phone",
                candidates=phone_rows,
                reason=REASON_DUPLICATE_PHONE,
            )

    # Priority 4: exact, unique email.
    if email:
        email_rows = [row for row in rows if _norm_email(row.email) == email]
        if len(email_rows) == 1:
            return MatchOutcome(status="matched", method="email", score=80, row=email_rows[0])
        if len(email_rows) > 1:
            return MatchOutcome(
                status="ambiguous",
                method="email",
                candidates=email_rows,
                reason=REASON_DUPLICATE_EMAIL,
            )

    if not phone and not email and not fb_id:
        return MatchOutcome(status="not_found", reason=REASON_MISSING_REQUIRED_FIELDS)
    return MatchOutcome(status="not_found", reason=REASON_NO_MATCHING_ROW)


def supporting_evidence(snapshot: LeadSnapshot, row: SpreadsheetRow) -> list[str]:
    """Non-authoritative hints shown to the manager alongside a match/ambiguity."""
    hints: list[str] = []
    if snapshot.name and row.client_name and snapshot.name.strip().casefold() == str(
        row.client_name
    ).strip().casefold():
        hints.append("совпадает имя клиента")
    if snapshot.region and row.region and snapshot.region.strip().casefold() == str(
        row.region
    ).strip().casefold():
        hints.append("совпадает регион")
    if snapshot.product and row.product:
        hints.append("товар похож (не используется как ключ сопоставления)")
    return hints
