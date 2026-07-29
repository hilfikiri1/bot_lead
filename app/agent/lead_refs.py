"""Resolve B&BS internal lead numbers vs Kommo entity IDs."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from app.config import get_settings
from app.services import kommo_service

settings = get_settings()

INTERNAL_NUMBER_MAX = 9999
KOMMO_ID_MIN_DIGITS = 4

# Lead title prefix: `120 - лампадки`, `120 — лампадки`, `№120 лампадки`, `120 лампадки`
_NAME_PREFIX_RE = re.compile(
    r"^(?:№\s*)?(\d{1,4})\s*(?:[-—–]\s*|\s+)(.+)$",
    flags=re.UNICODE,
)
_NAME_ONLY_NUMBER_RE = re.compile(r"^(?:№\s*)?(\d{1,4})$")

_EXPLICIT_KOMMO_PATTERNS = (
    re.compile(r"#\s*(\d{4,12})\b", re.I),
    re.compile(r"\bkommo\s*(?:id)?\s*[:#]?\s*(\d{4,12})\b", re.I),
    re.compile(r"\bкоммо\s*(?:id|ид)?\s*[:#]?\s*(\d{4,12})\b", re.I),
    re.compile(r"\bid\s*[:#]?\s*(\d{4,12})\b", re.I),
)

_ORDINAL_WORDS = {
    "первый": 1,
    "первую": 1,
    "первого": 1,
    "второй": 2,
    "вторую": 2,
    "второго": 2,
    "третий": 3,
    "третью": 3,
    "третьего": 3,
    "четвертый": 4,
    "четвёртый": 4,
    "четвертую": 4,
    "четвёртую": 4,
    "пятый": 5,
    "пятую": 5,
    "шестой": 6,
    "шестую": 6,
    "седьмой": 7,
    "седьмую": 7,
    "восьмой": 8,
    "восьмую": 8,
    "восьмого": 8,
    "восьмому": 8,
    "девятый": 9,
    "девятую": 9,
    "десятый": 10,
    "десятую": 10,
}

_ORDINAL_SUFFIX_RE = re.compile(
    r"\b(\d{1,2})(?:-?й|-?ю|-?го|-?ую)\b",
    re.I,
)

_DATE_FRAGMENT_RE = re.compile(
    r"\b(?:\d{1,2}[./]\d{1,2}(?:[./]\d{2,4})?|\d{1,2}\s+(?:январ|феврал|март|апрел|мая|июн|июл|август|сентябр|октябр|ноябр|декабр)|"
    r"завтра|сегодня|послезавтра|вчера)\b",
    re.I,
)
_TIME_FRAGMENT_RE = re.compile(
    r"\b(?:\d{1,2}[:.]\d{2}|\d{1,2}\s*(?:час|ч\.?|утра|вечера|дня|ночи))\b",
    re.I,
)
_MONTH_WORDS = (
    "январ", "феврал", "март", "апрел", "мая", "июн", "июл",
    "август", "сентябр", "октябр", "ноябр", "декабр",
)


def _is_date_fragment(normalized: str, start: int, end: int) -> bool:
    tail = normalized[end:].strip()
    head = normalized[max(0, start - 12):start]
    if any(month in tail for month in _MONTH_WORDS):
        return True
    if any(month in head for month in _MONTH_WORDS):
        return True
    value = normalized[start:end]
    if re.search(rf"\b{re.escape(value)}\s+(?:{'|'.join(_MONTH_WORDS)})", normalized):
        return True
    if re.search(r"\d{1,2}[:.]\d{2}", normalized[max(0, start - 3): end + 6]):
        return True
    return False

_USER_ERROR_HINT = (
    "Укажи внутренний номер клиента, название или выбери сделку кнопкой."
)


class LeadRefType(str, Enum):
    KOMMO_ID = "kommo_id"
    INTERNAL_NUMBER = "internal_number"
    DIGEST_POSITION = "digest_position"
    NAME_QUERY = "name_query"
    ACTIVE = "active"
    RAW = "raw"


@dataclass
class LeadReference:
    raw: str
    ref_type: LeadRefType
    internal_lead_number: str | None = None
    kommo_lead_id: int | None = None
    digest_position: int | None = None
    name_query: str | None = None
    resolved_kommo_lead_id: int | None = None
    confidence: float = 1.0


@dataclass
class LeadResolutionResult:
    resolved: list[dict[str, Any]] = field(default_factory=list)
    unresolved: list[LeadReference] = field(default_factory=list)
    candidates: list[dict[str, Any]] = field(default_factory=list)
    error_message: str | None = None


def normalize_text(text: str) -> str:
    return " ".join(text.strip().casefold().replace("ё", "е").split())


def extract_explicit_kommo_id(text: str) -> int | None:
    for pattern in _EXPLICIT_KOMMO_PATTERNS:
        match = pattern.search(text)
        if match:
            return int(match.group(1))
    return None


def extract_internal_lead_number(lead: dict[str, Any]) -> str | None:
    """Extract B&BS internal number from Kommo lead payload."""
    field_id = settings.kommo_internal_lead_number_field_id
    if field_id is not None:
        custom = lead.get("custom_fields") or {}
        if isinstance(custom, dict):
            for key, value in custom.items():
                if str(key) == str(field_id) and value:
                    digits = re.sub(r"\D", "", str(value))
                    if digits and int(digits) <= INTERNAL_NUMBER_MAX:
                        return digits
        for field in lead.get("custom_fields_values") or []:
            if int(field.get("field_id") or 0) == int(field_id):
                values = field.get("values") or []
                if values:
                    digits = re.sub(r"\D", "", str(values[0].get("value") or ""))
                    if digits and int(digits) <= INTERNAL_NUMBER_MAX:
                        return digits

    name = str(lead.get("name") or "").strip()
    if not name:
        return None
    prefix = _NAME_PREFIX_RE.match(name)
    if prefix:
        return prefix.group(1)
    only_num = _NAME_ONLY_NUMBER_RE.match(name)
    if only_num:
        return only_num.group(1)
    return None


def enrich_lead(lead: dict[str, Any]) -> dict[str, Any]:
    enriched = dict(lead)
    enriched["internal_lead_number"] = extract_internal_lead_number(lead)
    enriched["kommo_lead_id"] = int(lead.get("id") or 0) or None
    return enriched


def _internal_number_matches_lead(number: str, lead: dict[str, Any]) -> bool:
    internal = extract_internal_lead_number(lead)
    if internal and internal == number:
        return True
    name = str(lead.get("name") or "").strip()
    if not name:
        return False
    patterns = (
        re.compile(rf"^(?:№\s*)?{re.escape(number)}\s*[-—–]", re.I),
        re.compile(rf"^(?:№\s*)?{re.escape(number)}\s+", re.I),
        re.compile(rf"^{re.escape(number)}$", re.I),
    )
    for pattern in patterns:
        if pattern.match(name):
            return True
    return False


async def find_leads_by_internal_number(
    number: str,
    *,
    limit: int = 8,
) -> list[dict[str, Any]]:
    number = re.sub(r"\D", "", str(number or ""))
    if not number or int(number) > INTERNAL_NUMBER_MAX:
        return []
    if int(number) >= KOMMO_ID_MIN_DIGITS and len(number) >= KOMMO_ID_MIN_DIGITS:
        # Ambiguous long number — do not treat as internal without prefix context.
        return []

    open_result = await kommo_service.get_all_open_leads()
    matches: list[tuple[int, dict[str, Any]]] = []
    for lead in open_result.get("leads") or []:
        if _internal_number_matches_lead(number, lead):
            score = 0 if extract_internal_lead_number(lead) == number else 1
            matches.append((score, enrich_lead(lead)))
    matches.sort(key=lambda item: (item[0], -(int(item[1].get("updated_at") or 0))))
    return [item[1] for item in matches[:limit]]


def _digest_item_by_position(context: dict[str, Any], position: int) -> dict[str, Any] | None:
    last_digest = context.get("last_digest") or {}
    items = last_digest.get("items") or []
    for item in items:
        if int(item.get("position") or 0) == int(position):
            return item
    return None


def _digest_item_by_internal_number(
    context: dict[str, Any], number: str
) -> dict[str, Any] | None:
    last_digest = context.get("last_digest") or {}
    for item in last_digest.get("items") or []:
        if str(item.get("internal_lead_number") or "") == str(number):
            return item
    return None


def _extract_digest_positions(text: str, normalized: str) -> list[int]:
    positions: list[int] = []
    if "из дайджеста" in normalized or "в дайджесте" in normalized:
        for match in re.finditer(r"\b(\d{1,2})\b", normalized):
            positions.append(int(match.group(1)))
    for word, pos in _ORDINAL_WORDS.items():
        if re.search(rf"\b{re.escape(word)}\b", normalized):
            positions.append(pos)
    for match in _ORDINAL_SUFFIX_RE.finditer(normalized):
        positions.append(int(match.group(1)))
    if re.search(r"\bпо\s+(?:номеру\s+)?(\d{1,2})(?:\s+из\s+дайджеста)?\b", normalized):
        match = re.search(
            r"\bпо\s+(?:номеру\s+)?(\d{1,2})(?:\s+из\s+дайджеста)?\b",
            normalized,
        )
        if match and "из дайджеста" in normalized:
            positions.append(int(match.group(1)))
    # «по восьмому», «по восьмой»
    for word, pos in _ORDINAL_WORDS.items():
        if re.search(rf"\bпо\s+{re.escape(word)}\b", normalized):
            positions.append(pos)
    return list(dict.fromkeys(positions))


def _extract_internal_numbers(text: str, normalized: str) -> list[str]:
    numbers: list[str] = []
    for match in re.finditer(
        r"(?:клиент|лид|сделк[аеуы]?|№|#)\s*(\d{1,4})\b",
        normalized,
        flags=re.I,
    ):
        numbers.append(match.group(1))
    for match in re.finditer(r"(?:^|\s)№\s*(\d{1,4})\b", text, flags=re.I):
        numbers.append(match.group(1))
    # «по 120», but not «по 8 из дайджеста»
    for match in re.finditer(r"\bпо\s+(\d{1,4})\b", normalized):
        value = match.group(1)
        tail = normalized[match.end():].strip()
        if tail.startswith("из дайджеста"):
            continue
        numbers.append(value)
    # comma/slash separated lists: 107, 83 и 117 / 107/83/117
    stripped = _DATE_FRAGMENT_RE.sub(" ", normalized)
    stripped = _TIME_FRAGMENT_RE.sub(" ", stripped)
    for match in re.finditer(r"(?<![\d/])\b(\d{1,4})\b(?![\d/])", stripped):
        value = match.group(1)
        if int(value) <= INTERNAL_NUMBER_MAX:
            if _is_date_fragment(normalized, match.start(), match.end()):
                continue
            # Skip time fragments like «в 10:00»
            if re.search(
                rf"(?:^|\s)в\s+{re.escape(value)}(?:[:.]\d{{2}})?\b",
                normalized,
            ):
                continue
            numbers.append(value)
    return list(dict.fromkeys(numbers))


def _extract_name_queries(text: str, normalized: str) -> list[str]:
    queries: list[str] = []
    for match in re.finditer(
        r"(?:по|для)\s+([а-яa-z0-9][а-яa-z0-9\s\-]{2,40})",
        normalized,
        flags=re.I,
    ):
        fragment = match.group(1).strip()
        if fragment in _ORDINAL_WORDS or fragment.isdigit():
            continue
        if any(word in fragment for word in ("завтра", "сегодня", "клиент", "лид")):
            continue
        queries.append(fragment)
    return queries


def parse_lead_references(text: str, context: dict[str, Any]) -> list[LeadReference]:
    normalized = normalize_text(text)
    refs: list[LeadReference] = []

    explicit = extract_explicit_kommo_id(text)
    if explicit:
        refs.append(
            LeadReference(
                raw=str(explicit),
                ref_type=LeadRefType.KOMMO_ID,
                kommo_lead_id=explicit,
            )
        )

    if any(
        token in normalized
        for token in ("эта сделка", "этот лид", "по нему", "по ней", "вот этот", "вот эта")
    ):
        active = context.get("active_kommo_lead_id") or context.get("kommo_lead_id")
        if active:
            refs.append(
                LeadReference(
                    raw="active",
                    ref_type=LeadRefType.ACTIVE,
                    kommo_lead_id=int(active),
                    resolved_kommo_lead_id=int(active),
                )
            )

    for position in _extract_digest_positions(text, normalized):
        refs.append(
            LeadReference(
                raw=str(position),
                ref_type=LeadRefType.DIGEST_POSITION,
                digest_position=position,
            )
        )

    for number in _extract_internal_numbers(text, normalized):
        refs.append(
            LeadReference(
                raw=number,
                ref_type=LeadRefType.INTERNAL_NUMBER,
                internal_lead_number=number,
            )
        )

    for query in _extract_name_queries(text, normalized):
        refs.append(
            LeadReference(
                raw=query,
                ref_type=LeadRefType.NAME_QUERY,
                name_query=query,
            )
        )

    # Deduplicate by semantic key
    seen: set[str] = set()
    unique: list[LeadReference] = []
    for ref in refs:
        key = (
            f"{ref.ref_type}:{ref.kommo_lead_id}:{ref.internal_lead_number}:"
            f"{ref.digest_position}:{ref.name_query}"
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(ref)
    return unique


async def resolve_reference(
    ref: LeadReference,
    context: dict[str, Any],
) -> LeadResolutionResult:
    if ref.resolved_kommo_lead_id:
        lead = enrich_lead(await kommo_service.get_lead_details(ref.resolved_kommo_lead_id))
        return LeadResolutionResult(resolved=[lead])

    if ref.ref_type == LeadRefType.KOMMO_ID and ref.kommo_lead_id:
        try:
            lead = enrich_lead(await kommo_service.get_lead_details(ref.kommo_lead_id))
            return LeadResolutionResult(resolved=[lead])
        except kommo_service.KommoAPIError:
            return LeadResolutionResult(
                unresolved=[ref],
                error_message=f"Сделка Kommo ID {ref.kommo_lead_id} не найдена.",
            )

    if ref.ref_type == LeadRefType.DIGEST_POSITION and ref.digest_position:
        item = _digest_item_by_position(context, ref.digest_position)
        if item and item.get("kommo_lead_id"):
            lead = enrich_lead(
                await kommo_service.get_lead_details(int(item["kommo_lead_id"]))
            )
            return LeadResolutionResult(resolved=[lead])
        return LeadResolutionResult(
            unresolved=[ref],
            error_message=(
                f"В последнем дайджесте нет позиции {ref.digest_position}. "
                "Запроси /digest и выбери сделку кнопкой."
            ),
        )

    if ref.ref_type == LeadRefType.INTERNAL_NUMBER and ref.internal_lead_number:
        digest_item = _digest_item_by_internal_number(context, ref.internal_lead_number)
        if digest_item and digest_item.get("kommo_lead_id"):
            lead = enrich_lead(
                await kommo_service.get_lead_details(int(digest_item["kommo_lead_id"]))
            )
            return LeadResolutionResult(resolved=[lead])
        matches = await find_leads_by_internal_number(ref.internal_lead_number)
        if len(matches) == 1:
            return LeadResolutionResult(resolved=[matches[0]])
        if len(matches) > 1:
            return LeadResolutionResult(unresolved=[ref], candidates=matches)
        # fallback title search
        search = await kommo_service.search_open_leads(ref.internal_lead_number, limit=8)
        leads = [enrich_lead(item) for item in search.get("leads") or []]
        if len(leads) == 1:
            lead = enrich_lead(
                await kommo_service.get_lead_details(int(leads[0]["id"]))
            )
            return LeadResolutionResult(resolved=[lead])
        if len(leads) > 1:
            return LeadResolutionResult(unresolved=[ref], candidates=leads)
        return LeadResolutionResult(
            unresolved=[ref],
            error_message=f"Не нашёл клиента №{ref.internal_lead_number}.",
        )

    if ref.ref_type == LeadRefType.NAME_QUERY and ref.name_query:
        search = await kommo_service.search_open_leads(ref.name_query, limit=8)
        leads = [enrich_lead(item) for item in search.get("leads") or []]
        if len(leads) == 1:
            lead = enrich_lead(
                await kommo_service.get_lead_details(int(leads[0]["id"]))
            )
            return LeadResolutionResult(resolved=[lead])
        if len(leads) > 1:
            return LeadResolutionResult(unresolved=[ref], candidates=leads)
        return LeadResolutionResult(
            unresolved=[ref],
            error_message=f"Не нашёл сделку по запросу «{ref.name_query}».",
        )

    return LeadResolutionResult(unresolved=[ref], error_message=_USER_ERROR_HINT)


async def resolve_references(
    refs: list[LeadReference],
    context: dict[str, Any],
) -> LeadResolutionResult:
    resolved: list[dict[str, Any]] = []
    unresolved: list[LeadReference] = []
    candidates: list[dict[str, Any]] = []
    errors: list[str] = []
    seen_ids: set[int] = set()

    for ref in refs:
        result = await resolve_reference(ref, context)
        for lead in result.resolved:
            lead_id = int(lead.get("id") or lead.get("kommo_lead_id") or 0)
            if lead_id and lead_id not in seen_ids:
                seen_ids.add(lead_id)
                resolved.append(lead)
        unresolved.extend(result.unresolved)
        if result.candidates:
            candidates.extend(result.candidates)
        if result.error_message:
            errors.append(result.error_message)

    return LeadResolutionResult(
        resolved=resolved,
        unresolved=unresolved,
        candidates=candidates,
        error_message=errors[0] if errors and not resolved and not unresolved else None,
    )


async def resolve_lead_for_plan(
    *,
    lead_id: int | None,
    query: str | None,
    lead_refs: list[LeadReference] | None,
    context: dict[str, Any],
) -> LeadResolutionResult:
    refs = list(lead_refs or [])
    if lead_id:
        refs.insert(
            0,
            LeadReference(
                raw=str(lead_id),
                ref_type=LeadRefType.KOMMO_ID,
                kommo_lead_id=int(lead_id),
            ),
        )
    if query and not refs:
        # Small number → internal, not Kommo ID
        clean = query.strip()
        if clean.isdigit() and int(clean) <= INTERNAL_NUMBER_MAX:
            refs.append(
                LeadReference(
                    raw=clean,
                    ref_type=LeadRefType.INTERNAL_NUMBER,
                    internal_lead_number=clean,
                )
            )
        else:
            refs.append(
                LeadReference(
                    raw=clean,
                    ref_type=LeadRefType.NAME_QUERY,
                    name_query=clean,
                )
            )
    if not refs:
        active = context.get("active_kommo_lead_id") or context.get("kommo_lead_id")
        if active:
            refs.append(
                LeadReference(
                    raw="active",
                    ref_type=LeadRefType.ACTIVE,
                    kommo_lead_id=int(active),
                    resolved_kommo_lead_id=int(active),
                )
            )
    if not refs:
        return LeadResolutionResult(error_message=_USER_ERROR_HINT)
    return await resolve_references(refs, context)


def user_error_hint() -> str:
    return _USER_ERROR_HINT
