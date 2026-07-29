"""Match Kommo leads to spreadsheet rows."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any

from app.services.google_sheets_service import SpreadsheetRow


@dataclass(frozen=True)
class MatchCandidate:
    row: SpreadsheetRow
    matched_by: str
    score: int


@dataclass(frozen=True)
class MatchResult:
    candidates: list[MatchCandidate]

    @property
    def single(self) -> MatchCandidate | None:
        if len(self.candidates) != 1:
            return None
        return self.candidates[0]

    @property
    def is_empty(self) -> bool:
        return not self.candidates


def normalize_phone(value: str | None) -> str | None:
    digits = re.sub(r"\D", "", value or "")
    if len(digits) < 9:
        return None
    if digits.startswith("48") and len(digits) >= 11:
        digits = digits[-9:]
    elif len(digits) > 9:
        digits = digits[-9:]
    return digits if len(digits) >= 9 else None


def normalize_email(value: str | None) -> str | None:
    email = (value or "").strip().casefold()
    return email or None


def normalize_name(value: str | None) -> str | None:
    text = (value or "").strip().casefold().replace("ё", "е")
    text = re.sub(r"[^\w\s]", " ", text, flags=re.UNICODE)
    text = " ".join(text.split())
    return text or None


def _fold_text(value: str | None) -> str:
    text = unicodedata.normalize("NFKD", value or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return normalize_name(text) or ""


def _name_similarity(left: str | None, right: str | None) -> float:
    a = _fold_text(left)
    b = _fold_text(right)
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def _hash_match_value(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def match_lead_to_rows(
    *,
    phones: list[str] | None,
    emails: list[str] | None,
    contact_name: str | None,
    company: str | None,
    product_hint: str | None,
    rows: list[SpreadsheetRow],
    require_lead_number: bool = True,
) -> MatchResult:
    phone_set = {
        normalized
        for phone in phones or []
        if (normalized := normalize_phone(phone))
    }
    email_set = {
        normalized
        for email in emails or []
        if (normalized := normalize_email(email))
    }
    contact_norm = normalize_name(contact_name)
    company_norm = normalize_name(company)
    product_norm = _fold_text(product_hint)

    candidates: list[MatchCandidate] = []

    for row in rows:
        if require_lead_number and (
            not row.lead_number or not str(row.lead_number).strip()
        ):
            continue
        if not row.product or not str(row.product).strip():
            continue

        row_phone = normalize_phone(row.phone)
        if row_phone and phone_set and row_phone in phone_set:
            candidates.append(MatchCandidate(row=row, matched_by="phone", score=100))
            continue

        row_email = normalize_email(row.email)
        if row_email and email_set and row_email in email_set:
            candidates.append(MatchCandidate(row=row, matched_by="email", score=90))
            continue

        row_name = normalize_name(row.client_name)
        if contact_norm and row_name and contact_norm == row_name:
            candidates.append(MatchCandidate(row=row, matched_by="name", score=80))
            continue

        if company_norm and row_name and company_norm == row_name:
            candidates.append(MatchCandidate(row=row, matched_by="company", score=78))
            continue

        similarity = max(
            _name_similarity(contact_name, row.client_name),
            _name_similarity(company, row.client_name),
            _name_similarity(company, row.company),
        )
        if similarity >= 0.86:
            candidates.append(
                MatchCandidate(row=row, matched_by="name_fuzzy", score=60)
            )
            continue

        row_product = _fold_text(row.product)
        if product_norm and row_product and (
            product_norm in row_product or row_product in product_norm
        ):
            candidates.append(
                MatchCandidate(row=row, matched_by="product", score=40)
            )

    if not candidates:
        return MatchResult(candidates=[])

    best_score = max(item.score for item in candidates)
    if best_score >= 60:
        top = [item for item in candidates if item.score == best_score]
        return MatchResult(candidates=top)

    strong = [item for item in candidates if item.score >= 80]
    if len(strong) == 1:
        return MatchResult(candidates=strong)

    if len(strong) > 1:
        return MatchResult(candidates=strong)

    return MatchResult(candidates=[])


def match_value_hash(candidate: MatchCandidate) -> str:
    row = candidate.row
    if candidate.matched_by == "phone":
        return _hash_match_value(normalize_phone(row.phone) or "")
    if candidate.matched_by == "email":
        return _hash_match_value(normalize_email(row.email) or "")
    if candidate.matched_by in {"name", "name_fuzzy", "company"}:
        return _hash_match_value(normalize_name(row.client_name) or "")
    return _hash_match_value((row.product or "").casefold())


def lead_contact_snapshot(details: dict[str, Any]) -> dict[str, Any]:
    contacts = details.get("contacts") or []
    primary = contacts[0] if contacts else {}
    phones = primary.get("phones") or []
    emails = primary.get("emails") or []
    return {
        "phones": phones,
        "emails": emails,
        "contact_name": primary.get("name"),
        "company": details.get("company"),
        "product_hint": details.get("name"),
    }
