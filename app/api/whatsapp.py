"""Meta WhatsApp Cloud API webhook endpoints."""
from __future__ import annotations

import os
from typing import Any

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Header,
    HTTPException,
    Query,
    Request,
)
from fastapi.responses import PlainTextResponse

from app.services import whatsapp_webhook_service

router = APIRouter(prefix="/webhook/whatsapp", tags=["whatsapp"])


@router.get("", response_class=PlainTextResponse)
async def verify_webhook(
    hub_mode: str | None = Query(default=None, alias="hub.mode"),
    hub_verify_token: str | None = Query(default=None, alias="hub.verify_token"),
    hub_challenge: str | None = Query(default=None, alias="hub.challenge"),
) -> str:
    expected = os.getenv("WHATSAPP_VERIFY_TOKEN", "").strip()
    if hub_mode != "subscribe" or not expected or hub_verify_token != expected:
        raise HTTPException(status_code=403, detail="Invalid WhatsApp verification token")
    return str(hub_challenge or "")


@router.post("")
async def receive_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_hub_signature_256: str | None = Header(
        default=None, alias="X-Hub-Signature-256"
    ),
) -> dict[str, Any]:
    raw_body = await request.body()
    if not whatsapp_webhook_service.verify_meta_signature(
        raw_body, x_hub_signature_256
    ):
        raise HTTPException(status_code=403, detail="Invalid WhatsApp signature")
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON") from exc

    messages = whatsapp_webhook_service.extract_incoming_messages(payload)
    statuses = whatsapp_webhook_service.extract_status_updates(payload)
    for item in messages:
        background_tasks.add_task(
            whatsapp_webhook_service.notify_incoming_message, item
        )
    for item in statuses:
        background_tasks.add_task(
            whatsapp_webhook_service.process_status_update, item
        )
    return {
        "status": "accepted",
        "messages": len(messages),
        "statuses": len(statuses),
    }
