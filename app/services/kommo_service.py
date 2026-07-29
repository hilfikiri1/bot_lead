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
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

REQUEST_TIMEOUT = 20.0
PAGE_SIZE = 250
MAX_NOTE_CHARS = 14000
MAX_TRANSCRIPT_CHUNK = 8000


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


def _extract_embedded_items(data: Any, entity_key: str) -> list[dict[str, Any]]:
    """Parse Kommo responses that may be a wrapper dict or a bare list.

    ``/api/v4/leads/complex`` sometimes returns ``[{"id": ...}]`` instead of
    ``{"_embedded": {"leads": [...]}}``.
    """
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        embedded = data.get("_embedded")
        if isinstance(embedded, dict):
            items = embedded.get(entity_key) or []
            if isinstance(items, list):
                return [item for item in items if isinstance(item, dict)]
        if entity_key == "leads" and isinstance(data.get("id"), int):
            return [data]
        if entity_key == "tasks" and isinstance(data.get("id"), int):
            return [data]
    return []


def _chunk_text(text: str, limit: int) -> list[str]:
    clean = (text or "").strip()
    if not clean:
        return []
    if len(clean) <= limit:
        return [clean]
    chunks: list[str] = []
    start = 0
    while start < len(clean):
        chunks.append(clean[start : start + limit])
        start += limit
    return chunks


def _format_optional_lines(label: str, value: Any) -> str | None:
    clean = str(value or "").strip()
    if not clean:
        return None
    return f"{label}: {clean}"


def build_analysis_note_text(
    *,
    client_data: dict[str, Any],
    lead_data: dict[str, Any],
    conversation_summary: str | None,
    recommended_next_step: str | None,
    missing_questions: list[str] | None,
    confirmed_facts: list[str] | None = None,
    risks: list[str] | None = None,
    whatsapp_message: str | None = None,
) -> str:
    """Build the structured analysis note that should land in Kommo."""
    lines = ["📌 АНАЛИЗ РАЗГОВОРА ИЗ TELEGRAM", ""]
    lines.append("КЛИЕНТ")
    for label, key in (
        ("Имя", "name"),
        ("Компания", "company"),
        ("Телефон", "phone"),
        ("Email", "email"),
        ("Язык", "language"),
    ):
        row = _format_optional_lines(label, client_data.get(key))
        if row:
            lines.append(row)
    lines.append("")

    lines.append("ЗАПРОС")
    for label, key in (
        ("Товар или оборудование", "product_requested"),
        ("Количество", "quantity"),
        ("Бюджет", "budget"),
        ("Страна", "country"),
        ("Город", "city"),
        ("Условия поставки", "delivery_terms"),
        ("Сертификация", "certification"),
        ("Сроки", "timeline"),
    ):
        row = _format_optional_lines(label, lead_data.get(key))
        if row:
            lines.append(row)
    specifications = lead_data.get("specifications") or []
    if isinstance(specifications, list) and specifications:
        lines.append("Характеристики:")
        lines.extend(f"- {item}" for item in specifications[:20] if str(item).strip())
    lines.append("")

    if conversation_summary:
        lines.extend(["КРАТКОЕ РЕЗЮМЕ", conversation_summary.strip(), ""])
    if confirmed_facts:
        lines.append("ЧТО ПОДТВЕРЖДЕНО")
        lines.extend(f"- {item}" for item in confirmed_facts[:20] if str(item).strip())
        lines.append("")
    if missing_questions:
        lines.append("ЧТО НУЖНО УТОЧНИТЬ")
        lines.extend(f"- {item}" for item in missing_questions[:20] if str(item).strip())
        lines.append("")
    if risks:
        lines.append("РИСКИ")
        lines.extend(f"- {item}" for item in risks[:20] if str(item).strip())
        lines.append("")
    if recommended_next_step:
        lines.extend(["СЛЕДУЮЩИЙ ШАГ", recommended_next_step.strip(), ""])
    if whatsapp_message:
        lines.extend(["СООБЩЕНИЕ КЛИЕНТУ", whatsapp_message.strip(), ""])

    return "\n".join(lines).strip()


