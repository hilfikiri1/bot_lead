"""Webhook endpoint for Buy & Bring Solutions website contact forms."""

from __future__ import annotations

import hmac
from typing import Any

from fastapi import APIRouter, Header, HTTPException, status
from pydantic import BaseModel, EmailStr, Field

from app.config import get_settings
from app.services.website_form_service import notify_website_form

router = APIRouter(prefix="/webhook/website-form", tags=["website-form"])
settings = get_settings()


class WebsiteFormPayload(BaseModel):
    language: str = "pl"
    pageUrl: str = ""
    formType: str = "contact"
    submittedAt: str = ""
    name: str = Field(min_length=1, max_length=200)
    company: str = ""
    email: EmailStr
    phone: str = ""
    topic: str = ""
    description: str = ""


def require_website_form_secret(
    x_website_lead_secret: str | None = Header(default=None),
) -> None:
    configured = settings.website_lead_webhook_secret.strip()
    if not configured:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Website form webhook is disabled",
        )
    if not hmac.compare_digest(x_website_lead_secret or "", configured):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid webhook secret",
        )


@router.post("", dependencies=[])
async def receive_website_form(
    payload: WebsiteFormPayload,
    x_website_lead_secret: str | None = Header(default=None),
) -> dict[str, Any]:
    require_website_form_secret(x_website_lead_secret)

    if not settings.telegram_bot_token.strip():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Telegram bot is not configured",
        )

    try:
        await notify_website_form(payload.model_dump())
    except RuntimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Telegram delivery failed",
        ) from exc

    return {"success": True}
