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
        statuses = (pipeline.get("_embedded") or {}).get("statuses") or []
        for status in statuses:
            status_id = status.get("id")
            if isinstance(status_id, int):
                status_names[(pipeline_id, status_id)] = (
                    status.get("name") or f"Этап {status_id}"
                )
    return pipeline_names, status_names


async def _resolve_configured_lead_placement() -> dict[str, int]:
    """Validate configured pipeline/status IDs before sending a create request.

    A stale or copied STATUS_ID makes Kommo reject the whole lead with
    NotSupportedChoice.  Invalid values are ignored so Kommo can place the lead
    into the first stage of the selected or main pipeline.
    """
    configured_pipeline = settings.kommo_default_pipeline_id
    configured_status = settings.kommo_default_status_id
    if not configured_pipeline and not configured_status:
        return {}

    try:
        data = await _request("GET", "/api/v4/leads/pipelines")
    except Exception as exc:
        logger.warning(
            "Could not validate Kommo pipeline/status; using Kommo defaults: %s",
            exc,
        )
        return {}

    pipelines = ((data or {}).get("_embedded") or {}).get("pipelines", [])
    pipeline_by_id = {
        pipeline.get("id"): pipeline
        for pipeline in pipelines
        if isinstance(pipeline.get("id"), int)
    }

    selected_pipeline = None
    if configured_pipeline:
        selected_pipeline = pipeline_by_id.get(configured_pipeline)
        if selected_pipeline is None:
            logger.warning(
                "KOMMO_DEFAULT_PIPELINE_ID=%s does not exist; using Kommo default",
                configured_pipeline,
            )
            return {}
    elif configured_status:
        for pipeline in pipelines:
            statuses = (pipeline.get("_embedded") or {}).get("statuses") or []
            if any(status.get("id") == configured_status for status in statuses):
                selected_pipeline = pipeline
                break
        if selected_pipeline is None:
            logger.warning(
                "KOMMO_DEFAULT_STATUS_ID=%s does not exist; using Kommo default",
                configured_status,
            )
            return {}

    if selected_pipeline is None:
        return {}

    pipeline_id = selected_pipeline.get("id")
    result: dict[str, int] = {"pipeline_id": pipeline_id}
    if configured_status:
        statuses = (selected_pipeline.get("_embedded") or {}).get("statuses") or []
        valid_status_ids = {
            status.get("id") for status in statuses if isinstance(status.get("id"), int)
        }
        if configured_status in valid_status_ids:
            result["status_id"] = configured_status
        else:
            logger.warning(
                "KOMMO_DEFAULT_STATUS_ID=%s is not part of pipeline %s; "
                "creating in the pipeline default stage",
                configured_status,
                pipeline_id,
            )
    return result


async def _submit_new_lead(lead_payload: dict[str, Any], endpoint: str) -> Any:
    """Submit a lead and retry once without invalid placement metadata."""
    try:
        return await _request("POST", endpoint, json_body=[lead_payload])
    except KommoAPIError as exc:
        error_text = str(exc)
        has_placement = "status_id" in lead_payload or "pipeline_id" in lead_payload
        invalid_placement = (
            exc.status_code == 400
            and has_placement
            and (
                "NotSupportedChoice" in error_text
                or "status_id" in error_text
                or "pipeline_id" in error_text
            )
        )
        if not invalid_placement:
            raise

        safe_payload = dict(lead_payload)
        safe_payload.pop("status_id", None)
        safe_payload.pop("pipeline_id", None)
        logger.warning(
            "Kommo rejected configured pipeline/status; retrying lead creation "
            "with Kommo default placement"
        )
        return await _request("POST", endpoint, json_body=[safe_payload])


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

        page_leads = (data.get("_embedded") or {}).get("leads") or []
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
                "pipeline_name": pipeline_names.get(
                    pipeline_id, f"Воронка {pipeline_id}"
                ),
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


def _contact_has_exact_value(
    contact: dict[str, Any], phone: str | None, email: str | None
) -> bool:
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
        contacts = (data.get("_embedded") or {}).get("contacts") or []
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


def _lead_title(
    client_data: dict[str, Any],
    lead_data: dict[str, Any],
    *,
    lead_name_override: str | None = None,
) -> str:
    override = (lead_name_override or "").strip()
    if override:
        return override[:255]
    number = str(lead_data.get("lead_number") or "").strip()
    proposed = str(lead_data.get("proposed_name") or "").strip()
    product = str(lead_data.get("product_requested") or "Новый запрос").strip()
    base = proposed or product
    if number and not base.startswith(number):
        base = f"{number} {base}"
    return base[:255]


