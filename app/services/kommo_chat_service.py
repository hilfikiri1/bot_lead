"""Read external Facebook/WhatsApp chat context linked to a Kommo lead.

Kommo exposes conversation metadata to regular CRM API users. Message history for
channels created by other integrations requires the ``External chat history``
integration scope and an eligible Kommo plan. Failures are converted to a safe
availability status so a lead card still works when the scope is not granted.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

_TIMEOUT = 20.0


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {settings.kommo_access_token}",
        "Accept": "application/hal+json",
        "Content-Type": "application/json",
    }


def _base_url() -> str:
    return settings.kommo_base_url.rstrip("/")


async def _get(path: str, *, params: dict[str, Any] | None = None) -> Any:
    if not settings.kommo_access_token or not settings.kommo_base_url:
        raise RuntimeError("Kommo API is not configured")
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        response = await client.get(
            f"{_base_url()}{path}", headers=_headers(), params=params
        )
    if response.status_code == 204:
        return {}
    if response.status_code in {402, 403}:
        raise PermissionError(
            "Kommo external chat history is unavailable for the current plan or integration scope"
        )
    response.raise_for_status()
    return response.json() if response.content else {}


def _embedded(data: Any, key: str) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if not isinstance(data, dict):
        return []
    items = ((data.get("_embedded") or {}).get(key) or [])
    return [item for item in items if isinstance(item, dict)]


async def get_lead_talks(lead_id: int, *, limit: int = 20) -> list[dict[str, Any]]:
    data = await _get(
        "/api/v4/talks",
        params={
            "filter[entity_id][]": int(lead_id),
            "filter[entity_type]": "lead",
            "limit": max(1, min(int(limit), 250)),
        },
    )
    talks = _embedded(data, "talks")
    talks.sort(key=lambda item: int(item.get("updated_at") or 0), reverse=True)
    return talks


async def get_talk_messages(
    talk_id: int,
    *,
    limit: int = 50,
) -> list[dict[str, Any]]:
    data = await _get(
        f"/api/v4/talks/{int(talk_id)}/messages",
        params={"limit": max(1, min(int(limit), 250))},
    )
    messages = _embedded(data, "messages")
    normalized: list[dict[str, Any]] = []
    for item in messages:
        attachment = item.get("attachment") or {}
        text = str(item.get("text") or "").strip()
        if not text and attachment:
            text = str(
                attachment.get("file_name")
                or attachment.get("type")
                or "Вложение"
            )
        normalized.append(
            {
                "id": item.get("id"),
                "direction": item.get("type"),
                "message_type": item.get("message_type"),
                "text": text,
                "created_at": item.get("created_at"),
                "origin": item.get("origin"),
                "delivery_status": item.get("delivery_status"),
                "author_name": (item.get("author") or {}).get("name"),
                "author_type": (item.get("author") or {}).get("type"),
                "attachment": attachment or None,
            }
        )
    normalized.sort(key=lambda item: int(item.get("created_at") or 0))
    return normalized


def _analyse(messages: list[dict[str, Any]]) -> dict[str, Any]:
    if not messages:
        return {
            "waiting_on": None,
            "summary": "История сообщений пуста.",
            "recommended_action": None,
        }
    last = messages[-1]
    incoming = str(last.get("direction") or "") == "incoming"
    age_hours = None
    created_at = last.get("created_at")
    if isinstance(created_at, (int, float)):
        age_hours = max(
            0,
            int(
                (
                    datetime.now(timezone.utc)
                    - datetime.fromtimestamp(int(created_at), tz=timezone.utc)
                ).total_seconds()
                // 3600
            ),
        )
    origin = str(last.get("origin") or "чат")
    if incoming:
        return {
            "waiting_on": "us",
            "summary": f"Последнее сообщение клиента пришло через {origin}.",
            "recommended_action": "Ответить клиенту или выполнить обещанное действие",
            "age_hours": age_hours,
        }
    return {
        "waiting_on": "client",
        "summary": f"Последним через {origin} написал менеджер.",
        "recommended_action": "Проверить ответ клиента и при необходимости сделать follow-up",
        "age_hours": age_hours,
    }


async def get_lead_chat_context(
    lead_id: int,
    *,
    message_limit: int = 12,
) -> dict[str, Any]:
    """Return latest external chat messages and a simple next-action analysis."""
    if not getattr(settings, "kommo_chat_context_enabled", False):
        return {
            "enabled": False,
            "available": False,
            "reason": "disabled",
            "talks": [],
            "messages": [],
        }
    try:
        talks = await get_lead_talks(lead_id)
        if not talks:
            return {
                "enabled": True,
                "available": True,
                "reason": "no_conversations",
                "talks": [],
                "messages": [],
                "analysis": _analyse([]),
            }
        latest = talks[0]
        talk_id = int(latest.get("talk_id") or 0)
        messages = await get_talk_messages(talk_id, limit=max(message_limit, 20))
        messages = messages[-max(1, min(message_limit, 30)) :]
        return {
            "enabled": True,
            "available": True,
            "reason": None,
            "talk_id": talk_id,
            "origin": latest.get("origin"),
            "status": latest.get("status"),
            "is_read": latest.get("is_read"),
            "talks": talks[:5],
            "messages": messages,
            "analysis": _analyse(messages),
        }
    except PermissionError as exc:
        return {
            "enabled": True,
            "available": False,
            "reason": "external_chat_history_scope_required",
            "detail": str(exc),
            "talks": [],
            "messages": [],
        }
    except Exception as exc:
        logger.warning("Could not load Kommo chat context for lead %s: %s", lead_id, exc)
        return {
            "enabled": True,
            "available": False,
            "reason": "kommo_chat_error",
            "detail": type(exc).__name__,
            "talks": [],
            "messages": [],
        }
