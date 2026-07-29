"""Bundle onboarding contacts into one iPhone-friendly vCard file.

The onboarding workflow can create several contacts at once. Sending one Telegram
file per contact is noisy, and iOS may prefer the ORG value over FN when the
vCard lacks a structured person name. This runtime keeps the single-contact
flow, but sends one multi-contact .vcf when two or more contacts are available.
"""
from __future__ import annotations

import html
import logging
from typing import Any

from app.services import client_message_service, runtime_extensions, telegram_service

logger = logging.getLogger(__name__)
_INSTALLED = False


def _clean(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()


def _capitalize_first(value: str) -> str:
    value = _clean(value)
    if not value:
        return value
    return value[:1].upper() + value[1:]


def contact_display_name(card: dict[str, Any]) -> str:
    """Return the exact person name shown in iPhone Contacts."""

    lead_number = _clean(card.get("lead_number"))
    client_name = _capitalize_first(_clean(card.get("name")) or "Клиент")
    product = _clean(card.get("product")) or "Новый запрос"
    prefix = f"{lead_number} - " if lead_number else ""
    return f"{prefix}{client_name} — {product}"[:180]


def build_person_vcard(card: dict[str, Any]) -> bytes:
    """Build a vCard that iOS treats as a person, not as an organisation."""

    display_name = contact_display_name(card)
    phone = _clean(card.get("phone"))
    if not phone:
        raise ValueError("Contact has no phone")

    raw = client_message_service.build_vcard(
        name=display_name,
        company=None,
        phone=phone,
        email=_clean(card.get("email")) or None,
        language="pl",
    ).decode("utf-8")

    lines = raw.split("\r\n")
    result: list[str] = []
    structured_name_added = False
    for line in lines:
        result.append(line)
        if line.startswith("FN:") and not structured_name_added:
            escaped = client_message_service._vcard_escape(display_name)
            result.append(f"N:{escaped};;;;")
            result.append("X-ABShowAs:PERSON")
            structured_name_added = True
    return "\r\n".join(result).encode("utf-8")


def _bundle_filename(cards: list[dict[str, Any]]) -> str:
    numeric = sorted(
        int(value)
        for value in (_clean(card.get("lead_number")) for card in cards)
        if value.isdigit()
    )
    if numeric:
        suffix = str(numeric[0]) if len(numeric) == 1 else f"{numeric[0]}-{numeric[-1]}"
    else:
        suffix = str(len(cards))
    return f"BBS_contacts_{suffix}.vcf"


async def send_vcards_bundled(chat_id: int, result: dict[str, Any]) -> None:
    prepared: list[tuple[dict[str, Any], str, bytes]] = []
    for raw_card in result.get("contact_cards") or []:
        card = dict(raw_card or {})
        if not _clean(card.get("phone")):
            continue
        try:
            display_name = contact_display_name(card)
            prepared.append((card, display_name, build_person_vcard(card)))
        except Exception as exc:
            logger.warning("Could not prepare onboarding vCard: %s", exc)

    if not prepared:
        return

    if len(prepared) == 1:
        card, display_name, content = prepared[0]
        lead_number = _clean(card.get("lead_number"))
        await telegram_service.send_document(
            chat_id,
            filename=client_message_service.vcard_filename(display_name),
            content=content,
            caption=(
                f"👤 <b>Контакт для iPhone · №{html.escape(lead_number or '—')}</b>\n"
                f"{html.escape(display_name)}\n"
                "Нажмите на файл и выберите «Создать новый контакт»."
            ),
            mime_type="text/vcard",
        )
        return

    cards = [item[0] for item in prepared]
    content = b"".join(item[2] for item in prepared)
    lines = [
        f"👥 <b>Контакты для iPhone · {len(prepared)}</b>",
        "",
    ]
    for _, display_name, _ in prepared[:12]:
        lines.append(f"• {html.escape(display_name)}")
    if len(prepared) > 12:
        lines.append(f"• …и ещё {len(prepared) - 12}")
    lines.extend(
        [
            "",
            "Откройте один файл — iPhone предложит добавить все контакты.",
        ]
    )
    await telegram_service.send_document(
        chat_id,
        filename=_bundle_filename(cards),
        content=content,
        caption="\n".join(lines),
        mime_type="text/vcard",
    )


def install_contact_bundle_runtime() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True
    runtime_extensions._send_vcards = send_vcards_bundled
    logger.info("Bundled iPhone contact export installed")
