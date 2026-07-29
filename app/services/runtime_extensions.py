"""Small opt-in runtime extensions for onboarding contacts and chat context.

Keeping these wrappers in one module avoids invasive edits to the large Telegram
router and Agent service. The public service functions remain the integration
points used by the rest of the application.
"""
from __future__ import annotations

import html
import logging
import os
from typing import Any

from app.agent import tools as agent_tools
from app.services import (
    client_message_service,
    kommo_chat_service,
    kommo_service,
    telegram_service,
)

logger = logging.getLogger(__name__)
_INSTALLED = False


def _enabled(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().casefold() in {"1", "true", "yes", "on"}


def _chat_section(chat: dict[str, Any]) -> str:
    if not chat or not chat.get("enabled"):
        return ""
    if not chat.get("available"):
        if chat.get("reason") == "external_chat_history_scope_required":
            return (
                "<b>Переписка:</b> требуется разрешение Kommo "
                "<code>External chat history</code>."
            )
        return "<b>Переписка:</b> временно недоступна."

    messages = list(chat.get("messages") or [])
    if not messages:
        return "<b>Переписка:</b> беседы для этой сделки не найдены."

    lines = [
        f"<b>Последняя переписка · {html.escape(str(chat.get('origin') or 'чат'))}</b>"
    ]
    for item in messages[-6:]:
        direction = "Клиент" if item.get("direction") == "incoming" else "Мы"
        body = " ".join(str(item.get("text") or "[вложение]").split())
        if len(body) > 320:
            body = body[:317] + "…"
        lines.append(f"<b>{direction}:</b> {html.escape(body)}")
    analysis = chat.get("analysis") or {}
    if analysis:
        lines.extend(
            [
                "",
                f"<b>Анализ:</b> {html.escape(str(analysis.get('summary') or '—'))}",
                f"<b>Следующий шаг:</b> "
                f"{html.escape(str(analysis.get('recommended_action') or '—'))}",
            ]
        )
    return "\n".join(lines)


def install_runtime_extensions() -> None:
    """Install wrappers exactly once during application startup."""
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True

    original_get_lead_details = kommo_service.get_lead_details

    async def get_lead_details_with_chat(lead_id: int) -> dict[str, Any]:
        details = await original_get_lead_details(lead_id)
        if _enabled("KOMMO_CHAT_CONTEXT_ENABLED"):
            details["chat_context"] = await kommo_chat_service.get_lead_chat_context(
                lead_id
            )
        return details

    kommo_service.get_lead_details = get_lead_details_with_chat

    original_format_lead_summary = agent_tools.format_lead_summary

    def format_lead_summary_with_chat(lead: dict[str, Any]) -> str:
        base = original_format_lead_summary(lead)
        section = _chat_section(dict(lead.get("chat_context") or {}))
        if not section:
            return base
        link_marker = "\n\n<a href="
        if link_marker in base:
            body, link = base.rsplit(link_marker, 1)
            return (body + "\n\n" + section + link_marker + link)[:4000]
        return (base + "\n\n" + section)[:4000]

    agent_tools.format_lead_summary = format_lead_summary_with_chat

    original_send_status_sync_result = telegram_service.send_status_sync_result

    async def send_status_sync_result_with_contacts(
        chat_id: int, result: dict[str, Any]
    ) -> dict[str, Any]:
        response = await original_send_status_sync_result(chat_id, result)
        for card in result.get("contact_cards") or []:
            phone = str(card.get("phone") or "").strip()
            if not phone:
                continue
            try:
                client_name = str(card.get("name") or "Клиент").strip()
                product = str(card.get("product") or "Новый запрос").strip()
                lead_number = str(card.get("lead_number") or "").strip()
                display_name = f"{client_name} — {product}"[:120]
                content = client_message_service.build_vcard(
                    name=display_name,
                    company=(
                        f"B&BS · лид №{lead_number}"
                        if lead_number
                        else "B&BS"
                    ),
                    phone=phone,
                    email=str(card.get("email") or "").strip() or None,
                    language="pl",
                )
                await telegram_service.send_document(
                    chat_id,
                    filename=client_message_service.vcard_filename(display_name),
                    content=content,
                    caption=(
                        f"👤 <b>Контакт для iPhone · №{html.escape(lead_number)}</b>\n"
                        f"{html.escape(client_name)} — {html.escape(product)}\n"
                        "Нажмите на файл и выберите «Создать новый контакт»."
                    ),
                    mime_type="text/vcard",
                )
            except Exception as exc:
                logger.warning("Could not send onboarding vCard: %s", exc)
        return response

    telegram_service.send_status_sync_result = send_status_sync_result_with_contacts
