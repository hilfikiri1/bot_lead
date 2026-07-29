"""Client-facing drafts, WhatsApp handoff, vCard and delivery audit."""

from __future__ import annotations

import html
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent import audit
from app.agent.security import sanitize_text
from app.models.agent_user import AgentUser
from app.models.client_message_draft import ClientMessageDraft
from app.services import client_language_service, identity_service, kommo_service


def normalize_whatsapp_phone(phone: str | None, *, language: str | None = None) -> str:
    raw = str(phone or "").strip()
    digits = re.sub(r"\D", "", raw)
    if digits.startswith("00"):
        digits = digits[2:]
    if digits.startswith("0"):
        digits = digits.lstrip("0")
    if len(digits) == 9 and language == "pl":
        digits = "48" + digits
    elif len(digits) == 9 and language == "uk":
        digits = "380" + digits
    if not 8 <= len(digits) <= 15:
        raise ValueError("В Kommo нет корректного международного номера телефона.")
    return digits


def whatsapp_click_to_chat_url(
    phone: str | None,
    message: str,
    *,
    language: str | None = None,
) -> str:
    digits = normalize_whatsapp_phone(phone, language=language)
    # Keep the deep link usable across iOS/Android and below practical URL
    # limits. The complete text remains visible in Telegram for manual editing.
    compact_message = str(message or "").strip()[:1800]
    return f"https://wa.me/{digits}?text={quote(compact_message, safe='')}"


def _vcard_escape(value: str | None) -> str:
    return (
        str(value or "")
        .replace("\\", "\\\\")
        .replace("\r", "")
        .replace("\n", "\\n")
        .replace(";", "\\;")
        .replace(",", "\\,")
    )


def build_vcard(
    *,
    name: str | None,
    company: str | None,
    phone: str | None,
    email: str | None = None,
    language: str | None = None,
) -> bytes:
    display_name = str(name or company or "B&BS client").strip()
    lines = [
        "BEGIN:VCARD",
        "VERSION:3.0",
        f"FN:{_vcard_escape(display_name)}",
    ]
    if company:
        lines.append(f"ORG:{_vcard_escape(company)}")
    if phone:
        digits = normalize_whatsapp_phone(phone, language=language)
        lines.append(f"TEL;TYPE=CELL:+{digits}")
    if email:
        lines.append(f"EMAIL;TYPE=INTERNET:{_vcard_escape(email)}")
    lines.extend(["NOTE:Contact prepared by Buy & Bring Solutions", "END:VCARD", ""])
    return "\r\n".join(lines).encode("utf-8")


def vcard_filename(name: str | None) -> str:
    safe = re.sub(r"[^A-Za-zА-Яа-яЁёІіЇїЄєҐґ0-9._-]+", "_", str(name or "contact"))
    return f"{safe.strip('._')[:80] or 'contact'}.vcf"


async def _actor(db: AsyncSession, telegram_user_id: int) -> AgentUser:
    user = await identity_service.get_user_by_telegram_id(db, telegram_user_id)
    if user is None or user.status != "active":
        raise PermissionError("Пользователь агента не найден или отключён.")
    if not identity_service.can_write(user):
        raise PermissionError("Роль Viewer позволяет только просматривать данные.")
    return user


async def create_client_message_draft(
    db: AsyncSession,
    *,
    telegram_user_id: int,
    lead: dict[str, Any],
    draft: dict[str, Any],
    language_source: str,
    client_id: int | None,
    channel: str = "whatsapp",
) -> ClientMessageDraft:
    actor = await _actor(db, telegram_user_id)
    contact = ((lead.get("contacts") or [{}])[0]) or {}
    phones = contact.get("phones") or []
    emails = contact.get("emails") or []
    recipient = (phones[0] if phones else None) if channel == "whatsapp" else (
        emails[0] if emails else None
    )
    record = ClientMessageDraft(
        kommo_lead_id=int(lead["id"]),
        kommo_contact_id=(
            int(contact["id"]) if isinstance(contact.get("id"), int) else None
        ),
        client_id=client_id,
        channel=channel,
        communication_language=str(draft.get("language") or "pl")[:10],
        language_source=language_source[:32],
        recipient=recipient,
        client_name=contact.get("name"),
        company=next(
            (
                str(field.get("value"))
                for field in (contact.get("custom_fields") or [])
                if "company" in str(field.get("code") or "").casefold()
                or "компан" in str(field.get("name") or "").casefold()
            ),
            None,
        ),
        subject=draft.get("subject"),
        body=str(draft.get("body") or "").strip(),
        status="prepared",
        prepared_by_user_id=actor.id,
        delivery_marker=f"BBS-MSG-{uuid4().hex}",
        metadata_json={
            "lead_name": lead.get("name"),
            "lead_url": lead.get("url"),
            "draft_kind": draft.get("kind"),
            "email": emails[0] if emails else None,
        },
    )
    if not record.body:
        raise ValueError("Текст клиентского сообщения пустой.")
    db.add(record)
    await db.commit()
    await db.refresh(record)
    await audit.record_event(
        db,
        service="client_message",
        operation="prepared",
        status="ok",
        external_id=str(record.id),
        telegram_user_id=telegram_user_id,
        payload={
            "kommo_lead_id": record.kommo_lead_id,
            "channel": record.channel,
            "language": record.communication_language,
            "language_source": record.language_source,
        },
    )
    return record


