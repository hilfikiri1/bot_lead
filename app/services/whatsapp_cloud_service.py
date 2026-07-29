"""Durable WhatsApp Cloud API inbox/outbox and confirmed text sending."""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.agent import audit
from app.agent.security import sanitize_text
from app.models.agent_user import AgentUser
from app.models.client_message_draft import ClientMessageDraft
from app.models.whatsapp_cloud_message import WhatsAppCloudMessage
from app.services import client_message_service, identity_service, kommo_service

logger = logging.getLogger(__name__)


class WhatsAppCloudError(RuntimeError):
    pass


class WhatsAppWindowClosedError(WhatsAppCloudError):
    pass


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _env(name: str) -> str:
    return os.getenv(name, "").strip()


def enabled() -> bool:
    return bool(_env("WHATSAPP_ACCESS_TOKEN") and _env("WHATSAPP_PHONE_NUMBER_ID"))


def graph_version() -> str:
    value = _env("WHATSAPP_GRAPH_API_VERSION") or "v23.0"
    if not re.fullmatch(r"v\d+\.\d+", value):
        raise WhatsAppCloudError("WHATSAPP_GRAPH_API_VERSION должен иметь вид v23.0")
    return value


def _api_url() -> str:
    phone_number_id = _env("WHATSAPP_PHONE_NUMBER_ID")
    if not phone_number_id:
        raise WhatsAppCloudError("WHATSAPP_PHONE_NUMBER_ID не настроен")
    return f"https://graph.facebook.com/{graph_version()}/{phone_number_id}/messages"


def _token() -> str:
    token = _env("WHATSAPP_ACCESS_TOKEN")
    if not token:
        raise WhatsAppCloudError("WHATSAPP_ACCESS_TOKEN не настроен")
    return token


def _normalize_phone(phone: str | None, language: str | None = None) -> str:
    return client_message_service.normalize_whatsapp_phone(phone, language=language)


def _provider_datetime(value: Any) -> datetime:
    try:
        return datetime.fromtimestamp(int(value), tz=timezone.utc)
    except (TypeError, ValueError, OSError, OverflowError):
        return utcnow()


