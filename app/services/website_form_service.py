"""Format and deliver website form submissions to Telegram."""

from __future__ import annotations

import html
from typing import Any

from app.config import get_settings
from app.services import telegram_service

settings = get_settings()

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


def format_website_form_message(payload: dict[str, Any]) -> str:
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


async def notify_website_form(payload: dict[str, Any]) -> None:
    chat_id = resolve_notification_chat_id()
    message = format_website_form_message(payload)
    await telegram_service.send_message(chat_id, message)
