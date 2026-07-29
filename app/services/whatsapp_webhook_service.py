"""Meta WhatsApp Cloud API webhook processing and Telegram notifications."""
from __future__ import annotations

import hashlib
import hmac
import html
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any

from app.config import get_settings
from app.database import AsyncSessionLocal
from app.services import (
    followup_service,
    kommo_service,
    telegram_service,
    whatsapp_cloud_service,
)
from app.services.lead_matching_service import normalize_phone

logger = logging.getLogger(__name__)
settings = get_settings()


def verify_meta_signature(raw_body: bytes, signature_header: str | None) -> bool:
    """Validate ``X-Hub-Signature-256``; fail closed until configured."""
    secret = os.getenv("WHATSAPP_APP_SECRET", "").strip()
    if not secret:
        return False
    supplied = str(signature_header or "").strip()
    if not supplied.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(supplied[7:], expected)


def _message_text(message: dict[str, Any], message_type: str) -> tuple[str, dict[str, Any] | None]:
    if message_type == "text":
        return str((message.get("text") or {}).get("body") or "").strip(), None
    if message_type == "button":
        return str((message.get("button") or {}).get("text") or "").strip(), None
    if message_type == "interactive":
        interactive = message.get("interactive") or {}
        reply = interactive.get("button_reply") or interactive.get("list_reply") or {}
        return str(reply.get("title") or reply.get("id") or "").strip(), None
    media = message.get(message_type) or {}
    media_data = {
        key: media.get(key)
        for key in ("id", "mime_type", "sha256", "filename", "caption")
        if media.get(key) is not None
    }
    caption = str(media.get("caption") or "").strip()
    label = caption or f"[{message_type}]"
    return label, media_data or None


