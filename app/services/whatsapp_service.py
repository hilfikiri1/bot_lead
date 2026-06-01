"""
whatsapp_service.py
Prepares WhatsApp message drafts and provides a stub integration
with the Meta WhatsApp Cloud API. Actual sending is DISABLED until
business verification and explicit approval are confirmed.
"""
from __future__ import annotations

import logging
import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

WHATSAPP_API_URL = "https://graph.facebook.com/v20.0"


def prepare_message_draft(phone: str, message: str) -> dict:
    """
    Prepare a WhatsApp message payload without sending.
    Returns the payload dict for human review.
    """
    return {
        "to": phone,
        "message": message,
        "preview": True,
        "note": "⚠️ This message has NOT been sent. Requires explicit approval.",
    }


async def send_message(phone: str, message: str) -> dict:
    """
    Send a WhatsApp message via Meta Cloud API.
    Only callable if WHATSAPP_ENABLED=true AND explicit approval is granted.
    """
    if not settings.whatsapp_enabled:
        logger.warning("WhatsApp sending is DISABLED. Set WHATSAPP_ENABLED=true to enable.")
        raise RuntimeError(
            "WhatsApp sending is disabled. Enable it in settings after business verification."
        )

    if not settings.whatsapp_phone_number_id or not settings.whatsapp_access_token:
        raise RuntimeError("WhatsApp credentials not configured.")

    payload = {
        "messaging_product": "whatsapp",
        "to": phone,
        "type": "text",
        "text": {"body": message},
    }

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{WHATSAPP_API_URL}/{settings.whatsapp_phone_number_id}/messages",
            headers={
                "Authorization": f"Bearer {settings.whatsapp_access_token}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
        resp.raise_for_status()
        logger.info("WhatsApp message sent to %s", phone)
        return resp.json()
