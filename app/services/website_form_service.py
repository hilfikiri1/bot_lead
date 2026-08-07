"""Deliver authenticated website-form submissions to Kommo and Telegram."""

from __future__ import annotations

import html
import logging
from typing import Any

from app.config import get_settings
from app.services import kommo_service, telegram_service

settings = get_settings()
logger = logging.getLogger(__name__)

FORM_TYPE_LABELS: dict[str, str] = {
    "contact": "Запрос с сайта (контакт)",
    "consultation": "Заявка на консультацию",
}

TOPIC_LABELS: dict[str, str] = {
    "sourcing": "Wyszukiwanie producenta",
    "audit": "Weryfikacja / audyt fabryki",
    "qc": "Kontrola jakości",
    "oem": "Private Label / OEM",
    "consolidation": "Konsolidacja",
    "freight": "Transport i odprawa",
    "full": "Kompleksowa obsługa importu",
    "other": "Inne",
    "logistics": "Logistyka i transport",
}


def _escape(value: str) -> str:
    return html.escape(value or "—", quote=False)


def _one_line(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()


def build_website_lead_title(payload: dict[str, Any]) -> str:
    """Create a readable temporary title until the manager assigns a B&BS number."""
    language = _one_line(payload.get("language") or "pl").upper()[:5]
    name = _one_line(payload.get("name")) or "Клиент с сайта"
    description = _one_line(payload.get("description"))
    topic_raw = _one_line(payload.get("topic"))
    topic_label = TOPIC_LABELS.get(topic_raw, topic_raw)
    request_summary = description[:100] or topic_label or "Новый запрос"
    return f"WWW {language} — {name} — {request_summary}"[:255]


def format_website_form_note(payload: dict[str, Any]) -> str:
    """Plain-text source note stored on the Kommo lead."""
    form_type = _one_line(payload.get("formType") or "contact")
    form_label = FORM_TYPE_LABELS.get(form_type, form_type)
    topic_raw = _one_line(payload.get("topic"))
    topic_label = TOPIC_LABELS.get(topic_raw, topic_raw or "—")

    rows = [
        "🌐 ЗАЯВКА С САЙТА BUY & BRING SOLUTIONS",
        "",
        f"Тип: {form_label}",
        f"Имя: {_one_line(payload.get('name')) or '—'}",
        f"Email: {_one_line(payload.get('email')) or '—'}",
    ]
    phone = _one_line(payload.get("phone"))
    if phone:
        rows.append(f"Телефон: {phone}")
    company = _one_line(payload.get("company"))
    if company:
        rows.append(f"Компания: {company}")
    rows.extend(
        [
            f"Тема / услуга: {topic_label}",
            "",
            "Описание:",
            str(payload.get("description") or "—").strip() or "—",
            "",
            f"Страница: {_one_line(payload.get('pageUrl')) or '—'}",
            f"Язык: {_one_line(payload.get('language') or 'pl').upper()}",
            f"Отправлено: {_one_line(payload.get('submittedAt')) or '—'}",
            "Источник: website_form",
        ]
    )
    return "\n".join(rows)


async def sync_website_form_to_kommo(payload: dict[str, Any]) -> dict[str, Any]:
    """Create the CRM contact + lead using the existing server-side Kommo client."""
    client_data = {
        "name": _one_line(payload.get("name")),
        "company": _one_line(payload.get("company")),
        "email": _one_line(payload.get("email")),
        "phone": _one_line(payload.get("phone")),
        "language": _one_line(payload.get("language") or "pl"),
    }
    return await kommo_service.create_lead_from_external_intake(
        lead_title=build_website_lead_title(payload),
        client_data=client_data,
        note_text=format_website_form_note(payload),
    )


def format_website_form_message(
    payload: dict[str, Any],
    *,
    kommo_result: dict[str, Any] | None = None,
    kommo_failed: bool = False,
) -> str:
    form_type = str(payload.get("formType") or "contact")
    form_label = FORM_TYPE_LABELS.get(form_type, form_type)
    topic_raw = str(payload.get("topic") or "").strip()
    topic_label = TOPIC_LABELS.get(topic_raw, topic_raw or "—")
    language = str(payload.get("language") or "pl").upper()
    submitted_at = str(payload.get("submittedAt") or "—")
    page_url = str(payload.get("pageUrl") or "—")

    name = str(payload.get("name") or "")
    email = str(payload.get("email") or "")

    lines = [
        "🌐 <b>Новая заявка с сайта</b>",
        "",
        f"<b>Тип:</b> {_escape(form_label)}",
        f"<b>Имя:</b> {_escape(name)}",
        f"<b>Email:</b> {_escape(email)}",
    ]

    phone = str(payload.get("phone") or "").strip()
    if phone:
        lines.append(f"<b>Телефон:</b> {_escape(phone)}")

    company = str(payload.get("company") or "").strip()
    if company:
        lines.append(f"<b>Компания:</b> {_escape(company)}")

    lines.extend(
        [
            f"<b>Тема / услуга:</b> {_escape(topic_label)}",
            "",
            "<b>Описание:</b>",
            _escape(str(payload.get("description") or "—")),
            "",
            f"<b>Страница:</b> {_escape(page_url)}",
            f"<b>Язык:</b> {language}",
            f"<b>Отправлено:</b> {_escape(submitted_at)}",
        ]
    )

    if kommo_result:
        lead_id = kommo_result.get("lead_id")
        lead_url = str(kommo_result.get("url") or "").strip()
        lines.append("")
        if lead_url:
            safe_url = html.escape(lead_url, quote=True)
            lines.append(
                f'✅ <b>Kommo:</b> <a href="{safe_url}">сделка #{lead_id} создана</a>'
            )
        else:
            lines.append(f"✅ <b>Kommo:</b> сделка #{_escape(str(lead_id or '—'))} создана")
        if kommo_result.get("note_saved") is False:
            lines.append(
                "⚠️ Примечание с данными заявки в Kommo не сохранилось; "
                "полная заявка остаётся в этом сообщении."
            )
    elif kommo_failed:
        lines.extend(
            [
                "",
                "⚠️ <b>Kommo:</b> автоматическая запись не удалась. "
                "Заявка сохранена в Telegram — проверьте CRM.",
            ]
        )

    return "\n".join(lines)


def resolve_notification_chat_id() -> int:
    if settings.telegram_approval_chat_id:
        return int(settings.telegram_approval_chat_id)
    if settings.telegram_owner_user_id:
        return int(settings.telegram_owner_user_id)
    raise RuntimeError(
        "Telegram notification chat is not configured "
        "(set TELEGRAM_APPROVAL_CHAT_ID or TELEGRAM_OWNER_USER_ID)"
    )


async def notify_website_form(
    payload: dict[str, Any],
    *,
    kommo_result: dict[str, Any] | None = None,
    kommo_failed: bool = False,
) -> None:
    chat_id = resolve_notification_chat_id()
    message = format_website_form_message(
        payload,
        kommo_result=kommo_result,
        kommo_failed=kommo_failed,
    )
    lead_url = str((kommo_result or {}).get("url") or "").strip()
    reply_markup = None
    if lead_url.startswith("https://"):
        reply_markup = {
            "inline_keyboard": [[{"text": "Открыть в Kommo", "url": lead_url}]]
        }
    await telegram_service.send_message(
        chat_id,
        message,
        reply_markup=reply_markup,
    )


async def deliver_website_form(payload: dict[str, Any]) -> dict[str, Any]:
    """Fan out one lead to Kommo and Telegram without making either a single point of failure."""
    kommo_result: dict[str, Any] | None = None
    kommo_failed = False
    try:
        kommo_result = await sync_website_form_to_kommo(payload)
    except Exception as exc:
        kommo_failed = True
        logger.error(
            "website_form.kommo_sync_failed error_type=%s",
            type(exc).__name__,
            exc_info=True,
        )

    telegram_sent = False
    try:
        await notify_website_form(
            payload,
            kommo_result=kommo_result,
            kommo_failed=kommo_failed,
        )
        telegram_sent = True
    except Exception as exc:
        logger.error(
            "website_form.telegram_delivery_failed error_type=%s",
            type(exc).__name__,
            exc_info=True,
        )

    if kommo_result is None and not telegram_sent:
        raise RuntimeError("Website lead delivery failed")

    return {
        "kommo": kommo_result is not None,
        "telegram": telegram_sent,
        "lead_id": (kommo_result or {}).get("lead_id"),
    }