async def save_analysis_to_kommo_notes(
    lead_id: int,
    *,
    client_data: dict[str, Any],
    lead_data: dict[str, Any],
    conversation_summary: str | None,
    recommended_next_step: str | None,
    missing_questions: list[str] | None,
    transcript: str | None,
    confirmed_facts: list[str] | None = None,
    risks: list[str] | None = None,
    whatsapp_message: str | None = None,
) -> int:
    """Persist analysis and transcript chunks as Kommo notes."""
    notes_added = 0
    analysis_text = build_analysis_note_text(
        client_data=client_data,
        lead_data=lead_data,
        conversation_summary=conversation_summary,
        recommended_next_step=recommended_next_step,
        missing_questions=missing_questions,
        confirmed_facts=confirmed_facts,
        risks=risks,
        whatsapp_message=whatsapp_message,
    )
    if analysis_text:
        await add_common_note(lead_id, analysis_text[:MAX_NOTE_CHARS])
        notes_added += 1

    transcript_chunks = _chunk_text(transcript or "", MAX_TRANSCRIPT_CHUNK)
    for index, chunk in enumerate(transcript_chunks, start=1):
        prefix = (
            "ТРАНСКРИПТ РАЗГОВОРА"
            if len(transcript_chunks) == 1
            else f"ТРАНСКРИПТ РАЗГОВОРА ({index}/{len(transcript_chunks)})"
        )
        await add_common_note(lead_id, f"{prefix}\n\n{chunk}")
        notes_added += 1
    return notes_added


async def find_recent_lead_by_name(
    name: str,
    *,
    within_minutes: int = 60,
) -> dict[str, Any] | None:
    """Find a recently created Kommo lead with an exact title match."""
    trimmed = name.strip()
    if not trimmed:
        return None

    data = await _request(
        "GET",
        "/api/v4/leads",
        params={
            "query": trimmed,
            "limit": 50,
            "order[created_at]": "desc",
        },
    )
    leads = _extract_embedded_items(data, "leads")
    cutoff = datetime.now(tz=timezone.utc) - timedelta(minutes=within_minutes)

    for lead in leads:
        if (lead.get("name") or "").strip() != trimmed:
            continue
        created_at = lead.get("created_at")
        if isinstance(created_at, int):
            created_dt = datetime.fromtimestamp(created_at, tz=timezone.utc)
            if created_dt < cutoff:
                continue
        return lead
    return None


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


def configured_menu_pipeline_id() -> int | None:
    """Pipeline used for open-deal list and search in Telegram."""
    return settings.kommo_menu_pipeline_id or settings.kommo_default_pipeline_id


def _lead_belongs_to_pipeline(lead: dict[str, Any], pipeline_id: int | None) -> bool:
    if pipeline_id is None:
        return True
    return lead.get("pipeline_id") == pipeline_id


async def get_pipeline_statuses(pipeline_id: int) -> list[dict[str, Any]]:
    """Return stages for one Kommo pipeline."""
    data = await _request("GET", "/api/v4/leads/pipelines")
    pipelines = ((data or {}).get("_embedded") or {}).get("pipelines") or []
    for pipeline in pipelines:
        if pipeline.get("id") != pipeline_id:
            continue
        statuses = (pipeline.get("_embedded") or {}).get("statuses") or []
        return [
            {
                "id": status.get("id"),
                "name": status.get("name") or f"Этап {status.get('id')}",
                "sort": status.get("sort", 0),
            }
            for status in statuses
            if isinstance(status.get("id"), int)
        ]
    return []


async def update_kommo_lead(
    lead_id: int,
    *,
    name: str | None = None,
    price: int | None = None,
    status_id: int | None = None,
) -> dict[str, Any]:
    """Update an existing Kommo lead after manager confirmation."""
    if lead_id <= 0:
        raise ValueError("Некорректный ID сделки.")

    payload: dict[str, Any] = {"id": lead_id}
    if name is not None:
        clean_name = name.strip()
        if not clean_name:
            raise ValueError("Название сделки не может быть пустым.")
        payload["name"] = clean_name[:255]
    if price is not None:
        payload["price"] = max(0, int(price))
    if status_id is not None:
        payload["status_id"] = int(status_id)

    if len(payload) == 1:
        raise ValueError("Нет полей для обновления.")

    # Re-check the current assignee immediately before the mutating request.
    # A Manager may have prepared the preview while the deal was assigned to
    # them and then lose access before pressing the confirmation button.
    await get_lead_details(lead_id)
    data = await _request("PATCH", "/api/v4/leads", json_body=[payload])
    updated_items = _extract_embedded_items(data, "leads")
    updated = updated_items[0] if updated_items else {"id": lead_id, **payload}
    details = await get_lead_details(lead_id)
    return {
        "lead_id": lead_id,
        "lead_name": updated.get("name") or details.get("name"),
        "price": updated.get("price", details.get("price")),
        "status_name": details.get("status_name"),
        "pipeline_name": details.get("pipeline_name"),
        "url": details.get("url"),
    }


