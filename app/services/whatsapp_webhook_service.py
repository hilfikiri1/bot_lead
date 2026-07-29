"""Meta WhatsApp Cloud API webhook processing and Telegram notifications."""
from __future__ import annotations

import hashlib
import hmac
import html
import logging
import re
from datetime import datetime, timezone
from typing import Any

from app.config import get_settings
from app.services import kommo_service, telegram_service
from app.services.lead_matching_service import normalize_phone

logger = logging.getLogger(__name__)
settings = get_settings()


def verify_meta_signature(raw_body: bytes, signature_header: str | None) -> bool:
    """Validate ``X-Hub-Signature-256`` when an app secret is configured."""
    secret = str(getattr(settings, "whatsapp_app_secret", "") or "").strip()
    if not secret:
        return True
    supplied = str(signature_header or "").strip()
    if not supplied.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(supplied[7:], expected)


def extract_incoming_messages(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten Meta webhook entries into simple incoming message records."""
    result: list[dict[str, Any]] = []
    for entry in payload.get("entry") or []:
        for change in entry.get("changes") or []:
            value = change.get("value") or {}
            contacts = value.get("contacts") or []
            contact_by_wa_id = {
                str(item.get("wa_id") or ""): item for item in contacts if item.get("wa_id")
            }
            metadata = value.get("metadata") or {}
            for message in value.get("messages") or []:
                if not isinstance(message, dict):
                    continue
                sender = str(message.get("from") or "").strip()
                message_type = str(message.get("type") or "unknown")
                text = ""
                if message_type == "text":
                    text = str((message.get("text") or {}).get("body") or "").strip()
                elif message_type == "button":
                    text = str((message.get("button") or {}).get("text") or "").strip()
                elif message_type == "interactive":
                    interactive = message.get("interactive") or {}
                    reply = interactive.get("button_reply") or interactive.get("list_reply") or {}
                    text = str(reply.get("title") or reply.get("id") or "").strip()
                else:
                    media = message.get(message_type) or {}
                    text = str(media.get("caption") or f"[{message_type}]").strip()
                contact = contact_by_wa_id.get(sender) or {}
                profile = contact.get("profile") or {}
                timestamp = message.get("timestamp")
                result.append(
                    {
                        "message_id": message.get("id"),
                        "phone": sender,
                        "name": profile.get("name"),
                        "message_type": message_type,
                        "text": text,
                        "timestamp": int(timestamp) if str(timestamp or "").isdigit() else None,
                        "phone_number_id": metadata.get("phone_number_id"),
                        "display_phone_number": metadata.get("display_phone_number"),
                    }
                )
    return result


async def _find_lead_by_phone(phone: str) -> dict[str, Any] | None:
    target = normalize_phone(phone)
    if not target:
        return None
    try:
        result = await kommo_service.get_all_leads_for_status_sync()
        leads = await kommo_service.enrich_leads_with_contacts(
            list(result.get("leads") or [])
        )
    except Exception as exc:
        logger.warning("Could not search Kommo for WhatsApp sender %s: %s", phone, exc)
        return None
    matches: list[dict[str, Any]] = []
    for lead in leads:
        phones = [normalize_phone(str(value)) for value in lead.get("phones") or []]
        if target in {value for value in phones if value}:
            matches.append(lead)
    return matches[0] if len(matches) == 1 else None


def _message_time(item: dict[str, Any]) -> str:
    timestamp = item.get("timestamp")
    if not isinstance(timestamp, int):
        return "сейчас"
    try:
        return datetime.fromtimestamp(timestamp, tz=timezone.utc).strftime("%d.%m.%Y %H:%M UTC")
    except Exception:
        return "сейчас"


async def _log_to_kommo(lead: dict[str, Any], item: dict[str, Any]) -> None:
    lead_id = int(lead.get("id") or 0)
    if not lead_id:
        return
    message_id = str(item.get("message_id") or "")
    marker = f"[BBS-WA-IN-{message_id}]" if message_id else "[BBS-WA-IN]"
    try:
        recent = await kommo_service.get_recent_common_notes(lead_id, limit=50)
        if message_id and any(marker in str(note.get("text") or "") for note in recent):
            return
        note = (
            f"{marker}\n"
            "ВХОДЯЩЕЕ WHATSAPP-СООБЩЕНИЕ\n\n"
            f"От: {item.get('name') or item.get('phone') or 'Клиент'}\n"
            f"Телефон: {item.get('phone') or '—'}\n"
            f"Время: {_message_time(item)}\n"
            f"Тип: {item.get('message_type') or 'text'}\n\n"
            f"{item.get('text') or '[без текста]'}"
        )
        await kommo_service.add_common_note(lead_id, note[:13_500])
    except Exception as exc:
        logger.warning("Could not log WhatsApp message to Kommo lead %s: %s", lead_id, exc)


def _notification_markup(item: dict[str, Any], lead: dict[str, Any] | None) -> dict[str, Any]:
    digits = re.sub(r"\D", "", str(item.get("phone") or ""))
    rows: list[list[dict[str, str]]] = []
    if digits:
        rows.append([{"text": "💬 Открыть WhatsApp", "url": f"https://wa.me/{digits}"}])
    if lead and lead.get("url"):
        rows.append([{"text": "🔗 Открыть Kommo", "url": str(lead.get("url"))}])
    return {"inline_keyboard": rows}


async def notify_incoming_message(item: dict[str, Any]) -> None:
    lead = await _find_lead_by_phone(str(item.get("phone") or ""))
    if lead:
        await _log_to_kommo(lead, item)
    lead_name = str((lead or {}).get("name") or "Сделка не найдена")
    lead_id = (lead or {}).get("id")
    text = (
        "💬 <b>НОВОЕ СООБЩЕНИЕ WHATSAPP</b>\n\n"
        f"Клиент: <b>{html.escape(str(item.get('name') or '—'))}</b>\n"
        f"Телефон: <code>{html.escape(str(item.get('phone') or '—'))}</code>\n"
        f"Сделка: {html.escape(lead_name)}\n"
        + (f"Kommo ID: <code>{lead_id}</code>\n" if lead_id else "")
        + f"Время: {_message_time(item)}\n\n"
        + f"<b>Сообщение</b>\n{html.escape(str(item.get('text') or '[без текста]')[:2500])}\n\n"
        + "Анализ: клиент написал последним — теперь он ждёт наш ответ."
    )
    for chat_id in settings.get_allowed_user_ids():
        try:
            await telegram_service.send_message(
                chat_id,
                text,
                reply_markup=_notification_markup(item, lead),
            )
        except Exception as exc:
            logger.warning("Could not notify Telegram user %s: %s", chat_id, exc)


async def process_webhook(payload: dict[str, Any]) -> int:
    messages = extract_incoming_messages(payload)
    for item in messages:
        await notify_incoming_message(item)
    return len(messages)