async def get_draft(
    db: AsyncSession, draft_id: int, *, lock: bool = False
) -> ClientMessageDraft | None:
    query = select(ClientMessageDraft).where(ClientMessageDraft.id == int(draft_id))
    if lock:
        query = query.with_for_update()
    return (await db.execute(query)).scalar_one_or_none()


def format_client_message_draft(record: ClientMessageDraft) -> str:
    language = client_language_service.LANGUAGE_LABELS.get(
        record.communication_language, record.communication_language.upper()
    )
    channel = "WhatsApp" if record.channel == "whatsapp" else record.channel.upper()
    lines = [
        (
            f"<b>{html.escape(str(record.client_name or 'Клиент'))} — "
            f"{html.escape(channel)} — язык: {html.escape(language)}</b>"
        ),
        "",
        html.escape(record.body[:3200]),
    ]
    if len(record.body) > 3200:
        lines.extend(["", "…текст сокращён в предпросмотре"])
    if not record.recipient:
        lines.extend(
            [
                "",
                "⚠️ В Kommo не найден номер телефона. Добавьте его перед открытием WhatsApp.",
            ]
        )
    lines.extend(
        [
            "",
            "Сообщение ещё не отправлено. Отправка выполняется вручную с вашего телефона.",
        ]
    )
    return "\n".join(lines)


def message_draft_markup(record: ClientMessageDraft) -> dict[str, Any]:
    rows: list[list[dict[str, str]]] = []
    if record.channel == "whatsapp" and record.recipient:
        try:
            rows.append(
                [
                    {
                        "text": "💬 Открыть WhatsApp",
                        "url": whatsapp_click_to_chat_url(
                            record.recipient,
                            record.body,
                            language=record.communication_language,
                        ),
                    }
                ]
            )
        except ValueError:
            pass
    rows.append(
        [
            {"text": "✏️ Изменить текст", "callback_data": f"clientmsg:edit:{record.id}"},
            {"text": "👤 Контакт .vcf", "callback_data": f"clientmsg:vcf:{record.id}"},
        ]
    )
    rows.append(
        [
            {"text": "PL", "callback_data": f"clientmsg:lang:pl:{record.id}"},
            {"text": "UA", "callback_data": f"clientmsg:lang:uk:{record.id}"},
            {"text": "RU", "callback_data": f"clientmsg:lang:ru:{record.id}"},
        ]
    )
    rows.append(
        [
            {
                "text": "✅ Да, отметить в Kommo",
                "callback_data": f"clientmsg:sent:{record.id}",
            },
            {"text": "❌ Отмена", "callback_data": f"clientmsg:cancel:{record.id}"},
        ]
    )
    return {"inline_keyboard": rows}


async def update_body(
    db: AsyncSession,
    *,
    draft_id: int,
    telegram_user_id: int,
    body: str,
) -> ClientMessageDraft:
    actor = await _actor(db, telegram_user_id)
    record = await get_draft(db, draft_id, lock=True)
    if record is None:
        raise ValueError("Черновик не найден.")
    if record.status in {"sent", "cancelled"}:
        raise ValueError("Отправленный или отменённый черновик нельзя редактировать.")
    clean = str(body or "").strip()
    if not clean:
        raise ValueError("Текст сообщения не может быть пустым.")
    record.body = clean[:15_000]
    record.status = "edited"
    record.last_edited_by_user_id = actor.id
    record.edited_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(record)
    await audit.record_event(
        db,
        service="client_message",
        operation="edited",
        status="ok",
        external_id=str(record.id),
        telegram_user_id=telegram_user_id,
        result={"kommo_lead_id": record.kommo_lead_id},
    )
    return record