async def get_default_lead_placement_preview() -> dict[str, Any]:
    """Return the validated destination displayed before lead creation."""
    placement = await _resolve_configured_lead_placement()
    pipeline_names, status_names = await get_pipeline_index()
    pipeline_id = placement.get("pipeline_id")
    status_id = placement.get("status_id")
    if pipeline_id is None:
        return {
            "pipeline_id": None,
            "status_id": None,
            "pipeline_name": "Основная воронка Kommo",
            "status_name": "Первый доступный этап",
        }
    return {
        "pipeline_id": pipeline_id,
        "status_id": status_id,
        "pipeline_name": pipeline_names.get(pipeline_id, f"Воронка {pipeline_id}"),
        "status_name": status_names.get(
            (pipeline_id, status_id),
            "Первый этап выбранной воронки"
            if status_id is None
            else f"Этап {status_id}",
        ),
    }


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
    lead_name_override: str | None = None,
) -> dict[str, Any]:
    """Create one Kommo lead after explicit human approval in Telegram."""
    lead_payload: dict[str, Any] = {
        "name": _lead_title(
            client_data,
            lead_data,
            lead_name_override=lead_name_override,
        )
    }
    lead_payload.update(await _resolve_configured_lead_placement())

    phone = client_data.get("phone")
    email = client_data.get("email")
    contact_name = (
        client_data.get("name") or client_data.get("company") or "Контакт из Telegram"
    )

    existing_contact_id = await find_existing_contact(phone, email)
    contact_id: int | None = existing_contact_id

    if existing_contact_id:
        lead_payload["_embedded"] = {"contacts": [{"id": existing_contact_id}]}
        data = await _submit_new_lead(lead_payload, "/api/v4/leads/complex")
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
        data = await _submit_new_lead(lead_payload, "/api/v4/leads/complex")
    else:
        data = await _submit_new_lead(lead_payload, "/api/v4/leads")

    created_leads = ((data or {}).get("_embedded") or {}).get("leads") or []
    if not created_leads:
        raise KommoAPIError("Kommo не вернул созданную сделку в ответе.")

    created = created_leads[0]
    lead_id = created.get("id")
    if not isinstance(lead_id, int):
        raise KommoAPIError("Kommo вернул сделку без корректного ID.")

    embedded_contacts = (created.get("_embedded") or {}).get("contacts") or []
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

    pipeline_id = created.get("pipeline_id") or lead_payload.get("pipeline_id")
    status_id = created.get("status_id") or lead_payload.get("status_id")
    try:
        pipeline_names, status_names = await get_pipeline_index()
    except Exception:
        pipeline_names, status_names = {}, {}

    return {
        "lead_id": lead_id,
        "lead_name": created.get("name") or lead_payload["name"],
        "contact_id": contact_id,
        "pipeline_id": pipeline_id,
        "pipeline_name": pipeline_names.get(
            pipeline_id, f"Воронка {pipeline_id}" if pipeline_id else "—"
        ),
        "status_id": status_id,
        "status_name": status_names.get(
            (pipeline_id, status_id),
            f"Этап {status_id}" if status_id else "Первый этап",
        ),
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


def _flatten_custom_fields(entity: dict[str, Any]) -> list[dict[str, str]]:
    """Return human-readable custom-field values from a Kommo entity."""
    result: list[dict[str, str]] = []
    for field in entity.get("custom_fields_values") or []:
        field_name = (
            field.get("field_name")
            or field.get("field_code")
            or f"Поле {field.get('field_id')}"
        )
        field_code = field.get("field_code") or ""
        values: list[str] = []
        for item in field.get("values") or []:
            value = item.get("value")
            if value not in (None, ""):
                values.append(str(value))
        if values:
            result.append(
                {
                    "name": str(field_name),
                    "code": str(field_code),
                    "value": ", ".join(values),
                }
            )
    return result


def _contact_channels(contact: dict[str, Any]) -> tuple[list[str], list[str]]:
    phones: list[str] = []
    emails: list[str] = []
    for field in contact.get("custom_fields_values") or []:
        code = (field.get("field_code") or "").upper()
        for item in field.get("values") or []:
            value = str(item.get("value") or "").strip()
            if not value:
                continue
            if code == "PHONE":
                phones.append(value)
            elif code == "EMAIL":
                emails.append(value)
    return phones, emails


async def get_recent_common_notes(lead_id: int, limit: int = 5) -> list[dict[str, Any]]:
    """Return recent common text notes for a lead."""
    data = await _request(
        "GET",
        f"/api/v4/leads/{lead_id}/notes",
        params={
            "limit": max(1, min(limit, 50)),
            "order[updated_at]": "desc",
            "filter[note_type][]": "common",
        },
    )
    notes = ((data or {}).get("_embedded") or {}).get("notes") or []
    normalized: list[dict[str, Any]] = []
    for note in notes:
        params = note.get("params") or {}
        text = params.get("text")
        if not text:
            continue
        normalized.append(
            {
                "id": note.get("id"),
                "text": str(text),
                "created_at": note.get("created_at"),
                "updated_at": note.get("updated_at"),
                "created_by": note.get("created_by"),
            }
        )
    return normalized


async def get_lead_details(lead_id: int) -> dict[str, Any]:
    """Load one lead, its linked contacts, pipeline labels and recent common notes."""
    if lead_id <= 0:
        raise ValueError("Некорректный ID сделки.")

    lead = await _request(
        "GET",
        f"/api/v4/leads/{lead_id}",
        params={"with": "contacts"},
    )
    if not lead:
        raise KommoAPIError("Сделка не найдена.", status_code=404)

    try:
        pipeline_names, status_names = await get_pipeline_index()
    except Exception as exc:
        logger.warning("Could not load pipeline labels for lead %s: %s", lead_id, exc)
        pipeline_names, status_names = {}, {}

    contact_refs = (lead.get("_embedded") or {}).get("contacts") or []
    contacts: list[dict[str, Any]] = []
    for ref in contact_refs[:5]:
        contact_id = ref.get("id")
        if not isinstance(contact_id, int):
            continue
        try:
            contact = await _request("GET", f"/api/v4/contacts/{contact_id}") or {}
        except Exception as exc:
            logger.warning(
                "Could not load contact %s for lead %s: %s", contact_id, lead_id, exc
            )
            continue
        phones, emails = _contact_channels(contact)
        contacts.append(
            {
                "id": contact_id,
                "name": contact.get("name") or "Без имени",
                "phones": phones,
                "emails": emails,
                "custom_fields": _flatten_custom_fields(contact),
            }
        )

    try:
        notes = await get_recent_common_notes(lead_id, limit=5)
    except Exception as exc:
        logger.warning("Could not load notes for lead %s: %s", lead_id, exc)
        notes = []

    pipeline_id = lead.get("pipeline_id")
    status_id = lead.get("status_id")
    return {
        "id": lead.get("id"),
        "name": lead.get("name") or "Без названия",
        "price": lead.get("price"),
        "pipeline_id": pipeline_id,
        "pipeline_name": pipeline_names.get(pipeline_id, f"Воронка {pipeline_id}"),
        "status_id": status_id,
        "status_name": status_names.get((pipeline_id, status_id), f"Этап {status_id}"),
        "responsible_user_id": lead.get("responsible_user_id"),
        "created_at": lead.get("created_at"),
        "updated_at": lead.get("updated_at"),
        "closed_at": lead.get("closed_at"),
        "closest_task_at": lead.get("closest_task_at"),
        "custom_fields": _flatten_custom_fields(lead),
        "contacts": contacts,
        "notes": notes,
        "url": f"{_base_url()}/leads/detail/{lead_id}",
    }


async def get_open_leads_page(
    page: int = 1, page_size: int | None = None
) -> dict[str, Any]:
    """Return one Telegram menu page of open leads."""
    page_size = page_size or settings.kommo_menu_page_size
    page_size = max(1, min(page_size, 20))
    result = await get_all_open_leads()
    leads = result.get("leads") or []
    total = len(leads)
    total_pages = max(1, (total + page_size - 1) // page_size)
    page = max(1, min(page, total_pages))
    start = (page - 1) * page_size
    return {
        **result,
        "leads": leads[start : start + page_size],
        "page": page,
        "page_size": page_size,
        "total_pages": total_pages,
        "open_count": total,
    }


def _normalize_search_text(value: Any) -> str:
    text = str(value or "").casefold().replace("ё", "е")
    text = re.sub(r"[^\w]+", " ", text, flags=re.UNICODE)
    return " ".join(text.split())


def _lead_name_match_score(name: Any, query: str) -> int | None:
    """Return a lower-is-better score for partial lead-title matching."""
    normalized_name = _normalize_search_text(name)
    normalized_query = _normalize_search_text(query)
    if not normalized_name or not normalized_query:
        return None
    if normalized_name == normalized_query:
        return 0
    if normalized_name.startswith(normalized_query):
        return 1
    if normalized_query in normalized_name:
        return 2
    tokens = normalized_query.split()
    if tokens and all(token in normalized_name for token in tokens):
        return 3
    return None


async def search_open_leads(query: str, limit: int = 20) -> dict[str, Any]:
    """
    Search open leads by exact Kommo ID and by a partial fragment of the lead title.

    The local title scan is intentional: numeric prefixes such as ``90`` and title
    fragments such as ``надувная`` must match a lead named ``90 Надувная горка``.
    """
    query = query.strip()
    if not query:
        return {"leads": [], "open_count": 0, "query": query}

    limit = max(1, min(limit, 50))
    matches: dict[int, tuple[int, dict[str, Any]]] = {}

    if query.isdigit():
        try:
            exact = await _request(
                "GET",
                f"/api/v4/leads/{int(query)}",
                params={"with": "contacts"},
            )
            if (
                exact
                and not exact.get("closed_at")
                and isinstance(exact.get("id"), int)
            ):
                matches[int(exact["id"])] = (-1, exact)
        except KommoAPIError as exc:
            if exc.status_code != 404:
                raise

    # Kommo's query behaviour can vary for numeric strings and partial names.
    # Scan the normalized open-lead titles as the source of truth for this menu.
    all_open = await get_all_open_leads()
    for lead in all_open.get("leads") or []:
        lead_id = lead.get("id")
        if not isinstance(lead_id, int):
            continue
        score = _lead_name_match_score(lead.get("name"), query)
        if score is not None:
            previous = matches.get(lead_id)
            if previous is None or score < previous[0]:
                matches[lead_id] = (score, lead)

    ordered = sorted(
        matches.values(),
        key=lambda item: (
            item[0],
            -(int(item[1].get("updated_at") or 0)),
            int(item[1].get("id") or 0),
        ),
    )
    leads = [item[1] for item in ordered[:limit]]
    return {
        "leads": leads,
        "open_count": len(leads),
        "query": query,
        "search_kind": "partial_title",
    }


async def add_text_note(
    lead_id: int, text: str, *, source: str = "Telegram"
) -> dict[str, Any]:
    """Add a manager-confirmed text note to an existing lead."""
    clean_text = text.strip()
    if not clean_text:
        raise ValueError("Текст примечания пустой.")
    details = await get_lead_details(lead_id)
    timestamp = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    note_text = (
        f"Примечание добавлено через {source}.\n"
        f"Время: {timestamp}\n\n"
        f"{clean_text}"
    )
    await add_common_note(lead_id, note_text)
    return {
        "lead_id": lead_id,
        "lead_name": details.get("name"),
        "url": details.get("url"),
    }


async def add_followup_note_from_analysis(
    *,
    lead_id: int,
    conversation_summary: str | None,
    recommended_next_step: str | None,
    missing_questions: list[str] | None,
    transcript: str | None,
    client_data: dict[str, Any] | None = None,
    lead_data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Add a reviewed second-call report to an existing Kommo lead without creating a new one."""
    details = await get_lead_details(lead_id)
    client_data = client_data or {}
    lead_data = lead_data or {}

    note_parts = [
        "Повторный разговор добавлен через Telegram после подтверждения менеджером.",
        f"Время: {datetime.now(tz=timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
    ]
    client_label = client_data.get("name") or client_data.get("company")
    if client_label:
        note_parts.append(f"Клиент/компания из разговора: {client_label}")
    if lead_data.get("product_requested"):
        note_parts.append(f"Обсуждавшийся запрос: {lead_data['product_requested']}")
    if lead_data.get("budget"):
        note_parts.append(f"Бюджет/ориентир: {lead_data['budget']}")
    if conversation_summary:
        note_parts.append(f"Краткое резюме: {conversation_summary}")
    if recommended_next_step:
        note_parts.append(f"Следующий шаг: {recommended_next_step}")
    if missing_questions:
        note_parts.append("Что ещё уточнить:\n- " + "\n- ".join(missing_questions[:20]))
    if transcript:
        note_parts.append(f"Транскрипт разговора:\n{transcript[:9000]}")

    await add_common_note(lead_id, "\n\n".join(note_parts))
    return {
        "lead_id": lead_id,
        "lead_name": details.get("name"),
        "url": details.get("url"),
    }


async def create_lead_task(
    *,
    lead_id: int,
    text: str,
    complete_till: int,
    responsible_user_id: int | None = None,
) -> dict[str, Any]:
    """Create a manager-confirmed Kommo task attached to a lead."""
    clean_text = text.strip()
    if not clean_text:
        raise ValueError("Текст задачи пустой.")
    if complete_till <= int(datetime.now(tz=timezone.utc).timestamp()):
        raise ValueError("Срок задачи должен быть в будущем.")

    details = await get_lead_details(lead_id)
    payload: dict[str, Any] = {
        "entity_id": lead_id,
        "entity_type": "leads",
        "complete_till": int(complete_till),
        "task_type_id": int(settings.kommo_default_task_type_id or 1),
        "text": clean_text[:1000],
    }
    assignee = responsible_user_id or details.get("responsible_user_id")
    if isinstance(assignee, int) and assignee > 0:
        payload["responsible_user_id"] = assignee

    data = await _request("POST", "/api/v4/tasks", json_body=[payload])
    tasks = ((data or {}).get("_embedded") or {}).get("tasks") or []
    created = tasks[0] if tasks else {}
    return {
        "task_id": created.get("id"),
        "lead_id": lead_id,
        "lead_name": details.get("name"),
        "complete_till": complete_till,
        "text": clean_text,
        "url": details.get("url"),
    }
