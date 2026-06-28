"""
Kommo CRM integration layer.

Supports:
- read-only connection checks;
- listing all open leads;
- creating a lead from a reviewed Telegram voice-note report.

All write operations must be initiated by an explicit Telegram button click.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

REQUEST_TIMEOUT = 20.0
PAGE_SIZE = 250


class KommoAPIError(RuntimeError):
    """Safe Kommo error that never contains authorization headers."""

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {settings.kommo_access_token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def _base_url() -> str:
    return settings.kommo_base_url.rstrip("/")


def _ensure_configured() -> None:
    if not settings.kommo_access_token:
        raise ValueError("KOMMO_ACCESS_TOKEN не задан в переменных окружения.")
    if not settings.kommo_base_url:
        raise ValueError("KOMMO_BASE_URL не задан в переменных окружения.")


def _http_error_hint(status_code: int) -> str:
    hints = {
        400: "Kommo отклонил данные запроса.",
        401: "Неверный или просроченный токен Kommo.",
        402: "Функция недоступна для текущего состояния подписки Kommo.",
        403: "Недостаточно прав для этой операции в Kommo.",
        404: "Endpoint или сущность Kommo не найдены.",
        429: "Превышен лимит запросов Kommo. Повторите позже.",
    }
    if 500 <= status_code <= 599:
        return f"Внутренняя ошибка Kommo (HTTP {status_code})."
    return hints.get(status_code, f"Ошибка Kommo HTTP {status_code}.")


def _safe_response_preview(response: httpx.Response, limit: int = 500) -> str:
    try:
        payload = response.json()
        text = str(payload)
    except Exception:
        text = response.text
    return text.replace("\n", " ")[:limit]


async def _request(
    method: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    json_body: Any | None = None,
) -> Any | None:
    _ensure_configured()
    url = f"{_base_url()}{path}"

    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            response = await client.request(
                method,
                url,
                headers=_headers(),
                params=params,
                json=json_body,
            )
    except httpx.TimeoutException as exc:
        raise KommoAPIError("Kommo не ответил вовремя.") from exc
    except httpx.ConnectError as exc:
        raise KommoAPIError("Не удалось установить соединение с Kommo.") from exc

    if response.status_code == 204:
        return None

    if not 200 <= response.status_code < 300:
        hint = _http_error_hint(response.status_code)
        preview = _safe_response_preview(response)
        raise KommoAPIError(
            f"{hint} Ответ: {preview}",
            status_code=response.status_code,
        )

    if not response.content:
        return {}

    try:
        return response.json()
    except Exception as exc:
        raise KommoAPIError("Kommo вернул невалидный JSON.") from exc


async def get_account_info() -> dict[str, Any]:
    data = await _request("GET", "/api/v4/account") or {}
    return {
        "account_id": data.get("id"),
        "account_name": data.get("name"),
        "subdomain": data.get("subdomain"),
        "timezone": data.get("timezone"),
        "currency": data.get("currency"),
        "version": data.get("version"),
    }


async def get_leads(limit: int = 1) -> dict[str, Any]:
    """Small read-only lead request used by /kommo_test."""
    limit = max(1, min(limit, PAGE_SIZE))
    data = await _request(
        "GET",
        "/api/v4/leads",
        params={"limit": limit, "order[id]": "desc"},
    )
    if data is None:
        return {"total": 0, "leads": []}

    leads = (data.get("_embedded") or {}).get("leads", [])
    return {
        "total": len(leads),
        "leads": [
            {
                "id": lead.get("id"),
                "name": lead.get("name"),
                "status_id": lead.get("status_id"),
                "pipeline_id": lead.get("pipeline_id"),
                "closed_at": lead.get("closed_at"),
            }
            for lead in leads
        ],
    }


async def get_pipeline_index() -> tuple[dict[int, str], dict[tuple[int, int], str]]:
    """Return pipeline names and status names keyed by Kommo IDs."""
    data = await _request("GET", "/api/v4/leads/pipelines")
    pipelines = ((data or {}).get("_embedded") or {}).get("pipelines", [])

    pipeline_names: dict[int, str] = {}
    status_names: dict[tuple[int, int], str] = {}
    for pipeline in pipelines:
        pipeline_id = pipeline.get("id")
        if not isinstance(pipeline_id, int):
            continue
        pipeline_names[pipeline_id] = pipeline.get("name") or f"Воронка {pipeline_id}"
        statuses = ((pipeline.get("_embedded") or {}).get("statuses") or [])
        for status in statuses:
            status_id = status.get("id")
            if isinstance(status_id, int):
                status_names[(pipeline_id, status_id)] = (
                    status.get("name") or f"Этап {status_id}"
                )
    return pipeline_names, status_names


async def get_all_open_leads(max_pages: int | None = None) -> dict[str, Any]:
    """
    Fetch all leads page by page and keep only leads without closed_at.

    Kommo returns at most 250 entities per page. A configurable page cap protects
    the bot from accidental unbounded requests on very large accounts.
    """
    page_cap = max_pages or settings.kommo_open_leads_max_pages
    page_cap = max(1, min(page_cap, 100))

    open_leads: list[dict[str, Any]] = []
    scanned = 0
    truncated = False

    for page in range(1, page_cap + 1):
        data = await _request(
            "GET",
            "/api/v4/leads",
            params={
                "page": page,
                "limit": PAGE_SIZE,
                "order[updated_at]": "desc",
            },
        )
        if data is None:
            break

        page_leads = ((data.get("_embedded") or {}).get("leads") or [])
        scanned += len(page_leads)
        for lead in page_leads:
            if not lead.get("closed_at"):
                open_leads.append(lead)

        if len(page_leads) < PAGE_SIZE:
            break
        if page == page_cap:
            truncated = True

    try:
        pipeline_names, status_names = await get_pipeline_index()
    except Exception as exc:
        logger.warning("Could not load Kommo pipelines for lead labels: %s", exc)
        pipeline_names, status_names = {}, {}

    normalized: list[dict[str, Any]] = []
    for lead in open_leads:
        pipeline_id = lead.get("pipeline_id")
        status_id = lead.get("status_id")
        normalized.append(
            {
                "id": lead.get("id"),
                "name": lead.get("name") or "Без названия",
                "price": lead.get("price"),
                "pipeline_id": pipeline_id,
                "pipeline_name": pipeline_names.get(pipeline_id, f"Воронка {pipeline_id}"),
                "status_id": status_id,
                "status_name": status_names.get(
                    (pipeline_id, status_id), f"Этап {status_id}"
                ),
                "responsible_user_id": lead.get("responsible_user_id"),
                "updated_at": lead.get("updated_at"),
                "closest_task_at": lead.get("closest_task_at"),
                "url": f"{_base_url()}/leads/detail/{lead.get('id')}",
            }
        )

    normalized.sort(key=lambda item: item.get("updated_at") or 0, reverse=True)
    return {
        "leads": normalized,
        "open_count": len(normalized),
        "scanned_count": scanned,
        "truncated": truncated,
        "page_cap": page_cap,
    }


def _normalize_phone(value: str | None) -> str:
    return re.sub(r"\D", "", value or "")


def _contact_has_exact_value(contact: dict[str, Any], phone: str | None, email: str | None) -> bool:
    normalized_phone = _normalize_phone(phone)
    normalized_email = (email or "").strip().lower()

    for field in contact.get("custom_fields_values") or []:
        code = (field.get("field_code") or "").upper()
        for value_item in field.get("values") or []:
            value = str(value_item.get("value") or "").strip()
            if code == "PHONE" and normalized_phone:
                if _normalize_phone(value) == normalized_phone:
                    return True
            if code == "EMAIL" and normalized_email:
                if value.lower() == normalized_email:
                    return True
    return False


async def find_existing_contact(phone: str | None, email: str | None) -> int | None:
    """Search Kommo and return an exact phone/email match, avoiding partial matches."""
    for query in (phone, email):
        if not query:
            continue
        data = await _request(
            "GET",
            "/api/v4/contacts",
            params={"query": query, "limit": 50},
        )
        if data is None:
            continue
        contacts = ((data.get("_embedded") or {}).get("contacts") or [])
        for contact in contacts:
            if _contact_has_exact_value(contact, phone, email):
                contact_id = contact.get("id")
                if isinstance(contact_id, int):
                    return contact_id
    return None


async def get_contact_field_ids() -> dict[str, int]:
    data = await _request(
        "GET",
        "/api/v4/contacts/custom_fields",
        params={"limit": PAGE_SIZE},
    )
    fields = ((data or {}).get("_embedded") or {}).get("custom_fields", [])
    result: dict[str, int] = {}
    for field in fields:
        code = (field.get("code") or "").upper()
        field_id = field.get("id")
        if code in {"PHONE", "EMAIL"} and isinstance(field_id, int):
            result[code] = field_id
    return result


def _lead_title(client_data: dict[str, Any], lead_data: dict[str, Any]) -> str:
    client_name = (client_data.get("name") or client_data.get("company") or "").strip()
    product = (lead_data.get("product_requested") or "Новый запрос из Telegram").strip()
    title = f"{client_name} — {product}" if client_name else product
    return title[:255]


async def add_common_note(lead_id: int, text: str) -> bool:
    if not text.strip():
        return False
    payload = [
        {
            "entity_id": lead_id,
            "note_type": "common",
            "params": {"text": text[:15000]},
        }
    ]
    await _request("POST", "/api/v4/leads/notes", json_body=payload)
    return True


async def create_lead_from_analysis(
    *,
    client_data: dict[str, Any],
    lead_data: dict[str, Any],
    conversation_summary: str | None,
    recommended_next_step: str | None,
    missing_questions: list[str] | None,
    transcript: str | None,
) -> dict[str, Any]:
    """Create one Kommo lead after explicit human approval in Telegram."""
    lead_payload: dict[str, Any] = {"name": _lead_title(client_data, lead_data)}

    if settings.kommo_default_pipeline_id:
        lead_payload["pipeline_id"] = settings.kommo_default_pipeline_id
    if settings.kommo_default_status_id:
        lead_payload["status_id"] = settings.kommo_default_status_id

    phone = client_data.get("phone")
    email = client_data.get("email")
    contact_name = (
        client_data.get("name")
        or client_data.get("company")
        or "Контакт из Telegram"
    )

    existing_contact_id = await find_existing_contact(phone, email)
    contact_id: int | None = existing_contact_id

    if existing_contact_id:
        lead_payload["_embedded"] = {"contacts": [{"id": existing_contact_id}]}
        data = await _request(
            "POST",
            "/api/v4/leads/complex",
            json_body=[lead_payload],
        )
    elif any([client_data.get("name"), client_data.get("company"), phone, email]):
        field_ids = await get_contact_field_ids()
        custom_values: list[dict[str, Any]] = []
        if phone and field_ids.get("PHONE"):
            custom_values.append(
                {
                    "field_id": field_ids["PHONE"],
                    "values": [{"value": phone}],
                }
            )
        if email and field_ids.get("EMAIL"):
            custom_values.append(
                {
                    "field_id": field_ids["EMAIL"],
                    "values": [{"value": email}],
                }
            )
        contact_payload: dict[str, Any] = {"name": str(contact_name)[:255]}
        if custom_values:
            contact_payload["custom_fields_values"] = custom_values
        lead_payload["_embedded"] = {"contacts": [contact_payload]}
        data = await _request(
            "POST",
            "/api/v4/leads/complex",
            json_body=[lead_payload],
        )
    else:
        data = await _request(
            "POST",
            "/api/v4/leads",
            json_body=[lead_payload],
        )

    created_leads = (((data or {}).get("_embedded") or {}).get("leads") or [])
    if not created_leads:
        raise KommoAPIError("Kommo не вернул созданную сделку в ответе.")

    created = created_leads[0]
    lead_id = created.get("id")
    if not isinstance(lead_id, int):
        raise KommoAPIError("Kommo вернул сделку без корректного ID.")

    embedded_contacts = ((created.get("_embedded") or {}).get("contacts") or [])
    if not contact_id and embedded_contacts:
        maybe_contact_id = embedded_contacts[0].get("id")
        if isinstance(maybe_contact_id, int):
            contact_id = maybe_contact_id

    note_parts = [
        "Создано из Telegram после подтверждения менеджером.",
        f"Товар/запрос: {lead_data.get('product_requested') or 'не указан'}",
        f"Бюджет: {lead_data.get('budget') or 'не указан'}",
        f"Страна/город: {lead_data.get('country') or '—'} / {lead_data.get('city') or '—'}",
    ]
    if conversation_summary:
        note_parts.append(f"Краткое резюме: {conversation_summary}")
    if recommended_next_step:
        note_parts.append(f"Следующий шаг: {recommended_next_step}")
    if missing_questions:
        note_parts.append("Что уточнить:\n- " + "\n- ".join(missing_questions[:20]))
    if transcript:
        note_parts.append(f"Транскрипт:\n{transcript[:8000]}")

    note_saved = False
    try:
        note_saved = await add_common_note(lead_id, "\n\n".join(note_parts))
    except Exception as exc:
        logger.warning("Lead %s created, but note could not be added: %s", lead_id, exc)

    return {
        "lead_id": lead_id,
        "lead_name": created.get("name") or lead_payload["name"],
        "contact_id": contact_id,
        "note_saved": note_saved,
        "url": f"{_base_url()}/leads/detail/{lead_id}",
    }


async def test_connection() -> dict[str, Any]:
    result: dict[str, Any] = {
        "success": False,
        "account": None,
        "leads_accessible": False,
        "leads_count": 0,
        "error": None,
        "checked_at": datetime.now(tz=timezone.utc).isoformat(),
    }

    try:
        result["account"] = await get_account_info()
    except Exception as exc:
        result["error"] = str(exc)
        logger.error("Kommo account check failed: %s", exc)
        return result

    try:
        leads_data = await get_leads(limit=1)
        result["leads_accessible"] = True
        result["leads_count"] = leads_data["total"]
    except Exception as exc:
        logger.warning("Kommo leads check failed: %s", exc)

    result["success"] = True
    return result