async def get_all_open_leads(
    max_pages: int | None = None,
    *,
    pipeline_id: int | None = None,
    allow_menu_fallback: bool = True,
) -> dict[str, Any]:
    """
    Fetch all leads page by page and keep only leads without closed_at.

    Kommo returns at most 250 entities per page. A configurable page cap protects
    the bot from accidental unbounded requests on very large accounts.
    """
    page_cap = max_pages or settings.kommo_open_leads_max_pages
    page_cap = max(1, min(page_cap, 100))
    selected_pipeline = pipeline_id
    if selected_pipeline is None and allow_menu_fallback:
        selected_pipeline = configured_menu_pipeline_id()

    open_leads: list[dict[str, Any]] = []
    scanned = 0
    truncated = False

    for page in range(1, page_cap + 1):
        params: dict[str, Any] = {
            "page": page,
            "limit": PAGE_SIZE,
            "order[updated_at]": "desc",
        }
        if selected_pipeline is not None:
            params["filter[pipeline_id]"] = selected_pipeline

        data = await _request("GET", "/api/v4/leads", params=params)
        if data is None:
            break

        page_leads = (data.get("_embedded") or {}).get("leads") or []
        scanned += len(page_leads)
        for lead in page_leads:
            if lead.get("closed_at"):
                continue
            if not _lead_belongs_to_pipeline(lead, selected_pipeline):
                continue
            from app.services import identity_service

            if not identity_service.current_user_can_access_responsible_id(
                lead.get("responsible_user_id")
            ):
                continue
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
                "created_at": lead.get("created_at"),
                "closest_task_at": lead.get("closest_task_at"),
                "url": f"{_base_url()}/leads/detail/{lead.get('id')}",
            }
        )

    normalized.sort(key=lambda item: item.get("updated_at") or 0, reverse=True)
    pipeline_label = None
    if selected_pipeline is not None:
        pipeline_label = pipeline_names.get(
            selected_pipeline, f"Воронка {selected_pipeline}"
        )
    return {
        "leads": normalized,
        "open_count": len(normalized),
        "scanned_count": scanned,
        "truncated": truncated,
        "page_cap": page_cap,
        "pipeline_id": selected_pipeline,
        "pipeline_name": pipeline_label,
    }


