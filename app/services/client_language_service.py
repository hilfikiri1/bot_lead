"""Resolve and persist the language used for client-facing communication."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.client import Client

settings = get_settings()

SUPPORTED_LANGUAGES = {"pl", "uk", "ru", "en", "de"}
LANGUAGE_LABELS = {
    "pl": "PL",
    "uk": "UA",
    "ru": "RU",
    "en": "EN",
    "de": "DE",
}
_LANGUAGE_ALIASES = {
    "pl": "pl",
    "polish": "pl",
    "polski": "pl",
    "polska": "pl",
    "польский": "pl",
    "польська": "pl",
    "uk": "uk",
    "ua": "uk",
    "ukrainian": "uk",
    "українська": "uk",
    "украинский": "uk",
    "ru": "ru",
    "russian": "ru",
    "русский": "ru",
    "російська": "ru",
    "en": "en",
    "english": "en",
    "английский": "en",
    "англійська": "en",
    "de": "de",
    "german": "de",
    "deutsch": "de",
    "немецкий": "de",
    "німецька": "de",
}


@dataclass(frozen=True)
class LanguageResolution:
    language: str
    source: str
    confidence: float
    client_id: int | None = None


def normalize_language(value: str | None) -> str | None:
    normalized = " ".join(str(value or "").strip().casefold().split())
    if not normalized or normalized == "auto":
        return None
    if normalized in _LANGUAGE_ALIASES:
        return _LANGUAGE_ALIASES[normalized]
    for token, code in _LANGUAGE_ALIASES.items():
        if len(token) >= 4 and token in normalized:
            return code
    return None


def _custom_field_language(fields: list[dict[str, Any]]) -> str | None:
    for field in fields:
        name = f"{field.get('name') or ''} {field.get('code') or ''}".casefold()
        if not any(
            marker in name
            for marker in ("language", "język", "jezyk", "язык", "мова", "sprache")
        ):
            continue
        language = normalize_language(str(field.get("value") or ""))
        if language:
            return language
    return None


def infer_language_from_history(text: str) -> tuple[str, float] | None:
    """Conservative heuristic for prior client messages, not generic prose."""
    sample = f" {str(text or '').casefold()} "
    if len(sample.strip()) < 12:
        return None
    scores = {
        "pl": sum(
            sample.count(token)
            for token in (
                " dzień dobry ",
                " proszę ",
                " dziękuję ",
                " państwa ",
                " pozdrawiam ",
                " chciałbym ",
                " możemy ",
            )
        )
        + 2 * len(re.findall(r"[ąćęłńóśźż]", sample)),
        "uk": sum(
            sample.count(token)
            for token in (
                " добрий день ",
                " дякую ",
                " будь ласка ",
                " можете ",
                " надішліть ",
                " замовлення ",
            )
        )
        + 2 * len(re.findall(r"[іїєґ]", sample)),
        "ru": sum(
            sample.count(token)
            for token in (
                " здравствуйте ",
                " добрый день ",
                " спасибо ",
                " пожалуйста ",
                " можете ",
                " пришлите ",
            )
        )
        + len(re.findall(r"[ыэъё]", sample)),
        "de": sum(
            sample.count(token)
            for token in (
                " guten tag ",
                " vielen dank ",
                " bitte ",
                " freundlichen grüßen ",
                " angebot ",
            )
        ),
        "en": sum(
            sample.count(token)
            for token in (
                " hello ",
                " good morning ",
                " thank you ",
                " please ",
                " best regards ",
                " quotation ",
            )
        ),
    }
    language, score = max(scores.items(), key=lambda item: item[1])
    ordered = sorted(scores.values(), reverse=True)
    runner_up = ordered[1] if len(ordered) > 1 else 0
    if score < 2 or score <= runner_up:
        return None
    return language, min(0.95, 0.66 + score * 0.035)


def infer_direction_language(lead: dict[str, Any]) -> tuple[str, float] | None:
    contacts = lead.get("contacts") or []
    phone = ""
    if contacts:
        phone = str(((contacts[0].get("phones") or [""])[0]) or "")
    digits = re.sub(r"\D", "", phone)
    if digits.startswith("48"):
        return "pl", 0.93
    if digits.startswith("380"):
        return "uk", 0.93

    field_values = " ".join(
        str(field.get("value") or "") for field in (lead.get("custom_fields") or [])
    )
    haystack = " ".join(
        (
            str(lead.get("name") or ""),
            str(lead.get("pipeline_name") or ""),
            field_values,
        )
    ).casefold()
    if re.search(r"\b(polsk|poland|polska|warszaw|krak[oó]w|pozna[nń])", haystack):
        return "pl", 0.82
    if re.search(r"\b(ukrain|україн|украин|київ|киев|львів|одес)", haystack):
        return "uk", 0.82
    return None


def correspondence_history(lead: dict[str, Any]) -> str:
    """Exclude ordinary Russian internal notes from client-language inference."""
    history_parts: list[str] = []
    for note in (lead.get("notes") or [])[:8]:
        note_text = str(note.get("text") or "")
        lowered = note_text.casefold()
        looks_like_correspondence = any(
            marker in lowered
            for marker in (
                "whatsapp",
                "e-mail",
                "email",
                "сообщение клиент",
                "wiadomość",
                "message to client",
            )
        ) or bool(re.search(r"[ąćęłńóśźżіїєґ]", lowered))
        if looks_like_correspondence:
            history_parts.append(note_text)
    return "\n".join(history_parts)


def _contact_snapshot(lead: dict[str, Any]) -> dict[str, Any]:
    contacts = lead.get("contacts") or []
    return contacts[0] if contacts else {}


async def _get_or_create_client_profile(
    db: AsyncSession, lead: dict[str, Any]
) -> Client | None:
    contact = _contact_snapshot(lead)
    contact_id = contact.get("id")
    phone = ((contact.get("phones") or [None])[0]) if contact else None
    email = ((contact.get("emails") or [None])[0]) if contact else None
    if not contact_id and not phone and not email:
        return None

    predicates = []
    if isinstance(contact_id, int):
        predicates.append(Client.kommo_contact_id == contact_id)
    if phone:
        predicates.append(Client.phone == str(phone))
    if email:
        predicates.append(Client.email == str(email))
    client = (
        await db.execute(select(Client).where(or_(*predicates)).limit(1))
    ).scalar_one_or_none()
    if client:
        if isinstance(contact_id, int) and client.kommo_contact_id is None:
            client.kommo_contact_id = contact_id
        client.name = contact.get("name") or client.name
        client.phone = phone or client.phone
        client.email = email or client.email
        return client

    client = Client(
        kommo_contact_id=contact_id if isinstance(contact_id, int) else None,
        name=contact.get("name"),
        phone=phone,
        email=email,
        company=None,
        source="kommo",
    )
    db.add(client)
    await db.flush()
    return client


async def resolve_communication_language(
    db: AsyncSession,
    *,
    lead: dict[str, Any],
    explicit_language: str | None,
) -> LanguageResolution:
    explicit = normalize_language(explicit_language)
    if explicit:
        return LanguageResolution(explicit, "explicit_request", 1.0)

    client = await _get_or_create_client_profile(db, lead)
    durable_sources = {
        "manager_selected",
        "client_card",
        "kommo_client_card",
        "legacy_card",
    }
    if (
        client
        and normalize_language(client.communication_language)
        and client.communication_language_source in durable_sources
    ):
        await db.commit()
        return LanguageResolution(
            normalize_language(client.communication_language) or "pl",
            client.communication_language_source or "client_card",
            float(client.communication_language_confidence or 0.95),
            client.id,
        )
    if client and normalize_language(client.language):
        language = normalize_language(client.language) or "pl"
        resolution = LanguageResolution(language, "client_card", 0.90, client.id)
    else:
        contact = _contact_snapshot(lead)
        card_language = _custom_field_language(
            list(contact.get("custom_fields") or [])
            + list(lead.get("custom_fields") or [])
        )
        if card_language:
            resolution = LanguageResolution(
                card_language, "kommo_client_card", 0.96, client.id if client else None
            )
        else:
            history = correspondence_history(lead)
            inferred = infer_language_from_history(history)
            if inferred:
                resolution = LanguageResolution(
                    inferred[0], "previous_correspondence", inferred[1],
                    client.id if client else None,
                )
            else:
                direction = infer_direction_language(lead)
                if direction:
                    resolution = LanguageResolution(
                        direction[0], "market_fallback", direction[1],
                        client.id if client else None,
                    )
                else:
                    fallback = normalize_language(
                        settings.agent_default_client_language
                    ) or "pl"
                    resolution = LanguageResolution(
                        fallback, "system_fallback", 0.55, client.id if client else None
                    )

    if client:
        client.communication_language = resolution.language
        client.communication_language_source = resolution.source
        client.communication_language_confidence = resolution.confidence
        client.communication_language_updated_at = datetime.now(timezone.utc)
    await db.commit()
    return resolution


async def set_communication_language(
    db: AsyncSession,
    *,
    lead: dict[str, Any],
    language: str,
    telegram_user_id: int,
) -> LanguageResolution:
    normalized = normalize_language(language)
    if not normalized:
        raise ValueError("Поддерживаемые языки: PL, UA, RU, EN, DE.")
    client = await _get_or_create_client_profile(db, lead)
    if client is None:
        raise ValueError("В сделке нет контакта, для которого можно сохранить язык.")
    client.communication_language = normalized
    client.communication_language_source = "manager_selected"
    client.communication_language_confidence = 1.0
    client.communication_language_set_by_user_id = int(telegram_user_id)
    client.communication_language_updated_at = datetime.now(timezone.utc)
    await db.commit()
    return LanguageResolution(normalized, "manager_selected", 1.0, client.id)
