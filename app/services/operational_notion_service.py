"""Modern Notion Data Sources integration for the B&BS operational system.

The legacy ``notion_service`` remains untouched because it is used by the
existing call-analysis workflow.  This module works only with the new Russian
operational databases and the Notion 2025 Data Sources API.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

import httpx

from app.config import get_settings

settings = get_settings()
NOTION_API = "https://api.notion.com/v1"
NOTION_VERSION = "2025-09-03"
REQUEST_TIMEOUT = 30.0

PROJECT_SCHEMA: dict[str, str] = {
    "Название": "title",
    "Тип записи": "select",
    "Kommo ID": "number",
    "Kommo URL": "url",
    "Статус": "select",
    "Приоритет": "select",
    "Следующий шаг": "rich_text",
    "Дата следующего действия": "date",
    "Последний контакт": "date",
    "Родительский клиент": "relation",
}

TASK_SCHEMA: dict[str, str] = {
    "Задача": "title",
    "Проект": "relation",
    "Kommo ID": "number",
    "Тип": "select",
    "Статус": "select",
    "Приоритет": "select",
    "Срок": "date",
    "Следующий шаг": "rich_text",
    "Результат": "rich_text",
    "Источник": "select",
    "External ID": "rich_text",
    "Sync status": "select",
    "Last error": "rich_text",
    "Обновить Kommo": "checkbox",
}

OPTIONAL_SCHEMAS: tuple[tuple[str, str, dict[str, str]], ...] = (
    (
        "Коммерческие предложения",
        "notion_offers_data_source_id",
        {
            "Название": "title",
            "Статус": "select",
            "Версия": "number",
            "Дата подготовки": "date",
            "Kommo ID": "number",
            "Проект": "relation",
            "Черновик текста": "rich_text",
        },
    ),
    (
        "Каталоги и прайсы",
        "notion_catalogs_data_source_id",
        {
            "Название": "title",
            "Тип": "select",
            "Статус": "select",
            "Комментарий": "rich_text",
            "Kommo ID": "number",
            "Проект": "relation",
            "Черновик текста": "rich_text",
        },
    ),
    (
        "Переписка и касания",
        "notion_communications_data_source_id",
        {
            "Название": "title",
            "Канал": "select",
            "Тип": "select",
            "Статус": "select",
            "Краткое содержание": "rich_text",
            "Kommo ID": "number",
            "Проект": "relation",
            "Полный текст": "rich_text",
        },
    ),
)


class OperationalNotionError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


def _headers() -> dict[str, str]:
    token = settings.notion_api_token.strip()
    if not token:
        raise OperationalNotionError("NOTION_API_TOKEN не настроен.")
    return {
        "Authorization": f"Bearer {token}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


async def _request(method: str, path: str, **kwargs: Any) -> dict[str, Any]:
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
                response = await client.request(
                    method,
                    f"{NOTION_API}{path}",
                    headers=_headers(),
                    **kwargs,
                )
            if 200 <= response.status_code < 300:
                return response.json() if response.content else {}
            try:
                payload = response.json()
                detail = payload.get("message") or response.text
            except Exception:
                detail = response.text
            error = OperationalNotionError(
                f"Notion HTTP {response.status_code}: {str(detail)[:500]}",
                status_code=response.status_code,
            )
            if response.status_code not in {429, 500, 502, 503, 504}:
                raise error
            last_error = error
        except OperationalNotionError:
            raise
        except (httpx.TimeoutException, httpx.ConnectError) as exc:
            last_error = exc
        if attempt < 2:
            await asyncio.sleep(1.5 * (attempt + 1))
    raise OperationalNotionError(f"Notion request failed: {last_error}")


def _data_source_id(value: str) -> str:
    return (value or "").removeprefix("collection://").strip()


def _page_id(value: str) -> str:
    return (value or "").replace("-", "").strip()


def notion_page_url(page_id: str) -> str:
    return f"https://www.notion.so/{_page_id(page_id)}"


def _actual_property_type(prop: dict[str, Any]) -> str:
    value = str(prop.get("type") or "")
    return "rich_text" if value == "text" else value


def _rich_text(value: str | None, *, max_chars: int = 19_000) -> dict[str, Any]:
    text = str(value or "")[:max_chars]
    chunks = [text[index : index + 1900] for index in range(0, len(text), 1900)]
    return {
        "rich_text": [
            {"type": "text", "text": {"content": chunk}}
            for chunk in chunks[:10]
        ]
    }


def _title(value: str | None) -> dict[str, Any]:
    return {
        "title": [
            {
                "type": "text",
                "text": {"content": str(value or "Без названия")[:1900]},
            }
        ]
    }


async def retrieve_data_source(data_source_id: str) -> dict[str, Any]:
    return await _request("GET", f"/data_sources/{_data_source_id(data_source_id)}")


async def _validate_one(
    label: str,
    source_id: str,
    expected: dict[str, str],
    *,
    required: bool,
) -> dict[str, Any]:
    if not source_id.strip():
        return {
            "name": label,
            "ok": not required,
            "required": required,
            "errors": ["Data source ID is empty"] if required else [],
            "warnings": ["Optional database is not configured"] if not required else [],
        }
    try:
        source = await retrieve_data_source(source_id)
        properties = source.get("properties") or {}
        errors: list[str] = []
        for name, expected_type in expected.items():
            prop = properties.get(name)
            if not prop:
                errors.append(f"missing property: {name}")
                continue
            actual = _actual_property_type(prop)
            if actual != expected_type:
                errors.append(f"{name}: expected {expected_type}, got {actual}")
        return {
            "name": label,
            "ok": not errors,
            "required": required,
            "errors": errors,
            "warnings": [],
        }
    except Exception as exc:
        return {
            "name": label,
            "ok": False,
            "required": required,
            "errors": [str(exc)],
            "warnings": [],
        }


async def validate_schema(include_optional: bool = True) -> dict[str, Any]:
    checks = [
        await _validate_one(
            "Клиенты и проекты",
            settings.notion_projects_data_source_id,
            PROJECT_SCHEMA,
            required=True,
        ),
        await _validate_one(
            "Задачи дня",
            settings.notion_tasks_data_source_id,
            TASK_SCHEMA,
            required=True,
        ),
    ]
    if include_optional:
        for label, attr, expected in OPTIONAL_SCHEMAS:
            checks.append(
                await _validate_one(
                    label,
                    str(getattr(settings, attr, "") or ""),
                    expected,
                    required=False,
                )
            )
    return {
        "ok": all(item["ok"] for item in checks if item.get("required")),
        "checks": checks,
    }


def format_schema_report(result: dict[str, Any]) -> str:
    lines = ["<b>🔌 B&BS Operational Notion</b>", ""]
    for check in result.get("checks") or []:
        mark = "✅" if check.get("ok") else "❌"
        lines.append(f"{mark} <b>{check.get('name')}</b>")
        for error in check.get("errors") or []:
            lines.append(f"   • {str(error)[:300]}")
        for warning in check.get("warnings") or []:
            lines.append(f"   • {str(warning)[:300]}")
    lines.extend(
        [
            "",
            "Итог: " + ("✅ готово к работе" if result.get("ok") else "❌ требуется исправление"),
            "",
            "Интеграция должна быть подключена ко всей странице "
            "<b>B&BS — Операционная система</b> и ко всем связанным базам.",
        ]
    )
    return "\n".join(lines)


async def query_by_number(
    data_source_id: str,
    property_name: str,
    value: int,
) -> list[dict[str, Any]]:
    payload = {
        "filter": {"property": property_name, "number": {"equals": value}},
        "page_size": 10,
    }
    data = await _request(
        "POST",
        f"/data_sources/{_data_source_id(data_source_id)}/query",
        json=payload,
    )
    return data.get("results") or []


async def query_by_text(
    data_source_id: str,
    property_name: str,
    value: str,
) -> list[dict[str, Any]]:
    payload = {
        "filter": {"property": property_name, "rich_text": {"equals": value}},
        "page_size": 10,
    }
    data = await _request(
        "POST",
        f"/data_sources/{_data_source_id(data_source_id)}/query",
        json=payload,
    )
    return data.get("results") or []


async def upsert_project_from_kommo(
    lead: dict[str, Any],
    *,
    priority: str | None = None,
    next_step: str | None = None,
    next_action_at: datetime | None = None,
) -> dict[str, Any]:
    kommo_id = int(lead["id"])
    matches = await query_by_number(
        settings.notion_projects_data_source_id,
        "Kommo ID",
        kommo_id,
    )
    properties: dict[str, Any] = {
        "Название": _title(str(lead.get("name") or f"Kommo {kommo_id}")),
        "Тип записи": {"select": {"name": "Проект"}},
        "Kommo ID": {"number": kommo_id},
        "Kommo URL": {"url": lead.get("url") or lead.get("kommo_url")},
        "Статус": {"select": {"name": "В работе"}},
    }
    if priority:
        properties["Приоритет"] = {"select": {"name": priority}}
    if next_step:
        properties["Следующий шаг"] = _rich_text(next_step)
    if next_action_at:
        properties["Дата следующего действия"] = {
            "date": {"start": next_action_at.isoformat()}
        }
    if lead.get("updated_at"):
        properties["Последний контакт"] = {
            "date": {
                "start": datetime.fromtimestamp(
                    int(lead["updated_at"]), tz=timezone.utc
                ).isoformat()
            }
        }
    if matches:
        page_id = matches[0]["id"]
        data = await _request(
            "PATCH",
            f"/pages/{page_id}",
            json={"properties": properties},
        )
        return {
            "id": page_id,
            "url": data.get("url") or notion_page_url(page_id),
            "created": False,
        }
    data = await _request(
        "POST",
        "/pages",
        json={
            "parent": {
                "type": "data_source_id",
                "data_source_id": _data_source_id(
                    settings.notion_projects_data_source_id
                ),
            },
            "properties": properties,
        },
    )
    return {
        "id": data["id"],
        "url": data.get("url") or notion_page_url(data["id"]),
        "created": True,
    }


async def create_task(
    *,
    title: str,
    lead_id: int,
    project_page_id: str | None,
    priority: str,
    task_type: str,
    due_at: datetime | None,
    next_step: str,
    source: str,
    external_id: str,
    result: str | None = None,
) -> dict[str, Any]:
    existing = await query_by_text(
        settings.notion_tasks_data_source_id,
        "External ID",
        external_id,
    )
    if existing:
        page_id = existing[0]["id"]
        return {
            "id": page_id,
            "url": existing[0].get("url") or notion_page_url(page_id),
            "created": False,
            "external_id": external_id,
        }
    properties: dict[str, Any] = {
        "Задача": _title(title),
        "Kommo ID": {"number": int(lead_id)},
        "Тип": {"select": {"name": task_type}},
        "Статус": {"select": {"name": "Предложено"}},
        "Приоритет": {"select": {"name": priority}},
        "Следующий шаг": _rich_text(next_step),
        "Источник": {"select": {"name": source}},
        "External ID": _rich_text(external_id),
        "Sync status": {"select": {"name": "pending"}},
        "Обновить Kommo": {"checkbox": True},
    }
    if result:
        properties["Результат"] = _rich_text(result)
    if project_page_id:
        properties["Проект"] = {"relation": [{"id": project_page_id}]}
    if due_at:
        properties["Срок"] = {"date": {"start": due_at.isoformat()}}
    data = await _request(
        "POST",
        "/pages",
        json={
            "parent": {
                "type": "data_source_id",
                "data_source_id": _data_source_id(
                    settings.notion_tasks_data_source_id
                ),
            },
            "properties": properties,
        },
    )
    return {
        "id": data["id"],
        "url": data.get("url") or notion_page_url(data["id"]),
        "created": True,
        "external_id": external_id,
    }


async def create_digest_task(
    *,
    title: str,
    lead: dict[str, Any],
    project_page_id: str | None,
    priority: str,
    task_type: str,
    due_at: datetime | None,
    next_step: str,
) -> dict[str, Any]:
    external_id = (
        f"digest:{datetime.now(timezone.utc).date().isoformat()}:"
        f"kommo:{int(lead['id'])}:{task_type}"
    )
    return await create_task(
        title=title,
        lead_id=int(lead["id"]),
        project_page_id=project_page_id,
        priority=priority,
        task_type=task_type,
        due_at=due_at,
        next_step=next_step,
        source="Digest",
        external_id=external_id,
    )


async def update_task(
    page_id: str,
    *,
    status: str | None = None,
    result: str | None = None,
    due_at: datetime | None = None,
    sync_status: str | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    properties: dict[str, Any] = {}
    if status:
        properties["Статус"] = {"select": {"name": status}}
    if result is not None:
        properties["Результат"] = _rich_text(result)
    if due_at:
        properties["Срок"] = {"date": {"start": due_at.isoformat()}}
    if sync_status:
        properties["Sync status"] = {"select": {"name": sync_status}}
    if error is not None:
        properties["Last error"] = _rich_text(error)
    data = await _request(
        "PATCH",
        f"/pages/{page_id}",
        json={"properties": properties},
    )
    return {
        "id": data.get("id") or page_id,
        "url": data.get("url") or notion_page_url(page_id),
    }


async def _create_related_draft_page(
    *,
    source_id: str,
    properties: dict[str, Any],
) -> dict[str, Any] | None:
    if not source_id.strip():
        return None
    data = await _request(
        "POST",
        "/pages",
        json={
            "parent": {
                "type": "data_source_id",
                "data_source_id": _data_source_id(source_id),
            },
            "properties": properties,
        },
    )
    return {
        "id": data["id"],
        "url": data.get("url") or notion_page_url(data["id"]),
    }


async def create_offer_draft(
    *,
    lead: dict[str, Any],
    draft: dict[str, Any],
    project_page_id: str | None = None,
) -> dict[str, Any] | None:
    properties: dict[str, Any] = {
        "Название": _title(
            str(draft.get("title") or f"КП — {lead.get('name') or lead.get('id')}")
        ),
        "Статус": {"select": {"name": "Черновик"}},
        "Версия": {"number": 1},
        "Дата подготовки": {
            "date": {"start": datetime.now(timezone.utc).date().isoformat()}
        },
        "Валюта": {"select": {"name": "USD"}},
        "Следующее действие": _rich_text(
            str(draft.get("next_action") or "Проверить и дополнить")
        ),
        "Комментарий клиента": _rich_text(
            "Недостающие данные: " + ", ".join(draft.get("missing_data") or [])
        ),
        "Черновик текста": _rich_text(str(draft.get("body") or "")),
        "Ссылка": {"url": lead.get("url") or lead.get("kommo_url")},
        "Kommo ID": {"number": int(lead["id"])},
    }
    if project_page_id:
        properties["Проект"] = {"relation": [{"id": project_page_id}]}
    return await _create_related_draft_page(
        source_id=settings.notion_offers_data_source_id,
        properties=properties,
    )


async def create_catalog_draft(
    *,
    lead: dict[str, Any],
    draft: dict[str, Any],
    project_page_id: str | None = None,
) -> dict[str, Any] | None:
    properties: dict[str, Any] = {
        "Название": _title(
            str(
                draft.get("title")
                or f"Каталог — {lead.get('name') or lead.get('id')}"
            )
        ),
        "Тип": {"select": {"name": "Каталог"}},
        "Статус": {"select": {"name": "Черновик"}},
        "Версия": {"number": 1},
        "Дата обновления": {
            "date": {"start": datetime.now(timezone.utc).date().isoformat()}
        },
        "Комментарий": _rich_text(
            "Недостающие данные: " + ", ".join(draft.get("missing_data") or [])
        ),
        "Черновик текста": _rich_text(str(draft.get("body") or "")),
        "Язык": {"multi_select": [{"name": "Русский"}]},
        "Ссылка": {"url": lead.get("url") or lead.get("kommo_url")},
        "Kommo ID": {"number": int(lead["id"])},
    }
    if project_page_id:
        properties["Проект"] = {"relation": [{"id": project_page_id}]}
    return await _create_related_draft_page(
        source_id=settings.notion_catalogs_data_source_id,
        properties=properties,
    )


async def create_communication_draft(
    *,
    lead: dict[str, Any],
    draft: dict[str, Any],
    project_page_id: str | None = None,
) -> dict[str, Any] | None:
    body = str(draft.get("body") or "")
    properties: dict[str, Any] = {
        "Название": _title(
            str(
                draft.get("title")
                or f"Follow-up — {lead.get('name') or lead.get('id')}"
            )
        ),
        "Канал": {"select": {"name": "Telegram"}},
        "Тип": {"select": {"name": "Исходящее"}},
        "Статус": {"select": {"name": "Запланировано"}},
        "Дата и время": {"date": {"start": datetime.now(timezone.utc).isoformat()}},
        "Тема": _rich_text(str(draft.get("title") or "Follow-up")),
        "Краткое содержание": _rich_text(body[:1500]),
        "Полный текст": _rich_text(body),
        "Kommo ID": {"number": int(lead["id"])},
    }
    if project_page_id:
        properties["Проект"] = {"relation": [{"id": project_page_id}]}
    return await _create_related_draft_page(
        source_id=settings.notion_communications_data_source_id,
        properties=properties,
    )