async def last_incoming_at(db: AsyncSession, *, phone: str) -> datetime | None:
    normalized = _normalize_phone(phone)
    row = (
        await db.execute(
            select(WhatsAppCloudMessage)
            .where(
                WhatsAppCloudMessage.direction == "incoming",
                WhatsAppCloudMessage.phone == normalized,
            )
            .order_by(desc(WhatsAppCloudMessage.provider_timestamp), desc(WhatsAppCloudMessage.id))
            .limit(1)
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    return row.provider_timestamp or row.created_at


async def freeform_window_open(db: AsyncSession, *, phone: str, at: datetime | None = None) -> bool:
    if _env("WHATSAPP_ENFORCE_24H_WINDOW").casefold() in {"0", "false", "no", "off"}:
        return True
    incoming = await last_incoming_at(db, phone=phone)
    if incoming is None:
        return False
    if incoming.tzinfo is None:
        incoming = incoming.replace(tzinfo=timezone.utc)
    return (at or utcnow()) - incoming <= timedelta(hours=24)


async def record_incoming(
    db: AsyncSession,
    *,
    item: dict[str, Any],
    lead: dict[str, Any] | None,
) -> tuple[WhatsAppCloudMessage, bool]:
    provider_id = str(item.get("message_id") or "").strip()
    if not provider_id:
        raise ValueError("Incoming WhatsApp event has no message id")
    existing = (
        await db.execute(
            select(WhatsAppCloudMessage).where(
                WhatsAppCloudMessage.provider_message_id == provider_id
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing, False

    phone = _normalize_phone(str(item.get("phone") or ""))
    row = WhatsAppCloudMessage(
        provider_message_id=provider_id,
        kommo_lead_id=int((lead or {}).get("id") or 0) or None,
        direction="incoming",
        status="received",
        phone=phone,
        client_name=str(item.get("name") or "").strip() or None,
        message_type=str(item.get("message_type") or "unknown")[:32],
        text=str(item.get("text") or "").strip()[:15000] or None,
        phone_number_id=str(item.get("phone_number_id") or "").strip() or None,
        context_message_id=str(item.get("context_message_id") or "").strip() or None,
        media_json=dict(item.get("media") or {}) or None,
        raw_json=dict(item.get("raw") or item),
        provider_timestamp=_provider_datetime(item.get("timestamp")),
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row, True


async def record_status(db: AsyncSession, *, item: dict[str, Any]) -> WhatsAppCloudMessage | None:
    provider_id = str(item.get("message_id") or "").strip()
    if not provider_id:
        return None
    row = (
        await db.execute(
            select(WhatsAppCloudMessage).where(
                WhatsAppCloudMessage.provider_message_id == provider_id
            )
        )
    ).scalar_one_or_none()
    status = str(item.get("status") or "").strip().casefold()
    occurred_at = _provider_datetime(item.get("timestamp"))
    if row is None:
        recipient = str(item.get("recipient_id") or "").strip()
        if not recipient:
            return None
        row = WhatsAppCloudMessage(
            provider_message_id=provider_id,
            direction="outgoing",
            status=status or "unknown",
            phone=_normalize_phone(recipient),
            message_type="unknown",
            provider_timestamp=occurred_at,
            raw_json=dict(item.get("raw") or item),
        )
        db.add(row)
    else:
        row.status = status or row.status
        row.raw_json = dict(item.get("raw") or item)

    if status == "sent":
        row.sent_at = occurred_at
    elif status == "delivered":
        row.delivered_at = occurred_at
    elif status == "read":
        row.read_at = occurred_at
    elif status == "failed":
        row.failed_at = occurred_at
        errors = item.get("errors") or []
        error = errors[0] if errors and isinstance(errors[0], dict) else {}
        row.error_code = str(error.get("code") or "")[:128] or None
        row.error_message = sanitize_text(
            str(error.get("title") or error.get("message") or error.get("details") or ""),
            limit=2000,
        ) or None
    await db.commit()
    await db.refresh(row)
    return row


async def _send_text_request(*, to: str, body: str) -> dict[str, Any]:
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": "text",
        "text": {"preview_url": False, "body": body},
    }
    timeout = httpx.Timeout(30.0, connect=10.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(
            _api_url(),
            headers={
                "Authorization": f"Bearer {_token()}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
    try:
        data = response.json()
    except Exception:
        data = {"raw": response.text[:2000]}
    if response.status_code >= 400:
        error = data.get("error") if isinstance(data, dict) else None
        message = (
            str((error or {}).get("message") or "")
            or f"Meta HTTP {response.status_code}"
        )
        raise WhatsAppCloudError(sanitize_text(message, limit=1000))
    messages = data.get("messages") or []
    provider_id = str((messages[0] if messages else {}).get("id") or "").strip()
    if not provider_id:
        raise WhatsAppCloudError("Meta приняла запрос без message id")
    return {"provider_message_id": provider_id, "response": data, "payload": payload}


async def _actor(db: AsyncSession, telegram_user_id: int) -> AgentUser:
    actor = await identity_service.get_user_by_telegram_id(db, telegram_user_id)
    if actor is None or actor.status != "active":
        raise PermissionError("Пользователь агента не найден или отключён.")
    if not identity_service.can_write(actor):
        raise PermissionError("Роль Viewer позволяет только просматривать данные.")
    return actor


async def _log_api_send_to_kommo(record: ClientMessageDraft, provider_id: str, actor: AgentUser) -> None:
    marker = f"[BBS-WA-OUT-{provider_id}]"
    actor_label = actor.display_name or (
        f"@{actor.telegram_username}" if actor.telegram_username else str(actor.telegram_user_id)
    )
    note = (
        f"{marker}\n"
        "WHATSAPP-СООБЩЕНИЕ ОТПРАВЛЕНО ЧЕРЕЗ META CLOUD API\n\n"
        f"Язык: {record.communication_language.upper()}\n"
        f"Отправил: {actor_label} (Telegram ID {actor.telegram_user_id})\n"
        f"Meta message ID: {provider_id}\n\n"
        f"Текст:\n{record.body[:5000]}"
    )
    try:
        recent = await kommo_service.get_recent_common_notes(record.kommo_lead_id, limit=50)
        if not any(marker in str(item.get("text") or "") for item in recent):
            await kommo_service.add_common_note(record.kommo_lead_id, note[:13500])
    except Exception as exc:
        logger.warning("Could not log API WhatsApp send in Kommo: %s", exc)


async def send_draft(
    db: AsyncSession,
    *,
    draft_id: int,
    telegram_user_id: int,
) -> dict[str, Any]:
    if not enabled():
        raise WhatsAppCloudError(
            "WhatsApp Cloud API не настроен: нужны WHATSAPP_ACCESS_TOKEN и WHATSAPP_PHONE_NUMBER_ID"
        )
    actor = await _actor(db, telegram_user_id)
    record = await client_message_service.get_draft(db, int(draft_id), lock=True)
    if record is None:
        raise ValueError("Черновик не найден.")
    if record.channel != "whatsapp":
        raise ValueError("Черновик не является WhatsApp-сообщением.")
    if record.status == "cancelled":
        raise ValueError("Черновик был отменён.")
    phone = _normalize_phone(record.recipient, language=record.communication_language)

    existing = (
        await db.execute(
            select(WhatsAppCloudMessage).where(
                WhatsAppCloudMessage.client_message_draft_id == record.id
            )
        )
    ).scalar_one_or_none()
    if existing and existing.provider_message_id and existing.status not in {"failed"}:
        return {
            "draft": record,
            "message": existing,
            "provider_message_id": existing.provider_message_id,
            "idempotent": True,
        }

    if not await freeform_window_open(db, phone=phone):
        raise WhatsAppWindowClosedError(
            "24-часовое окно WhatsApp закрыто или ещё не открывалось. "
            "Для первого/возобновляющего сообщения нужен одобренный шаблон Meta."
        )

    message = existing or WhatsAppCloudMessage(
        client_message_draft_id=record.id,
        kommo_lead_id=record.kommo_lead_id,
        direction="outgoing",
        status="sending",
        phone=phone,
        client_name=record.client_name,
        message_type="text",
        text=record.body,
        phone_number_id=_env("WHATSAPP_PHONE_NUMBER_ID") or None,
    )
    message.status = "sending"
    message.error_code = None
    message.error_message = None
    message.text = record.body
    if existing is None:
        db.add(message)
    await db.commit()

    try:
        result = await _send_text_request(to=phone, body=record.body)
    except Exception as exc:
        message.status = "failed"
        message.failed_at = utcnow()
        message.error_message = sanitize_text(str(exc), limit=2000)
        record.delivery_error = message.error_message
        await db.commit()
        await audit.record_event(
            db,
            service="whatsapp_cloud",
            operation="send_text",
            status="error",
            external_id=str(record.id),
            telegram_user_id=telegram_user_id,
            error_message=str(exc),
        )
        raise

    now = utcnow()
    provider_id = str(result["provider_message_id"])
    message.provider_message_id = provider_id
    message.status = "accepted"
    message.sent_at = now
    message.provider_timestamp = now
    message.raw_json = dict(result.get("response") or {})

    record.status = "sent"
    record.sent_confirmed_by_user_id = actor.id
    record.sent_by_user_id = actor.id
    record.sent_confirmed_at = now
    record.sent_at = now
    record.delivery_error = None
    meta = dict(record.metadata_json or {})
    meta["whatsapp_cloud"] = {
        "provider_message_id": provider_id,
        "phone_number_id": _env("WHATSAPP_PHONE_NUMBER_ID"),
        "status": "accepted",
        "sent_at": now.isoformat(),
    }
    record.metadata_json = meta
    flag_modified(record, "metadata_json")
    await db.commit()
    await db.refresh(message)
    await db.refresh(record)

    await _log_api_send_to_kommo(record, provider_id, actor)
    try:
        actor_label = actor.display_name or str(actor.telegram_user_id)
        await client_message_service._sync_sent_to_notion(
            db, record=record, actor_label=actor_label, occurred_at=now
        )
        await db.commit()
    except Exception as exc:
        logger.warning("Could not sync API WhatsApp send to Notion: %s", exc)

    await audit.record_event(
        db,
        service="whatsapp_cloud",
        operation="send_text",
        status="ok",
        external_id=provider_id,
        telegram_user_id=telegram_user_id,
        result={
            "draft_id": record.id,
            "kommo_lead_id": record.kommo_lead_id,
            "phone": phone,
        },
    )
    return {
        "draft": record,
        "message": message,
        "provider_message_id": provider_id,
        "idempotent": False,
    }


def status_label(status: str | None) -> str:
    return {
        "sending": "отправляется",
        "accepted": "принято Meta",
        "sent": "отправлено",
        "delivered": "доставлено",
        "read": "прочитано",
        "failed": "ошибка",
        "received": "получено",
    }.get(str(status or "").casefold(), str(status or "неизвестно"))
