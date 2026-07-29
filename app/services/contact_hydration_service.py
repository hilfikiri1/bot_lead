"""Extract contact fields from Kommo chat/form text and propose CRM hydration."""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from typing import Any

from app.services import contact_resolver


PHONE_LABELS = (
    "proszę podać swój numer",
    "prosze podac swoj numer",
    "numer kontaktowy",
    "numer telefonu",
    "phone number",
    "phone",
    "телефон",
    "номер телефона",
    "номер телефону",
    "мобильн",
)
EMAIL_LABELS = ("email", "e-mail", "почта", "mail")
NAME_LABELS = (
    "full name",
    "imie i nazwisko",
    "imię i nazwisko",
    "имя",
    "name",
    "як вас звати",
)
CHANNEL_LABELS = (
    "w jaki sposób najlepiej",
    "w jaki sposob najlepiej",
    "как удобнее",
    "preferred contact",
    "канал",
)
BUDGET_LABELS = (
    "jaką wartość zamówienia",
    "jaka wartosc zamowienia",
    "бюджет",
    "budget",
    "який бюджет",
)
PRODUCT_LABELS = (
    "jakiego produktu",
    "продукт",
    "product",
    "який товар",
    "что нужно",
)
REGION_LABELS = (
    "w jakim regionie",
    "region",
    "регион",
    "місто",
    "город",
    "з якого ви міста",
)


@dataclass
class ExtractedContactFields:
    name: str | None = None
    phone: str | None = None
    email: str | None = None
    preferred_channel: str | None = None
    budget_text: str | None = None
    product: str | None = None
    region: str | None = None
    sources: dict[str, str] = field(default_factory=dict)


@dataclass
class FieldUpdate:
    key: str
    label: str
    value: str
    target: str  # contact | lead_custom
    field_id: int | None = None
    field_code: str | None = None
    field_name: str | None = None


@dataclass
class HydrationProposal:
    lead_id: int
    lead_name: str | None
    contact_id: int | None
    contact_name: str | None
    extracted: ExtractedContactFields
    updates: list[FieldUpdate] = field(default_factory=list)
    already_filled: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    corpus_chars: int = 0

    @property
    def has_updates(self) -> bool:
        return bool(self.updates)


def _normalize_label(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "").casefold().strip(" :.—-"))


def _clean_value(value: str | None) -> str | None:
    text = re.sub(r"\s+", " ", str(value or "").strip())
    text = text.strip(" .;,—-")
    if not text or text in {"...", "—", "-", "n/a", "нет"}:
        return None
    return text[:500]


def _match_label(line: str, labels: tuple[str, ...]) -> bool:
    marker = _normalize_label(line)
    return any(label in marker for label in labels)


def _pair_values(text: str) -> list[tuple[str, str]]:
    """Parse 'Label:\\nValue' and 'Label: Value' form submissions."""
    lines = [line.strip() for line in str(text or "").splitlines()]
    pairs: list[tuple[str, str]] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if not line:
            index += 1
            continue
        if ":" in line:
            label, rest = line.split(":", 1)
            value = rest.strip()
            if value:
                pairs.append((label.strip(), value))
                index += 1
                continue
            # Value on the next non-empty line.
            look = index + 1
            while look < len(lines) and not lines[look].strip():
                look += 1
            if look < len(lines):
                pairs.append((label.strip(), lines[look].strip()))
                index = look + 1
                continue
        index += 1
    return pairs


def _extract_emails(text: str) -> list[str]:
    return [
        match.group(0)
        for match in re.finditer(
            r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}",
            text,
            flags=re.I,
        )
    ]


def _extract_phones(text: str, *, default_country: str | None = None) -> list[str]:
    found: list[str] = []
    for match in re.finditer(
        r"(?<!\d)(?:\+|00)?[\d][\d\s().-]{7,18}\d(?!\d)",
        text,
    ):
        raw = match.group(0)
        normalized = contact_resolver.normalize_phone(
            raw, default_country=default_country
        )
        if normalized:
            found.append(raw.strip())
    # Prefer longer / more complete numbers first, keep order unique.
    unique: list[str] = []
    seen: set[str] = set()
    for item in found:
        key = contact_resolver.normalize_phone(item, default_country=default_country) or item
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique


