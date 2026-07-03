"""Notion workspace sync: clients, leads, calls, tasks, and goals."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

NOTION_API = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"
REQUEST_TIMEOUT = 30.0
RICH_TEXT_CHUNK = 1800

# Bot-owned properties are updated on sync; human-owned fields are never overwritten.
BOT_CALL_FIELDS = {
    "summary",
    "next_step",
    "missing_questions",
    "risks",
    "confidence",
    "transcript",
    "audio_link",
    "kommo_link",
    "needs_review",
}
HUMAN_FIELDS = {"manager_thoughts", "strategy_notes"}


def normalize_notion_id(value: str) -> str:
    """Format a Notion UUID with hyphens."""
    clean = re.sub(r"[^a-f0-9]", "", (value or "").lower())
    if len(clean) != 32:
        return (value or "").strip()
    return f"{clean[:8]}-{clean[8:12]}-{clean[12:16]}-{clean[16:20]}-{clean[20:]}"


def resolve_notion_database_id(value: str) -> str:
    """Accept UUID, hyphenated UUID, or full Notion URL (prefers ?v= for inline DB)."""
    raw = (value or "").strip()
    if not raw:
        return ""
    if "notion.com" in raw or raw.startswith("http"):
        view_match = re.search(r"[?&]v=([a-f0-9-]{32,36})", raw, re.I)
        if view_match:
            return normalize_notion_id(view_match.group(1))
        page_match = re.search(r"/(?:p|database)/([a-f0-9-]{32,36})", raw, re.I)
        if page_match:
            return normalize_notion_id(page_match.group(1))
        hex_match = re.search(r"([a-f0-9]{32})", raw, re.I)
        if hex_match:
            return normalize_notion_id(hex_match.group(1))
    return normalize_notion_id(raw)


def _database_id(value: str) -> str:
    return resolve_notion_database_id(value)


class NotionAPIError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


def notion_access_instructions(*, compact: bool = False) -> str:
    """Shared setup hint for granting the bot access to Notion databases."""
    if compact:
        return (
            "Откройте базу → <b>⋯ → Connections</b> → добавьте "
            "<b>Buy Bring Bot</b>. Private workspace подходит."
        )
    return (
        "Private workspace — <b>нормально</b>, teamspace не нужен.\n"
        "1. Откройте базу (Tasks / Clients / Leads / Calls).\n"
        "2. <b>⋯ → Connections → Add connections</b> → выберите "
        "<b>Buy Bring Bot</b>.\n"
        "3. Если база внутри страницы — подключите интеграцию и к родительской странице.\n"
        "4. В Railway можно вставить <b>полную ссылку</b> на базу — бот возьмёт ID "
        "из <code>?v=</code> (для базы внутри страницы) или из URL."
    )


def format_user_error(exc: NotionAPIError) -> str:
    """Return a short Telegram-friendly explanation for common Notion failures."""
    if exc.status_code == 404 or "object_not_found" in str(exc).lower():
        return (
            "❌ <b>База Notion не найдена</b>\n\n"
            f"{notion_access_instructions()}"
        )
    if exc.status_code == 401:
        return "❌ <b>Notion отклонил токен</b>\n\nПроверьте <code>NOTION_API_TOKEN</code>."
    if exc.status_code == 403:
        return (
            "❌ <b>Notion: нет доступа</b>\n\n"
            f"{notion_access_instructions()}"
        )
    return f"❌ <b>Ошибка Notion</b>\n\n{html_escape(str(exc)[:400])}"


@dataclass
class NotionSyncResult:
    client_page_id: str | None
    lead_page_id: str | None
    call_page_id: str | None
    task_page_id: str | None
    message: str


def is_configured() -> bool:
    return bool(
        settings.notion_api_token.strip()
        and settings.notion_calls_database_id.strip()
    )


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {settings.notion_api_token.strip()}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


def _rich_text(value: str | None) -> list[dict[str, Any]]:
    clean = str(value or "").strip()
    if not clean:
        return []
    return [{"type": "text", "text": {"content": clean[:RICH_TEXT_CHUNK]}}]


def _title(value: str) -> dict[str, Any]:
    return {"title": _rich_text(value or "Без названия")}


def _prop_text(value: str | None) -> dict[str, Any]:
    return {"rich_text": _rich_text(value)}


def _prop_number(value: float | int | None) -> dict[str, Any]:
    if value is None:
        return {"number": None}
    return {"number": float(value)}


def _prop_url(value: str | None) -> dict[str, Any]:
    clean = str(value or "").strip()
    return {"url": clean or None}


def _prop_date(value: datetime | date | str | None) -> dict[str, Any]:
    if value is None:
        return {"date": None}
    if isinstance(value, str):
        return {"date": {"start": value}}
    if isinstance(value, datetime):
        return {"date": {"start": value.astimezone(timezone.utc).isoformat()}}
    return {"date": {"start": value.isoformat()}}


def _prop_select(value: str | None) -> dict[str, Any]:
    clean = str(value or "").strip()
    if not clean:
        return {"select": None}
    return {"select": {"name": clean[:100]}}


def _prop_checkbox(value: bool) -> dict[str, Any]:
    return {"checkbox": bool(value)}


def _prop_relation(page_ids: list[str]) -> dict[str, Any]:
    return {"relation": [{"id": page_id} for page_id in page_ids if page_id]}


async def _request(
    method: str,
    path: str,
    *,
    json_body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not settings.notion_api_token.strip():
        raise NotionAPIError("NOTION_API_TOKEN не задан.")

    url = f"{NOTION_API}{path}"
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            response = await client.request(
                method,
                url,
                headers=_headers(),
                json=json_body,
            )
    except httpx.TimeoutException as exc:
        raise NotionAPIError("Notion не ответил вовремя.") from exc
    except httpx.ConnectError as exc:
        raise NotionAPIError("Не удалось соединиться с Notion API.") from exc

    if response.status_code == 204:
        return {}
    if not 200 <= response.status_code < 300:
        preview = response.text.replace("\n", " ")[:400]
        if response.status_code == 404:
            raise NotionAPIError(
                "База Notion не найдена. Подключите её к интеграции Buy Bring Bot "
                "(⋯ → Connections), даже если база в Private.",
                status_code=404,
            )
        raise NotionAPIError(
            f"Notion HTTP {response.status_code}: {preview}",
            status_code=response.status_code,
        )
    if not response.content:
        return {}
    return response.json()


async def query_database(
    database_id: str,
    *,
    filter_body: dict[str, Any] | None = None,
    page_size: int = 20,
) -> list[dict[str, Any]]:
    payload: dict[str, Any] = {"page_size": max(1, min(page_size, 100))}
    if filter_body:
        payload["filter"] = filter_body
    data = await _request(
        "POST",
        f"/databases/{database_id}/query",
        json_body=payload,
    )
    return data.get("results") or []


async def create_page(
    database_id: str,
    properties: dict[str, Any],
) -> dict[str, Any]:
    return await _request(
        "POST",
        "/pages",
        json_body={
            "parent": {"database_id": database_id},
            "properties": properties,
        },
    )


async def update_page(page_id: str, properties: dict[str, Any]) -> dict[str, Any]:
    return await _request(
        "PATCH",
        f"/pages/{page_id}",
        json_body={"properties": properties},
    )


async def archive_page(page_id: str) -> dict[str, Any]:
    return await _request(
        "PATCH",
        f"/pages/{page_id}",
        json_body={"archived": True},
    )


async def append_manager_thoughts(page_id: str, text: str) -> None:
    """Append to human-owned page body without touching bot properties."""
    clean = text.strip()
    if not clean:
        return
    await _request(
        "PATCH",
        f"/blocks/{page_id}/children",
        json_body={
            "children": [
                {
                    "object": "block",
                    "type": "paragraph",
                    "paragraph": {
                        "rich_text": _rich_text(clean),
                    },
                }
            ]
        },
    )


def _page_title(page: dict[str, Any]) -> str:
    properties = page.get("properties") or {}
    for prop in properties.values():
        if prop.get("type") == "title":
            parts = prop.get("title") or []
            return "".join(part.get("plain_text", "") for part in parts).strip()
    return ""


def _page_plain_text(page: dict[str, Any], field_names: tuple[str, ...]) -> str:
    properties = page.get("properties") or {}
    for name in field_names:
        prop = properties.get(name) or {}
        if prop.get("type") == "rich_text":
            parts = prop.get("rich_text") or []
            text = "".join(part.get("plain_text", "") for part in parts).strip()
            if text:
                return text
    return ""


async def _find_client_page(
    *,
    company: str | None,
    phone: str | None,
    email: str | None,
    name: str | None,
) -> str | None:
    db_id = _database_id(settings.notion_clients_database_id)
    if not db_id:
        return None

    filters: list[dict[str, Any]] = []
    if company:
        filters.append(
            {
                "property": "Company",
                "rich_text": {"contains": company[:80]},
            }
        )
    if phone:
        filters.append(
            {
                "property": "Phone",
                "rich_text": {"contains": phone[-8:]},
            }
        )
    if email:
        filters.append(
            {
                "property": "Email",
                "rich_text": {"equals": email},
            }
        )
    if name:
        filters.append(
            {
                "property": "Name",
                "title": {"contains": name[:80]},
            }
        )

    for item in filters:
        results = await query_database(db_id, filter_body=item, page_size=1)
        if results:
            return results[0]["id"]
    return None


async def upsert_client_page(
    *,
    client_id: int,
    name: str | None,
    company: str | None,
    phone: str | None,
    email: str | None,
    language: str | None,
    existing_page_id: str | None = None,
) -> str | None:
    db_id = _database_id(settings.notion_clients_database_id)
    if not db_id:
        return None

    title = (company or name or f"Client #{client_id}").strip()
    page_id = existing_page_id or await _find_client_page(
        company=company,
        phone=phone,
        email=email,
        name=name,
    )
    properties = {
        "Name": _title(title),
        "Company": _prop_text(company),
        "Phone": _prop_text(phone),
        "Email": _prop_text(email),
        "Language": _prop_select(language),
        "Status": _prop_select("Active"),
        "Local ID": _prop_number(client_id),
    }

    if page_id:
        await update_page(page_id, properties)
        return page_id

    created = await create_page(db_id, properties)
    return created.get("id")


async def upsert_lead_page(
    *,
    lead_id: int,
    title: str,
    client_page_id: str | None,
    product: str | None,
    budget: str | None,
    country: str | None,
    city: str | None,
    kommo_url: str | None,
    kommo_lead_id: int | None,
    existing_page_id: str | None = None,
) -> str | None:
    db_id = _database_id(settings.notion_leads_database_id)
    if not db_id:
        return None

    page_id = existing_page_id
    if not page_id and kommo_lead_id:
        results = await query_database(
            db_id,
            filter_body={
                "property": "Kommo ID",
                "number": {"equals": float(kommo_lead_id)},
            },
            page_size=1,
        )
        if results:
            page_id = results[0]["id"]

    properties: dict[str, Any] = {
        "Name": _title(title),
        "Product": _prop_text(product),
        "Budget": _prop_text(budget),
        "Country": _prop_text(country),
        "City": _prop_text(city),
        "Kommo URL": _prop_url(kommo_url),
        "Kommo ID": _prop_number(kommo_lead_id),
        "Stage": _prop_select("Open"),
        "Local ID": _prop_number(lead_id),
    }
    if client_page_id:
        properties["Client"] = _prop_relation([client_page_id])

    if page_id:
        await update_page(page_id, properties)
        return page_id

    created = await create_page(db_id, properties)
    return created.get("id")


async def create_call_page(
    *,
    title: str,
    client_page_id: str | None,
    lead_page_id: str | None,
    summary: str | None,
    next_step: str | None,
    missing_questions: list[str] | None,
    risks: list[str] | None,
    confidence: float | None,
    transcript: str | None,
    audio_url: str | None,
    kommo_url: str | None,
    needs_review: bool,
    local_voice_note_id: int,
) -> str | None:
    db_id = _database_id(settings.notion_calls_database_id)
    if not db_id:
        return None

    missing = "\n".join(f"- {item}" for item in (missing_questions or [])[:20])
    risk_text = "\n".join(f"- {item}" for item in (risks or [])[:20])
    transcript_preview = (transcript or "")[:RICH_TEXT_CHUNK]
    if transcript and len(transcript) > RICH_TEXT_CHUNK:
        transcript_preview += "\n\n[Полный транскрипт сохранён в базе бота]"

    properties: dict[str, Any] = {
        "Name": _title(title),
        "Date": _prop_date(datetime.now(tz=timezone.utc)),
        "Summary": _prop_text(summary),
        "Next step": _prop_text(next_step),
        "Missing questions": _prop_text(missing or None),
        "Risks": _prop_text(risk_text or None),
        "Confidence": _prop_number(confidence),
        "Transcript": _prop_text(transcript_preview or None),
        "Audio link": _prop_url(audio_url),
        "Kommo link": _prop_url(kommo_url),
        "Needs review": _prop_checkbox(needs_review),
        "Local voice note ID": _prop_number(local_voice_note_id),
    }
    if client_page_id:
        properties["Client"] = _prop_relation([client_page_id])
    if lead_page_id:
        properties["Lead"] = _prop_relation([lead_page_id])

    created = await create_page(db_id, properties)
    return created.get("id")


async def create_task_page(
    *,
    title: str,
    task_type: str = "Task",
    due_at: datetime | str | None = None,
    client_page_id: str | None = None,
    lead_page_id: str | None = None,
    source: str = "AI",
) -> str | None:
    db_id = _database_id(settings.notion_tasks_database_id)
    if not db_id:
        return None

    properties: dict[str, Any] = {
        "Name": _title(title),
        "Type": _prop_select(task_type),
        "Status": _prop_select("Todo"),
        "Source": _prop_select(source),
    }
    if due_at:
        properties["Due"] = _prop_date(due_at)
    if client_page_id:
        properties["Client"] = _prop_relation([client_page_id])
    if lead_page_id:
        properties["Lead"] = _prop_relation([lead_page_id])

    try:
        created = await create_page(db_id, properties)
        return created.get("id")
    except NotionAPIError as exc:
        logger.warning("Notion task page was not created: %s", exc)
        return None


async def sync_analyzed_call(
    *,
    client_id: int,
    client_name: str | None,
    client_company: str | None,
    client_phone: str | None,
    client_email: str | None,
    client_language: str | None,
    client_notion_page_id: str | None,
    lead_id: int,
    lead_title: str,
    lead_product: str | None,
    lead_budget: str | None,
    lead_country: str | None,
    lead_city: str | None,
    lead_kommo_url: str | None,
    lead_kommo_id: int | None,
    lead_notion_page_id: str | None,
    voice_note_id: int,
    transcript: str | None,
    audio_url: str | None,
    analysis: dict[str, Any],
) -> NotionSyncResult:
    if not is_configured() or not settings.notion_auto_sync:
        return NotionSyncResult(None, None, None, None, "Notion sync disabled")

    client_page_id = await upsert_client_page(
        client_id=client_id,
        name=client_name,
        company=client_company,
        phone=client_phone,
        email=client_email,
        language=client_language,
        existing_page_id=client_notion_page_id,
    )
    lead_page_id = await upsert_lead_page(
        lead_id=lead_id,
        title=lead_title,
        client_page_id=client_page_id,
        product=lead_product,
        budget=lead_budget,
        country=lead_country,
        city=lead_city,
        kommo_url=lead_kommo_url,
        kommo_lead_id=lead_kommo_id,
        existing_page_id=lead_notion_page_id,
    )

    call_title = (
        f"Call · {datetime.now().strftime('%Y-%m-%d')} · "
        f"{client_company or client_name or lead_title}"
    )
    call_page_id = await create_call_page(
        title=call_title,
        client_page_id=client_page_id,
        lead_page_id=lead_page_id,
        summary=analysis.get("conversation_summary"),
        next_step=analysis.get("recommended_next_step"),
        missing_questions=analysis.get("missing_questions") or [],
        risks=analysis.get("risks") or [],
        confidence=analysis.get("confidence_score"),
        transcript=transcript,
        audio_url=audio_url,
        kommo_url=lead_kommo_url,
        needs_review=bool(analysis.get("needs_human_review")),
        local_voice_note_id=voice_note_id,
    )

    task_page_id = None
    next_step = str(analysis.get("recommended_next_step") or "").strip()
    if next_step and _database_id(settings.notion_tasks_database_id):
        task_page_id = await create_task_page(
            title=next_step[:200],
            task_type="Task",
            client_page_id=client_page_id,
            lead_page_id=lead_page_id,
            source="AI",
        )

    return NotionSyncResult(
        client_page_id=client_page_id,
        lead_page_id=lead_page_id,
        call_page_id=call_page_id,
        task_page_id=task_page_id,
        message="Сохранено в Notion",
    )


async def add_note_to_page(page_id: str, note: str, *, human_field: bool = True) -> None:
    if human_field:
        await append_manager_thoughts(page_id, note)
        return
    await update_page(page_id, {"Summary": _prop_text(note)})


async def search_clients(query: str, *, limit: int = 5) -> list[dict[str, str]]:
    db_id = _database_id(settings.notion_clients_database_id)
    if not db_id or not query.strip():
        return []
    results = await query_database(
        db_id,
        filter_body={
            "or": [
                {"property": "Name", "title": {"contains": query}},
                {"property": "Company", "rich_text": {"contains": query}},
            ]
        },
        page_size=limit,
    )
    return [
        {
            "id": item["id"],
            "title": _page_title(item),
            "notes": _page_plain_text(item, ("Manager thoughts", "Strategy notes")),
        }
        for item in results
    ]


async def search_leads(query: str, *, limit: int = 5) -> list[dict[str, str]]:
    db_id = _database_id(settings.notion_leads_database_id)
    if not db_id or not query.strip():
        return []
    results = await query_database(
        db_id,
        filter_body={"property": "Name", "title": {"contains": query}},
        page_size=limit,
    )
    return [{"id": item["id"], "title": _page_title(item)} for item in results]


async def update_lead_fields(page_id: str, fields: dict[str, Any]) -> None:
    mapping = {
        "product": ("Product", _prop_text),
        "budget": ("Budget", _prop_text),
        "country": ("Country", _prop_text),
        "city": ("City", _prop_text),
        "stage": ("Stage", _prop_select),
        "title": ("Name", _title),
    }
    properties: dict[str, Any] = {}
    for key, value in fields.items():
        if key not in mapping or value in (None, ""):
            continue
        prop_name, builder = mapping[key]
        properties[prop_name] = builder(str(value))
    if properties:
        await update_page(page_id, properties)


async def delete_page_soft(page_id: str) -> None:
    await archive_page(page_id)


async def get_morning_digest() -> str:
    db_id = _database_id(settings.notion_tasks_database_id)
    if not db_id:
        return "Notion tasks database не настроена."

    tz = ZoneInfo(settings.manager_timezone)
    today = datetime.now(tz=tz).date()
    tomorrow = today + timedelta(days=1)

    due_tasks = await query_database(
        db_id,
        filter_body={
            "and": [
                {"property": "Status", "select": {"equals": "Todo"}},
                {
                    "or": [
                        {"property": "Due", "date": {"equals": today.isoformat()}},
                        {"property": "Due", "date": {"equals": tomorrow.isoformat()}},
                    ]
                },
            ]
        },
        page_size=20,
    )
    goals = await query_database(
        db_id,
        filter_body={
            "and": [
                {"property": "Type", "select": {"equals": "Goal"}},
                {"property": "Status", "select": {"equals": "Todo"}},
            ]
        },
        page_size=10,
    )

    lines = ["<b>Утренний дайджест</b>", ""]
    if goals:
        lines.append("<b>Цели</b>")
        for goal in goals:
            lines.append(f"• {html_escape(_page_title(goal))}")
        lines.append("")
    if due_tasks:
        lines.append("<b>Задачи и напоминания</b>")
        for task in due_tasks:
            lines.append(f"• {html_escape(_page_title(task))}")
    else:
        lines.append("На сегодня и завтра задач в Notion нет.")
    lines.append("")
    lines.append("Добавляйте мысли в Notion в поле <code>Manager thoughts</code> — бот их не перезаписывает.")
    return "\n".join(lines)


def html_escape(value: str) -> str:
    return (
        str(value or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
