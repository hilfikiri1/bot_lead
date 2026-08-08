"""Webhook endpoint for Buy & Bring Solutions website contact forms."""

from __future__ import annotations

import hmac
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request, status
from pydantic import BaseModel, EmailStr, Field, ValidationError
from starlette.datastructures import UploadFile

from app.config import get_settings
from app.services.website_form_service import deliver_website_form

router = APIRouter(prefix="/webhook/website-form", tags=["website-form"])
settings = get_settings()

MAX_ATTACHMENTS = 3
MAX_ATTACHMENTS_TOTAL_BYTES = 3_500_000
ALLOWED_ATTACHMENT_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
    ".pdf",
    ".doc",
    ".docx",
    ".xls",
    ".xlsx",
}


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
    product: str = ""
    quantity: str = ""
    budget: str = ""
    destination: str = ""
    deadline: str = ""
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


async def _read_attachments(values: list[UploadFile]) -> list[dict[str, Any]]:
    if len(values) > MAX_ATTACHMENTS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Too many attachments",
        )

    attachments: list[dict[str, Any]] = []
    total_bytes = 0
    for upload in values:
        filename = (upload.filename or "attachment").strip()[:180]
        if Path(filename).suffix.lower() not in ALLOWED_ATTACHMENT_EXTENSIONS:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Unsupported attachment type",
            )
        content = await upload.read()
        total_bytes += len(content)
        if total_bytes > MAX_ATTACHMENTS_TOTAL_BYTES:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail="Attachments are too large",
            )
        attachments.append(
            {
                "filename": filename,
                "content_type": upload.content_type or "application/octet-stream",
                "content": content,
            }
        )
    return attachments


async def _parse_request(request: Request) -> tuple[WebsiteFormPayload, list[dict[str, Any]]]:
    content_type = request.headers.get("content-type", "").lower()
    try:
        if "multipart/form-data" in content_type:
            form = await request.form()
            raw_payload = form.get("payload")
            if not isinstance(raw_payload, str):
                raise ValueError("Missing multipart payload")
            payload = WebsiteFormPayload.model_validate_json(raw_payload)
            uploads = [
                value
                for key, value in form.multi_items()
                if key == "files" and isinstance(value, UploadFile)
            ]
            return payload, await _read_attachments(uploads)

        data = await request.json()
        return WebsiteFormPayload.model_validate(data), []
    except HTTPException:
        raise
    except (ValidationError, ValueError, TypeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Invalid website form payload",
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid website form request",
        ) from exc


@router.post("", dependencies=[])
async def receive_website_form(
    request: Request,
    x_website_lead_secret: str | None = Header(default=None),
) -> dict[str, Any]:
    require_website_form_secret(x_website_lead_secret)
    payload, attachments = await _parse_request(request)

    try:
        delivery = await deliver_website_form(
            payload.model_dump(),
            attachments=attachments,
        )
    except RuntimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Website lead delivery is unavailable",
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Website lead delivery failed",
        ) from exc

    return {"success": True, **delivery}