def extract_contact_fields_from_text(
    text: str,
    *,
    default_country: str | None = None,
) -> ExtractedContactFields:
    raw = str(text or "").strip()
    result = ExtractedContactFields()
    if not raw:
        return result

    country = default_country or contact_resolver.default_country_for_lead(
        {"pipeline_name": raw}
    )

    for label, value in _pair_values(raw):
        clean = _clean_value(value)
        if not clean:
            continue
        if not result.phone and _match_label(label, PHONE_LABELS):
            phone = _extract_phones(clean, default_country=country)
            if phone:
                result.phone = phone[0]
                result.sources["phone"] = label
        elif not result.email and _match_label(label, EMAIL_LABELS):
            emails = _extract_emails(clean)
            if emails:
                result.email = emails[0]
                result.sources["email"] = label
            elif "@" in clean:
                result.email = clean
                result.sources["email"] = label
        elif not result.name and _match_label(label, NAME_LABELS):
            if "@" not in clean and not re.fullmatch(r"[\d\s+\-()]+", clean):
                result.name = clean[:120]
                result.sources["name"] = label
        elif not result.preferred_channel and _match_label(label, CHANNEL_LABELS):
            result.preferred_channel = clean[:80]
            result.sources["preferred_channel"] = label
        elif not result.budget_text and _match_label(label, BUDGET_LABELS):
            result.budget_text = clean[:120]
            result.sources["budget_text"] = label
        elif not result.product and _match_label(label, PRODUCT_LABELS):
            result.product = clean[:300]
            result.sources["product"] = label
        elif not result.region and _match_label(label, REGION_LABELS):
            result.region = clean[:120]
            result.sources["region"] = label

    if not result.email:
        emails = _extract_emails(raw)
        if emails:
            result.email = emails[0]
            result.sources["email"] = "regex"
    if not result.phone:
        phones = _extract_phones(raw, default_country=country or "pl")
        if phones:
            # Prefer numbers near phone labels when possible.
            result.phone = phones[0]
            result.sources["phone"] = "regex"

    return result


def _current_contact_phone(lead: dict[str, Any]) -> str | None:
    resolved = contact_resolver.resolve_contact(lead)
    return resolved.phone_normalized or resolved.phone_display


def _current_contact_email(lead: dict[str, Any]) -> str | None:
    resolved = contact_resolver.resolve_contact(lead)
    if resolved.email:
        return resolved.email
    for contact in lead.get("contacts") or []:
        for email in contact.get("emails") or []:
            if email:
                return str(email)
    return None


def _lead_custom_value_map(lead: dict[str, Any]) -> dict[str, str]:
    values: dict[str, str] = {}
    custom = lead.get("custom_fields") or []
    if isinstance(custom, dict):
        for key, value in custom.items():
            if value:
                values[_normalize_label(str(key))] = str(value)
        return values
    for field in custom:
        name = _normalize_label(str(field.get("name") or field.get("code") or ""))
        value = str(field.get("value") or "").strip()
        if name and value:
            values[name] = value
    return values


def _pick_lead_field(
    catalog: list[dict[str, Any]],
    labels: tuple[str, ...],
) -> dict[str, Any] | None:
    for field in catalog:
        marker = _normalize_label(
            f"{field.get('name') or ''} {field.get('code') or ''}"
        )
        if any(label in marker for label in labels):
            return field
    return None


