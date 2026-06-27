"""
kommo_service.py
Read-only Kommo CRM integration layer.

Stage 1: connection test only — no data is written to Kommo.
Uses long-lived Bearer token from private integration.

Auth header: Authorization: Bearer <KOMMO_ACCESS_TOKEN>
Docs: https://www.kommo.com/developers/content/oauth2/
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

# HTTP timeout for all Kommo requests
REQUEST_TIMEOUT = 10.0


def _headers() -> dict[str, str]:
    """Build auth headers. Token is never logged."""
    return {
        "Authorization": f"Bearer {settings.kommo_access_token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def _base_url() -> str:
    return settings.kommo_base_url.rstrip("/")


def _http_error_hint(status_code: int) -> str:
    """Human-readable hint for common Kommo HTTP errors."""
    hints = {
        400: "Неправильный запрос. Проверь KOMMO_BASE_URL.",
        401: "Неверный или просроченный токен. Проверь KOMMO_ACCESS_TOKEN.",
        403: "Нет прав доступа. Убедись, что интеграция включена в Kommo.",
        404: "Неверный поддомен или endpoint. Проверь KOMMO_BASE_URL.",
        429: "Превышен лимит запросов Kommo. Попробуй позже.",
    }
    if 500 <= status_code <= 599:
        return f"Внутренняя ошибка Kommo (HTTP {status_code}). Попробуй позже."
    return hints.get(status_code, f"HTTP {status_code}")


async def get_account_info() -> dict[str, Any]:
    """
    GET /api/v4/account
    Returns account metadata. Read-only, safe.
    """
    if not settings.kommo_access_token:
        raise ValueError("KOMMO_ACCESS_TOKEN не задан в переменных окружения.")
    if not settings.kommo_base_url:
        raise ValueError("KOMMO_BASE_URL не задан в переменных окружения.")

    url = f"{_base_url()}/api/v4/account"
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            resp = await client.get(url, headers=_headers())
    except httpx.TimeoutException:
        raise ConnectionError("Timeout: Kommo не ответил за 10 секунд.")
    except httpx.ConnectError as e:
        raise ConnectionError(f"Нет соединения с Kommo: {e}")

    if resp.status_code != 200:
        hint = _http_error_hint(resp.status_code)
        raise ConnectionError(f"{hint} (HTTP {resp.status_code})")

    try:
        data = resp.json()
    except Exception:
        raise ValueError("Kommo вернул невалидный JSON.")

    return {
        "account_id": data.get("id"),
        "account_name": data.get("name"),
        "subdomain": data.get("subdomain"),
        "timezone": data.get("timezone"),
        "currency": data.get("currency"),
        "version": data.get("version"),
    }


async def get_leads(limit: int = 1) -> dict[str, Any]:
    """
    GET /api/v4/leads?limit=N
    Read-only fetch. Used only to verify access.
    """
    url = f"{_base_url()}/api/v4/leads"
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            resp = await client.get(
                url,
                headers=_headers(),
                params={"limit": limit, "order[id]": "desc"},
            )
    except httpx.TimeoutException:
        raise ConnectionError("Timeout при запросе сделок.")
    except httpx.ConnectError as e:
        raise ConnectionError(f"Нет соединения: {e}")

    if resp.status_code == 204:
        # 204 = success but no leads exist yet
        return {"total": 0, "leads": []}

    if resp.status_code != 200:
        hint = _http_error_hint(resp.status_code)
        raise ConnectionError(f"Ошибка получения сделок: {hint}")

    try:
        data = resp.json()
    except Exception:
        raise ValueError("Kommo вернул невалидный JSON для сделок.")

    embedded = data.get("_embedded", {})
    leads = embedded.get("leads", [])
    page_info = data.get("_page", {})
    total = page_info.get("count", len(leads))

    return {
        "total": total,
        "leads": [
            {"id": l.get("id"), "name": l.get("name"), "status_id": l.get("status_id")}
            for l in leads
        ],
    }


async def test_connection() -> dict[str, Any]:
    """
    Full read-only connection test.
    Returns structured result dict — never raises.
    """
    result: dict[str, Any] = {
        "success": False,
        "account": None,
        "leads_accessible": False,
        "leads_count": 0,
        "error": None,
        "checked_at": datetime.now(tz=timezone.utc).isoformat(),
    }

    # Step 1: account info
    try:
        account = await get_account_info()
        result["account"] = account
    except Exception as e:
        result["error"] = str(e)
        logger.error("Kommo account check failed: %s", e)
        return result

    # Step 2: leads access
    try:
        leads_data = await get_leads(limit=1)
        result["leads_accessible"] = True
        result["leads_count"] = leads_data["total"]
    except Exception as e:
        # Leads check failing is non-fatal — account check already passed
        logger.warning("Kommo leads check failed: %s", e)
        result["leads_accessible"] = False

    result["success"] = True
    return result