async def get_all_leads_for_status_sync(
    max_pages: int | None = None,
    *,
    pipeline_id: int | None = None,
) -> dict[str, Any]:
    """Return open and closed Kommo leads for spreadsheet status comparison."""
    page_cap = max_pages or settings.kommo_open_leads_max_pages
    page_cap = max(1, min(page_cap, 100))
    selected_pipeline = pipeline_id
    if selected_pipeline is None:
        selected_pipeline = (
            settings.lead_status_sync_pipeline_id or configured_menu_pipeline_id()
        )

    leads: list[dict[str, Any]] = []
    scanned = 0
    truncated = False
    for page in range(1, page_cap + 1):
        params: dict[str, Any] = {
            "page": page,
            "limit": PAGE_SIZE,
            "order[updated_at]": "desc",
        }
        if selected_pipeline is not None:
            params["filter[pipeline_id]"] = selected_pipeline

        data = await _request("GET", "/api/v4/leads", params=params)
        if data is None:
            break
        page_leads = (data.get("_embedded") or {}).get("leads") or []
        scanned += len(page_leads)
        for lead in page_leads:
            if not _lead_belongs_to_pipeline(lead, selected_pipeline):
                continue
            leads.append(lead)

        if len(page_leads) < PAGE_SIZE:
            break
        if page == page_cap:
            truncated = True

    pipeline_names, status_names = await get_pipeline_index()
    normalized: list[dict[str, Any]] = []
    for lead in leads:
        current_pipeline_id = lead.get("pipeline_id")
        status_id = lead.get("status_id")
        normalized.append(
            {
                "id": lead.get("id"),
                "name": lead.get("name") or "Без названия",
                "pipeline_id": current_pipeline_id,
                "pipeline_name": pipeline_names.get(
                    current_pipeline_id, f"Воронка {current_pipeline_id}"
                ),
                "status_id": status_id,
                "status_name": status_names.get(
                    (current_pipeline_id, status_id), f"Этап {status_id}"
                ),
                "updated_at": lead.get("updated_at"),
                "created_at": lead.get("created_at"),
                "closed_at": lead.get("closed_at"),
                "url": f"{_base_url()}/leads/detail/{lead.get('id')}",
            }
        )

    normalized.sort(key=lambda item: item.get("updated_at") or 0, reverse=True)
    pipeline_label = (
        pipeline_names.get(selected_pipeline, f"Воронка {selected_pipeline}")
        if selected_pipeline is not None
        else None
    )
    return {
        "leads": normalized,
        "count": len(normalized),
        "scanned_count": scanned,
        "truncated": truncated,
        "page_cap": page_cap,
        "pipeline_id": selected_pipeline,
        "pipeline_name": pipeline_label,
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
    confirmed_facts: list[str] | None = None,
    risks: list[str] | None = None,
    whatsapp_message: str | None = None,
) -> dict[str, Any]:
    """Create one Kommo lead after explicit human approval in Telegram."""
    lead_title = _lead_title(
        client_data,
        lead_data,
        lead_name_override=lead_name_override,
    )
    lead_payload: dict[str, Any] = {"name": lead_title}
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

    created_leads = _extract_embedded_items(data, "leads")
    if not created_leads:
        recovered = await find_recent_lead_by_name(lead_title)
        if recovered:
            logger.warning(
                "Kommo response had no embedded lead, but a recent lead with the same "
                "title was found: %s",
                recovered.get("id"),
            )
            created_leads = [recovered]
        else:
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

    notes_added = await save_analysis_to_kommo_notes(
        lead_id,
        client_data=client_data,
        lead_data=lead_data,
        conversation_summary=conversation_summary,
        recommended_next_step=recommended_next_step,
        missing_questions=missing_questions,
        transcript=transcript,
        confirmed_facts=confirmed_facts,
        risks=risks,
        whatsapp_message=whatsapp_message,
    )

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
        "note_saved": notes_added > 0,
        "notes_added": notes_added,
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

    from app.services import identity_service

    identity_service.assert_current_user_can_access_lead(lead)

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


async def get_open_lead_tasks(lead_id: int, limit: int = 20) -> list[dict[str, Any]]:
    """Return incomplete Kommo tasks for the project card."""
    data = await _request(
        "GET",
        "/api/v4/tasks",
        params={
            "filter[entity_type]": "leads",
            "filter[entity_id]": int(lead_id),
            "filter[is_completed]": 0,
            "limit": max(1, min(limit, 50)),
            "order[complete_till]": "asc",
        },
    )
    tasks = ((data or {}).get("_embedded") or {}).get("tasks") or []
    return [
        {
            "id": task.get("id"),
            "text": task.get("text"),
            "complete_till": task.get("complete_till"),
            "responsible_user_id": task.get("responsible_user_id"),
            "task_type_id": task.get("task_type_id"),
            "is_completed": bool(task.get("is_completed")),
            "source": "kommo",
        }
        for task in tasks
        if not task.get("is_completed")
    ]


async def get_user_summary(user_id: int | None) -> dict[str, Any] | None:
    if not user_id:
        return None
    try:
        user = await _request("GET", f"/api/v4/users/{int(user_id)}")
    except KommoAPIError as exc:
        if exc.status_code == 404:
            return {"id": int(user_id), "name": f"Kommo user {user_id}"}
        raise
    return {
        "id": user.get("id") or int(user_id),
        "name": user.get("name") or f"Kommo user {user_id}",
        "email": user.get("email"),
        "language": user.get("lang"),
    }