async def update_language_and_body(
    db: AsyncSession,
    *,
    draft_id: int,
    telegram_user_id: int,
    lead: dict[str, Any],
    language: str,
    generated: dict[str, Any],
) -> ClientMessageDraft:
    actor = await _actor(db, telegram_user_id)
    resolution = await client_language_service.set_communication_language(
        db,
        lead=lead,
        language=language,
        telegram_user_id=telegram_user_id,
    )
    record = await get_draft(db, draft_id, lock=True)
    if record is None:
        raise ValueError("Черновик не найден.")
    if record.status in {"sent", "cancelled"}:
        raise ValueError("Отправленный или отменённый черновик нельзя менять.")
    record.communication_language = resolution.language
    record.language_source = resolution.source
    record.body = str(generated.get("body") or "").strip()[:15_000]
    record.subject = generated.get("subject")
    record.status = "edited"
    record.last_edited_by_user_id = actor.id
    record.edited_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(record)
    await audit.record_event(
        db,
        service="client_message",
        operation="language_changed",
        status="ok",
        external_id=str(record.id),
        telegram_user_id=telegram_user_id,
        result={
            "kommo_lead_id": record.kommo_lead_id,
            "language": record.communication_language,
        },
    )
    return record


async def cancel_draft(
    db: AsyncSession, *, draft_id: int, telegram_user_id: int
) -> ClientMessageDraft:
    await _actor(db, telegram_user_id)
    record = await get_draft(db, draft_id, lock=True)
    if record is None:
        raise ValueError("Черновик не найден.")
    if record.status == "sent":
        raise ValueError("Сообщение уже отмечено как отправленное.")
    record.status = "cancelled"
    record.cancelled_at = datetime.now(timezone.utc)
    await db.commit()
    await audit.record_event(
        db,
        service="client_message",
        operation="cancelled",
        status="ok",
        external_id=str(record.id),
        telegram_user_id=telegram_user_id,
        result={"kommo_lead_id": record.kommo_lead_id},
    )
    return record


async def confirm_sent(
    db: AsyncSession, *, draft_id: int, telegram_user_id: int
) -> ClientMessageDraft:
    actor = await _actor(db, telegram_user_id)
    record = await get_draft(db, draft_id, lock=True)
    if record is None:
        raise ValueError("Черновик не найден.")
    if record.status == "sent":
        return record
    if record.status == "cancelled":
        raise ValueError("Черновик был отменён.")
    if record.status == "confirming_sent":
        started = record.sent_confirmed_at
        if started is None:
            raise ValueError("Подтверждение уже обрабатывается.")
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) - started < timedelta(minutes=2):
            raise ValueError("Подтверждение уже обрабатывается.")

    now = datetime.now(timezone.utc)
    record.status = "confirming_sent"
    record.sent_confirmed_by_user_id = actor.id
    record.sent_by_user_id = actor.id
    record.sent_confirmed_at = now
    record.sent_at = now
    record.delivery_error = None
    await db.commit()

    actor_label = actor.display_name or (
        f"@{actor.telegram_username}" if actor.telegram_username else str(actor.telegram_user_id)
    )
    preparer = await db.get(AgentUser, int(record.prepared_by_user_id))
    preparer_label = (
        preparer.display_name
        if preparer and preparer.display_name
        else (
            f"@{preparer.telegram_username}"
            if preparer and preparer.telegram_username
            else str(preparer.telegram_user_id if preparer else record.prepared_by_user_id)
        )
    )
    marker = f"[{record.delivery_marker}]"
    note = (
        f"{marker}\n"
        "WhatsApp-сообщение отправлено вручную после подтверждения в Telegram.\n"
        f"Язык: {record.communication_language.upper()}\n"
        f"Подготовил: {preparer_label}\n"
        f"Отправил: {actor_label} (Telegram ID {actor.telegram_user_id})\n\n"
        f"Текст:\n{record.body[:5000]}"
    )
    try:
        recent = await kommo_service.get_recent_common_notes(
            record.kommo_lead_id, limit=50
        )
        already_logged = any(
            marker in str(item.get("text") or "") for item in recent
        )
        if not already_logged:
            await kommo_service.add_common_note(record.kommo_lead_id, note)
        record.status = "sent"
        record.delivery_error = None
        await db.commit()
        await audit.record_event(
            db,
            service="client_message",
            operation="sent_confirmed",
            status="ok",
            external_id=str(record.id),
            telegram_user_id=telegram_user_id,
            result={
                "kommo_lead_id": record.kommo_lead_id,
                "kommo_note_logged": True,
                "delivery_marker": record.delivery_marker,
            },
        )
        return record
    except Exception as exc:
        record.status = "sent_log_failed"
        record.delivery_error = sanitize_text(str(exc), limit=2000)
        await db.commit()
        await audit.record_event(
            db,
            service="client_message",
            operation="sent_confirmed",
            status="error",
            external_id=str(record.id),
            telegram_user_id=telegram_user_id,
            payload={"kommo_lead_id": record.kommo_lead_id},
            error_message=str(exc),
        )
        raise