def build_hydration_proposal(
    lead: dict[str, Any],
    *,
    corpus: str,
    lead_field_catalog: list[dict[str, Any]] | None = None,
) -> HydrationProposal:
    country = contact_resolver.default_country_for_lead(lead) or "pl"
    extracted = extract_contact_fields_from_text(corpus, default_country=country)
    contacts = list(lead.get("contacts") or [])
    contact = contacts[0] if contacts else {}
    proposal = HydrationProposal(
        lead_id=int(lead.get("id") or 0),
        lead_name=str(lead.get("name") or "") or None,
        contact_id=int(contact["id"]) if contact.get("id") else None,
        contact_name=str(contact.get("name") or "").strip() or None,
        extracted=extracted,
        corpus_chars=len(corpus or ""),
    )
    if proposal.corpus_chars < 20:
        proposal.warnings.append("В чате/заметках слишком мало текста для извлечения.")
    if not any(
        [
            extracted.phone,
            extracted.email,
            extracted.name,
            extracted.product,
            extracted.region,
            extracted.budget_text,
        ]
    ):
        proposal.warnings.append("Не нашёл телефон/email/имя в тексте переписки.")

    current_phone = _current_contact_phone(lead)
    current_email = _current_contact_email(lead)
    lead_values = _lead_custom_value_map(lead)
    catalog = list(lead_field_catalog or [])

    if extracted.phone:
        if current_phone:
            proposal.already_filled.append(f"телефон уже есть: {current_phone}")
        else:
            proposal.updates.append(
                FieldUpdate(
                    key="phone",
                    label="Телефон контакта",
                    value=extracted.phone,
                    target="contact",
                    field_code="PHONE",
                )
            )
            phone_field = _pick_lead_field(catalog, PHONE_LABELS)
            if phone_field:
                field_name = str(phone_field.get("name") or "")
                if not lead_values.get(_normalize_label(field_name)):
                    proposal.updates.append(
                        FieldUpdate(
                            key="lead_phone",
                            label=f"Поле сделки: {field_name}",
                            value=extracted.phone,
                            target="lead_custom",
                            field_id=int(phone_field["id"])
                            if phone_field.get("id")
                            else None,
                            field_name=field_name,
                        )
                    )

    if extracted.email:
        if current_email:
            proposal.already_filled.append(f"email уже есть: {current_email}")
        else:
            proposal.updates.append(
                FieldUpdate(
                    key="email",
                    label="Email контакта",
                    value=extracted.email,
                    target="contact",
                    field_code="EMAIL",
                )
            )
            email_field = _pick_lead_field(catalog, EMAIL_LABELS)
            if email_field:
                field_name = str(email_field.get("name") or "")
                if not lead_values.get(_normalize_label(field_name)):
                    proposal.updates.append(
                        FieldUpdate(
                            key="lead_email",
                            label=f"Поле сделки: {field_name}",
                            value=extracted.email,
                            target="lead_custom",
                            field_id=int(email_field["id"])
                            if email_field.get("id")
                            else None,
                            field_name=field_name,
                        )
                    )

    if extracted.name:
        contact_name = (proposal.contact_name or "").strip()
        generic = contact_name.casefold() in {
            "",
            "без имени",
            "контакт",
            "messenger",
            "facebook",
            "instagram",
        }
        if contact_name and not generic and contact_name.casefold() != extracted.name.casefold():
            proposal.already_filled.append(f"имя контакта уже есть: {contact_name}")
        elif generic or not contact_name:
            proposal.updates.append(
                FieldUpdate(
                    key="name",
                    label="Имя контакта",
                    value=extracted.name,
                    target="contact",
                )
            )
        name_field = _pick_lead_field(catalog, NAME_LABELS)
        if name_field:
            field_name = str(name_field.get("name") or "")
            if not lead_values.get(_normalize_label(field_name)):
                proposal.updates.append(
                    FieldUpdate(
                        key="lead_name_field",
                        label=f"Поле сделки: {field_name}",
                        value=extracted.name,
                        target="lead_custom",
                        field_id=int(name_field["id"]) if name_field.get("id") else None,
                        field_name=field_name,
                    )
                )

    for key, labels, value in (
        ("preferred_channel", CHANNEL_LABELS, extracted.preferred_channel),
        ("budget_text", BUDGET_LABELS, extracted.budget_text),
        ("product", PRODUCT_LABELS, extracted.product),
        ("region", REGION_LABELS, extracted.region),
    ):
        if not value:
            continue
        lead_field = _pick_lead_field(catalog, labels)
        if not lead_field:
            continue
        field_name = str(lead_field.get("name") or "")
        if lead_values.get(_normalize_label(field_name)):
            proposal.already_filled.append(f"{field_name} уже заполнено")
            continue
        proposal.updates.append(
            FieldUpdate(
                key=key,
                label=f"Поле сделки: {field_name}",
                value=value,
                target="lead_custom",
                field_id=int(lead_field["id"]) if lead_field.get("id") else None,
                field_name=field_name,
            )
        )

    return proposal


def format_hydration_preview(proposal: HydrationProposal) -> str:
    lines = [
        "<b>Заполнить контакт из чата?</b>",
        "",
        f"Сделка: <b>{html.escape(str(proposal.lead_name or proposal.lead_id))}</b>",
    ]
    if proposal.contact_name:
        lines.append(f"Контакт: {html.escape(proposal.contact_name)}")
    if proposal.updates:
        lines.extend(["", "<b>Будет записано</b>"])
        for item in proposal.updates:
            lines.append(
                f"• {html.escape(item.label)}: <code>{html.escape(item.value)}</code>"
            )
    else:
        lines.extend(["", "Нечего обновлять — поля уже заполнены или данные не найдены."])
    if proposal.already_filled:
        lines.extend(["", "<b>Уже есть</b>"])
        lines.extend(f"• {html.escape(x)}" for x in proposal.already_filled[:8])
    if proposal.warnings:
        lines.extend(["", "<b>Замечания</b>"])
        lines.extend(f"⚠️ {html.escape(x)}" for x in proposal.warnings[:5])
    lines.extend(
        [
            "",
            "<i>Пустые поля контакта/сделки будут заполнены. Существующие значения не перезаписываются.</i>",
        ]
    )
    return "\n".join(lines)


def proposal_to_payload(proposal: HydrationProposal) -> dict[str, Any]:
    return {
        "lead_id": proposal.lead_id,
        "contact_id": proposal.contact_id,
        "updates": [
            {
                "key": item.key,
                "label": item.label,
                "value": item.value,
                "target": item.target,
                "field_id": item.field_id,
                "field_code": item.field_code,
                "field_name": item.field_name,
            }
            for item in proposal.updates
        ],
        "extracted": {
            "name": proposal.extracted.name,
            "phone": proposal.extracted.phone,
            "email": proposal.extracted.email,
            "preferred_channel": proposal.extracted.preferred_channel,
            "budget_text": proposal.extracted.budget_text,
            "product": proposal.extracted.product,
            "region": proposal.extracted.region,
        },
    }