async def search_projects(query: str, limit: int = 8) -> dict[str, Any]:
    """Search projects by lead/product title and linked contact/company/phone."""
    clean_query = " ".join(str(query or "").strip().split())
    if not clean_query:
        return {"leads": [], "query": clean_query}
    limit = max(1, min(limit, 20))
    title_result = await search_open_leads(clean_query, limit=limit)
    matches: dict[int, dict[str, Any]] = {
        int(item["id"]): item
        for item in title_result.get("leads") or []
        if isinstance(item.get("id"), int)
    }
    if not matches:
        try:
            data = await _request(
                "GET",
                "/api/v4/contacts",
                params={
                    "query": clean_query,
                    "with": "leads",
                    "limit": min(50, limit * 5),
                },
            )
            contacts = ((data or {}).get("_embedded") or {}).get("contacts") or []
            for contact in contacts:
                linked = ((contact.get("_embedded") or {}).get("leads") or [])
                for ref in linked:
                    lead_id = ref.get("id")
                    if not isinstance(lead_id, int) or lead_id in matches:
                        continue
                    try:
                        details = await get_lead_details(lead_id)
                    except (KommoAPIError, PermissionError):
                        continue
                    if details.get("closed_at"):
                        continue
                    matches[lead_id] = details
                    if len(matches) >= limit:
                        break
                if len(matches) >= limit:
                    break
        except KommoAPIError as exc:
            logger.info("Contact project search fallback skipped: %s", exc)

    if not matches:
        try:
            data = await _request(
                "GET",
                "/api/v4/companies",
                params={
                    "query": clean_query,
                    "with": "leads",
                    "limit": min(50, limit * 5),
                },
            )
            companies = ((data or {}).get("_embedded") or {}).get("companies") or []
            for company in companies:
                linked = ((company.get("_embedded") or {}).get("leads") or [])
                for ref in linked:
                    lead_id = ref.get("id")
                    if not isinstance(lead_id, int) or lead_id in matches:
                        continue
                    try:
                        details = await get_lead_details(lead_id)
                    except (KommoAPIError, PermissionError):
                        continue
                    if details.get("closed_at"):
                        continue
                    details["company_name"] = company.get("name")
                    matches[lead_id] = details
                    if len(matches) >= limit:
                        break
                if len(matches) >= limit:
                    break
        except KommoAPIError as exc:
            logger.info("Company project search fallback skipped: %s", exc)

    leads = list(matches.values())[:limit]
    leads.sort(key=lambda item: int(item.get("updated_at") or 0), reverse=True)
    return {
        "leads": leads,
        "open_count": len(leads),
        "query": clean_query,
        "search_kind": "project_title_contact_company_phone",
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
    selected_pipeline = configured_menu_pipeline_id()

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
                and _lead_belongs_to_pipeline(exact, selected_pipeline)
            ):
                from app.services import identity_service

                if identity_service.current_user_can_access_responsible_id(
                    exact.get("responsible_user_id")
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
    pipeline_label = all_open.get("pipeline_name")
    if pipeline_label is None and selected_pipeline is not None:
        pipeline_label = f"Воронка {selected_pipeline}"
    return {
        "leads": leads,
        "open_count": len(leads),
        "query": query,
        "search_kind": "partial_title",
        "pipeline_id": selected_pipeline,
        "pipeline_name": pipeline_label,
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
    tasks = _extract_embedded_items(data, "tasks")
    created = tasks[0] if tasks else {}
    return {
        "task_id": created.get("id"),
        "lead_id": lead_id,
        "lead_name": details.get("name"),
        "complete_till": complete_till,
        "text": clean_text,
        "url": details.get("url"),
    }


_UNREVIEWED_NAME_RE = re.compile(r"^\d+\s*-\s*.+$")

INCOMING_LEADS_STATUS_ALIASES = frozenset(
    {
        "incoming leads",
        "incoming lead",
        "incoming",
        "входящие лиды",
        "входящие",
        "неразобранные",
        "неразобранное",
        "незапланированные",
        "незапланированное",
        "не запланированные",
        "не запланированное",
        "unplanned",
        "unsorted",
    }
)


def lead_has_internal_id(name: Any) -> bool:
    return bool(_UNREVIEWED_NAME_RE.match(str(name or "").strip()))


def configured_unreviewed_pipeline_id() -> int | None:
    return settings.kommo_unreviewed_pipeline_id


def configured_unreviewed_status_id() -> int | None:
    return settings.kommo_unreviewed_status_id


def _normalize_status_name(value: str) -> str:
    return " ".join((value or "").casefold().split())


def _incoming_status_matches(status_name: str, configured_name: str) -> bool:
    norm = _normalize_status_name(status_name)
    configured = _normalize_status_name(configured_name)
    if norm == configured:
        return True
    if configured == _normalize_status_name("Incoming leads"):
        return norm in INCOMING_LEADS_STATUS_ALIASES
    return False


async def resolve_unreviewed_status_scope() -> dict[str, Any]:
    """Resolve Kommo filters for the Incoming leads unreviewed list."""
    pipeline_id = configured_unreviewed_pipeline_id()
    explicit_status_id = configured_unreviewed_status_id()
    configured_name = (settings.kommo_unreviewed_status_name or "Incoming leads").strip()
    pipeline_names, status_names = await get_pipeline_index()

    if explicit_status_id is not None:
        status_pairs = {
            (pid, sid)
            for (pid, sid) in status_names
            if sid == explicit_status_id and (pipeline_id is None or pid == pipeline_id)
        }
        if not status_pairs and pipeline_id is not None:
            status_pairs = {(pipeline_id, explicit_status_id)}
        if not status_pairs:
            raise KommoAPIError(
                f"Этап ID {explicit_status_id} не найден в Kommo. "
                "Проверьте KOMMO_UNREVIEWED_STATUS_ID и KOMMO_UNREVIEWED_PIPELINE_ID."
            )
        labels = [
            f"{pipeline_names.get(pid, f'Воронка {pid}')} → {status_names.get((pid, sid), configured_name)}"
            for pid, sid in sorted(status_pairs)
        ]
        return {
            "pipeline_id": pipeline_id,
            "pipeline_ids": {pid for pid, _ in status_pairs},
            "status_ids": {sid for _, sid in status_pairs},
            "status_pairs": status_pairs,
            "status_label": labels[0] if len(labels) == 1 else configured_name,
        }

    status_pairs: set[tuple[int, int]] = set()
    matched_labels: list[str] = []
    for (pid, sid), status_name in status_names.items():
        if pipeline_id is not None and pid != pipeline_id:
            continue
        if not _incoming_status_matches(status_name, configured_name):
            continue
        status_pairs.add((pid, sid))
        pipeline_label = pipeline_names.get(pid, f"Воронка {pid}")
        matched_labels.append(f"{pipeline_label} → {status_name}")

    if not status_pairs:
        available = sorted(
            {
                f"{pipeline_names.get(pid, pid)} → {name}"
                for (pid, _), name in status_names.items()
                if pipeline_id is None or pid == pipeline_id
            }
        )
        preview = "\n".join(f"• {item}" for item in available[:12])
        raise KommoAPIError(
            f'Этап "{configured_name}" не найден в Kommo. '
            "Задайте KOMMO_UNREVIEWED_STATUS_ID или KOMMO_UNREVIEWED_PIPELINE_ID.\n\n"
            f"Доступные этапы:\n{preview}"
        )

    status_label = (
        matched_labels[0]
        if len(matched_labels) == 1
        else f"{configured_name} ({len(matched_labels)} этапов)"
    )
    return {
        "pipeline_id": pipeline_id,
        "pipeline_ids": {pid for pid, _ in status_pairs},
        "status_ids": {sid for _, sid in status_pairs},
        "status_pairs": status_pairs,
        "status_label": status_label,
    }


async def get_all_unreviewed_leads(
    max_pages: int | None = None,
) -> dict[str, Any]:
    """Return Kommo unreviewed leads from the unsorted inbox or pipeline stage."""
    configured_pipeline = configured_unreviewed_pipeline_id()

    if settings.kommo_unreviewed_use_unsorted:
        result = await get_all_unsorted_leads(
            max_pages=max_pages,
            pipeline_id=configured_pipeline,
        )
        pipeline_name = result.get("pipeline_name")
        if configured_pipeline and not pipeline_name:
            pipeline_names, _ = await get_pipeline_index()
            pipeline_name = pipeline_names.get(configured_pipeline)
        return {
            **result,
            "pipeline_id": configured_pipeline,
            "pipeline_name": pipeline_name,
            "status_label": "Неразобранное",
            "source": "unsorted",
        }

    scope = await resolve_unreviewed_status_scope()
    status_pairs = scope["status_pairs"]
    pipeline_ids = scope["pipeline_ids"]

    if configured_pipeline is not None:
        fetch_pipeline = configured_pipeline
    elif len(pipeline_ids) == 1:
        fetch_pipeline = next(iter(pipeline_ids))
    else:
        fetch_pipeline = None

    result = await get_all_open_leads(
        max_pages=max_pages,
        pipeline_id=fetch_pipeline,
        allow_menu_fallback=False,
    )
    filtered: list[dict[str, Any]] = []
    for lead in result.get("leads") or []:
        if settings.kommo_unreviewed_hide_numbered and lead_has_internal_id(
            lead.get("name")
        ):
            continue
        pair = (lead.get("pipeline_id"), lead.get("status_id"))
        if pair not in status_pairs:
            continue
        filtered.append(lead)

    pipeline_name = None
    if len(pipeline_ids) == 1:
        sample = next(iter(filtered), None)
        pipeline_name = (sample or {}).get("pipeline_name")
        if not pipeline_name:
            pipeline_names, _ = await get_pipeline_index()
            pipeline_name = pipeline_names.get(next(iter(pipeline_ids)))

    return {
        **result,
        "leads": filtered,
        "open_count": len(filtered),
        "pipeline_id": fetch_pipeline,
        "pipeline_name": pipeline_name,
        "status_id": next(iter(scope["status_ids"])) if len(scope["status_ids"]) == 1 else None,
        "status_ids": sorted(scope["status_ids"]),
        "status_label": scope["status_label"],
        "source": "pipeline",
    }


def _lead_id_from_unsorted(item: dict[str, Any]) -> int | None:
    leads = (item.get("_embedded") or {}).get("leads") or []
    if not leads:
        return None
    lead_id = leads[0].get("id")
    return lead_id if isinstance(lead_id, int) else None


def _contact_id_from_unsorted(item: dict[str, Any]) -> int | None:
    contacts = (item.get("_embedded") or {}).get("contacts") or []
    if not contacts:
        return None
    contact_id = contacts[0].get("id")
    return contact_id if isinstance(contact_id, int) else None


def _unsorted_display_name(item: dict[str, Any], *, lead_name: str | None = None) -> str:
    if lead_name and str(lead_name).strip():
        return str(lead_name).strip()
    metadata = item.get("metadata") or {}
    for key in ("form_name", "source_name", "subject", "title"):
        value = metadata.get(key) or item.get(key)
        if value:
            return str(value).strip()
    source = item.get("source_name")
    if source:
        category = str(item.get("category") or "").strip()
        if category:
            return f"{source} ({category})"
        return str(source).strip()
    uid = str(item.get("uid") or "").strip()
    if uid:
        return f"Заявка {uid[:8]}"
    return "Без названия"


async def get_all_unsorted_leads(
    *,
    pipeline_id: int | None = None,
    max_pages: int | None = None,
) -> dict[str, Any]:
    """Fetch Kommo incoming/unsorted leads (Неразобранное inbox)."""
    page_cap = max(1, min(max_pages or settings.kommo_open_leads_max_pages, 20))
    raw_items: list[dict[str, Any]] = []

    for page in range(1, page_cap + 1):
        params: dict[str, Any] = {
            "page": page,
            "limit": PAGE_SIZE,
            "order[created_at]": "desc",
        }
        if pipeline_id is not None:
            params["filter[pipeline_id]"] = pipeline_id
        try:
            data = await _request("GET", "/api/v4/leads/unsorted", params=params)
        except KommoAPIError as exc:
            if exc.status_code in {401, 403}:
                raise KommoAPIError(
                    "Нет доступа к неразобранным сделкам в Kommo. "
                    "Проверьте права интеграции на чтение входящих заявок."
                ) from exc
            raise
        if not data:
            break
        batch = (data.get("_embedded") or {}).get("unsorted") or []
        if not batch:
            break
        raw_items.extend(batch)
        if len(batch) < PAGE_SIZE:
            break

    pipeline_names, _ = await get_pipeline_index()
    normalized: list[dict[str, Any]] = []
    for item in raw_items:
        lead_id = _lead_id_from_unsorted(item)
        if lead_id is None:
            continue
        item_pipeline_id = item.get("pipeline_id")
        if pipeline_id is not None and item_pipeline_id != pipeline_id:
            continue
        lead_name: str | None = None
        embedded_leads = (item.get("_embedded") or {}).get("leads") or []
        if embedded_leads:
            lead_name = embedded_leads[0].get("name")
        display_name = _unsorted_display_name(item, lead_name=lead_name)
        if settings.kommo_unreviewed_hide_numbered and lead_has_internal_id(display_name):
            continue
        normalized.append(
            {
                "id": lead_id,
                "unsorted_uid": item.get("uid"),
                "name": display_name,
                "pipeline_id": item_pipeline_id,
                "pipeline_name": pipeline_names.get(
                    item_pipeline_id, f"Воронка {item_pipeline_id}"
                ),
                "created_at": item.get("created_at"),
                "source_name": item.get("source_name"),
                "category": item.get("category"),
                "metadata": item.get("metadata") or {},
                "contact_id": _contact_id_from_unsorted(item),
                "url": f"{_base_url()}/leads/detail/{lead_id}",
                "is_unsorted": True,
            }
        )

    pipeline_name = pipeline_names.get(pipeline_id) if pipeline_id else None
    return {
        "leads": normalized,
        "open_count": len(normalized),
        "pipeline_id": pipeline_id,
        "pipeline_name": pipeline_name,
        "scanned_count": len(raw_items),
    }


async def enrich_leads_with_contacts(
    leads: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    enriched: list[dict[str, Any]] = []
    for lead in leads:
        lead_id = lead.get("id")
        if not isinstance(lead_id, int):
            enriched.append(lead)
            continue
        try:
            details = await get_lead_details(lead_id)
        except Exception as exc:
            logger.warning("Could not enrich lead %s: %s", lead_id, exc)
            contact_id = lead.get("contact_id")
            if isinstance(contact_id, int):
                try:
                    contact = await _request("GET", f"/api/v4/contacts/{contact_id}") or {}
                    phones, emails = _contact_channels(contact)
                    enriched.append(
                        {
                            **lead,
                            "contact_name": contact.get("name"),
                            "phones": phones,
                            "emails": emails,
                        }
                    )
                    continue
                except Exception as contact_exc:
                    logger.warning(
                        "Could not enrich unsorted contact %s: %s",
                        contact_id,
                        contact_exc,
                    )
            enriched.append(lead)
            continue
        contacts = details.get("contacts") or []
        contact = contacts[0] if contacts else {}
        enriched.append(
            {
                **lead,
                "name": details.get("name") or lead.get("name"),
                "contact_name": contact.get("name"),
                "phones": contact.get("phones") or [],
                "emails": contact.get("emails") or [],
                "created_at": details.get("created_at") or lead.get("created_at"),
                "url": details.get("url") or lead.get("url"),
            }
        )
    return enriched


async def get_unreviewed_leads_page(
    page: int = 1, page_size: int | None = None
) -> dict[str, Any]:
    """Return one Telegram page of unreviewed Kommo leads."""
    page_size = page_size or settings.kommo_unreviewed_page_size
    page_size = max(1, min(page_size, 10))
    result = await get_all_unreviewed_leads()
    leads = result.get("leads") or []
    total = len(leads)
    total_pages = max(1, (total + page_size - 1) // page_size)
    page = max(1, min(page, total_pages))
    start = (page - 1) * page_size
    page_leads = leads[start : start + page_size]
    page_leads = await enrich_leads_with_contacts(page_leads)
    return {
        **result,
        "leads": page_leads,
        "page": page,
        "page_size": page_size,
        "total_pages": total_pages,
        "open_count": total,
    }