def extract_incoming_messages(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten Meta webhook entries into durable incoming message records."""
    result: list[dict[str, Any]] = []
    for entry in payload.get("entry") or []:
        for change in entry.get("changes") or []:
            value = change.get("value") or {}
            contacts = value.get("contacts") or []
            contact_by_wa_id = {
                str(item.get("wa_id") or ""): item
                for item in contacts
                if item.get("wa_id")
            }
            metadata = value.get("metadata") or {}
            for message in value.get("messages") or []:
                if not isinstance(message, dict):
                    continue
                sender = str(message.get("from") or "").strip()
                message_type = str(message.get("type") or "unknown")
                text, media = _message_text(message, message_type)
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
                        "context_message_id": (message.get("context") or {}).get("id"),
                        "media": media,
                        "raw": message,
                    }
                )
    return result


def extract_status_updates(payload: dict[str, Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for entry in payload.get("entry") or []:
        for change in entry.get("changes") or []:
            value = change.get("value") or {}
            for status in value.get("statuses") or []:
                if not isinstance(status, dict):
                    continue
                result.append(
                    {
                        "message_id": status.get("id"),
                        "status": status.get("status"),
                        "timestamp": status.get("timestamp"),
                        "recipient_id": status.get("recipient_id"),
                        "conversation": status.get("conversation"),
                        "pricing": status.get("pricing"),
                        "errors": status.get("errors") or [],
                        "raw": status,
                    }
                )
    return result


async def _find_lead_by_phone(phone: str) -> dict[str, Any] | None:
    target = normalize_phone(phone)
    if not target:
        return None
    try:
        result = await kommo_service.get_all_leads_for_status_sync()
        leads = await kommo_service.enrich_leads_with_contacts(list(result.get("leads") or []))
    except Exception as exc:
        logger.warning("Could not search Kommo for WhatsApp sender %s: %s", phone, exc)
        return None
    matches: list[dict[str, Any]] = []
    for lead in leads:
        phones = [normalize_phone(str(value)) for value in lead.get("phones") or []]
        if target in {value for value in phones if value}:
            matches.append(lead)
    return matches[0] if len(matches) == 1 else None


def _incoming_at(item: dict[str, Any]) -> datetime:
    timestamp = item.get("timestamp")
    if isinstance(timestamp, int):
        try:
            return datetime.fromtimestamp(timestamp, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            pass
    return datetime.now(timezone.utc)


def _message_time(item: dict[str, Any]) -> str:
    return _incoming_at(item).strftime("%d.%m.%Y %H:%M UTC")


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
        media = item.get("media") or {}
        media_line = ""
        if media:
            media_line = (
                f"\nВложение: {media.get('filename') or media.get('mime_type') or item.get('message_type')}"
                f"\nMedia ID: {media.get('id') or '—'}\n"
            )
        note = (
            f"{marker}\n"
            "ВХОДЯЩЕЕ WHATSAPP-СООБЩЕНИЕ (META CLOUD API)\n\n"
            f"От: {item.get('name') or item.get('phone') or 'Клиент'}\n"
            f"Телефон: {item.get('phone') or '—'}\n"
            f"Время: {_message_time(item)}\n"
            f"Тип: {item.get('message_type') or 'text'}\n"
            f"{media_line}\n"
            f"{item.get('text') or '[без текста]'}"
        )
        await kommo_service.add_common_note(lead_id, note[:13_500])
    except Exception as exc:
        logger.warning("Could not log WhatsApp message to Kommo lead %s: %s", lead_id, exc)


async def _close_active_followup(lead: dict[str, Any], item: dict[str, Any]) -> bool:
    if not followup_service.enabled():
        return False
    lead_id = int(lead.get("id") or 0)
    if not lead_id:
        return False
    message_id = str(item.get("message_id") or "") or None
    try:
        async with AsyncSessionLocal() as db:
            state = await followup_service.close_followup(
                db,
                lead_id=lead_id,
                reason="incoming_whatsapp",
                waiting_on="us",
                action_text="Ответить клиенту",
                incoming_at=_incoming_at(item),
                incoming_message_id=message_id,
            )
            metadata = dict((state.metadata_json or {}).get("followup") or {}) if state else {}
            return (
                metadata.get("closed_reason") == "incoming_whatsapp"
                and metadata.get("incoming_message_id") == message_id
            )
    except Exception as exc:
        logger.warning("Could not reconcile WhatsApp reply with follow-up: %s", exc)
        return False


def _notification_markup(item: dict[str, Any], lead: dict[str, Any] | None) -> dict[str, Any]:
    digits = re.sub(r"\D", "", str(item.get("phone") or ""))
    rows: list[list[dict[str, str]]] = []
    if lead and lead.get("id"):
        rows.append(
            [
                {
                    "text": "✍️ Подготовить ответ",
                    "callback_data": f"followup:prepare:{int(lead['id'])}",
                }
            ]
        )
    if digits:
        rows.append([{"text": "💬 Открыть WhatsApp", "url": f"https://wa.me/{digits}"}])
    if lead and lead.get("url"):
        rows.append([{"text": "🔗 Открыть Kommo", "url": str(lead.get("url"))}])
    return {"inline_keyboard": rows}


async def notify_incoming_message(item: dict[str, Any]) -> None:
    lead = await _find_lead_by_phone(str(item.get("phone") or ""))
    async with AsyncSessionLocal() as db:
        _, created = await whatsapp_cloud_service.record_incoming(db, item=item, lead=lead)
    if not created:
        return

    followup_closed = False
    if lead:
        await _log_to_kommo(lead, item)
        followup_closed = await _close_active_followup(lead, item)
    lead_name = str((lead or {}).get("name") or "Сделка не найдена")
    lead_id = (lead or {}).get("id")
    state_line = (
        "\n✅ Активное ожидание ответа закрыто. Теперь клиент ждёт нас."
        if followup_closed
        else ""
    )
    media = item.get("media") or {}
    attachment_line = ""
    if media:
        attachment_line = (
            "\nВложение: <b>"
            + html.escape(str(media.get("filename") or media.get("mime_type") or item.get("message_type")))
            + "</b>\n<i>Скачивание вложений будет добавлено отдельным модулем.</i>\n"
        )
    text = (
        "💬 <b>НОВОЕ СООБЩЕНИЕ WHATSAPP</b>\n\n"
        f"Клиент: <b>{html.escape(str(item.get('name') or '—'))}</b>\n"
        f"Телефон: <code>{html.escape(str(item.get('phone') or '—'))}</code>\n"
        f"Сделка: {html.escape(lead_name)}\n"
        + (f"Kommo ID: <code>{lead_id}</code>\n" if lead_id else "")
        + f"Время: {_message_time(item)}\n"
        + attachment_line
        + "\n<b>Сообщение</b>\n"
        + html.escape(str(item.get("text") or "[без текста]")[:2500])
        + "\n\nАнализ: клиент написал последним — теперь он ждёт наш ответ."
        + state_line
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


async def process_status_update(item: dict[str, Any]) -> None:
    async with AsyncSessionLocal() as db:
        row = await whatsapp_cloud_service.record_status(db, item=item)
    if row is None:
        return
    if row.status == "failed":
        for chat_id in settings.get_allowed_user_ids():
            try:
                await telegram_service.send_message(
                    chat_id,
                    (
                        "❌ <b>WHATSAPP: ОШИБКА ДОСТАВКИ</b>\n\n"
                        f"Телефон: <code>{html.escape(row.phone)}</code>\n"
                        f"Meta ID: <code>{html.escape(str(row.provider_message_id or '—'))}</code>\n"
                        f"Ошибка: {html.escape(str(row.error_message or row.error_code or 'неизвестно'))}"
                    ),
                )
            except Exception as exc:
                logger.warning("Could not notify WhatsApp failure: %s", exc)


async def process_webhook(payload: dict[str, Any]) -> int:
    messages = extract_incoming_messages(payload)
    statuses = extract_status_updates(payload)
    for item in messages:
        await notify_incoming_message(item)
    for item in statuses:
        await process_status_update(item)
    return len(messages) + len(statuses)
