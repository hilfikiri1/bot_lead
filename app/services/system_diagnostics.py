"""Read-only production diagnostics and support-bundle generation.

The diagnostic runner is intentionally conservative:
- it never changes Kommo, Sheets, Notion, Drive, Telegram or WhatsApp data;
- it never prints secret values;
- it produces one trace ID, one structured report and one human-readable report;
- it can optionally inspect one project without changing it.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable
from uuid import uuid4

import httpx
from redis.asyncio import Redis
from sqlalchemy import desc, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent import audit
from app.agent import tools as agent_tools
from app.agent.lead_refs import extract_internal_lead_number
from app.agent.security import sanitize_text, sanitize_value
from app.config import get_settings
from app.models.integration_event import IntegrationEvent
from app.services import (
    drive_diagnostics,
    google_sheets_service,
    kommo_chat_service,
    kommo_service,
    notion_service,
    project_link_service,
)

logger = logging.getLogger(__name__)
settings = get_settings()

_REQUIRED_TABLES = (
    "alembic_version",
    "integration_events",
    "agent_sessions",
    "pending_agent_actions",
    "project_links",
    "project_artifacts",
    "project_suppliers",
    "supplier_inquiries",
    "supplier_offers",
    "whatsapp_cloud_messages",
)

_SECRET_KEY_PARTS = (
    "token",
    "secret",
    "password",
    "authorization",
    "api_key",
    "private_key",
    "database_url",
    "redis_url",
    "access_key",
)


def new_trace_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"diag-{stamp}-{uuid4().hex[:6]}"


def _safe_error(exc: Exception) -> str:
    text_value = sanitize_text(str(exc), limit=500) or exc.__class__.__name__
    text_value = re.sub(r"https?://[^\s]+", "[url hidden]", text_value)
    return text_value[:500]


def _safe_value(value: Any, *, depth: int = 0) -> Any:
    if depth > 5:
        return "…"
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if any(part in key_text.casefold() for part in _SECRET_KEY_PARTS):
                result[key_text] = "***"
            else:
                result[key_text] = _safe_value(item, depth=depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        return [_safe_value(item, depth=depth + 1) for item in list(value)[:100]]
    if isinstance(value, str):
        return sanitize_text(value, limit=2000) or ""
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return sanitize_text(str(value), limit=1000) or value.__class__.__name__


def _env_state(value: Any) -> str:
    if value is None:
        return "MISSING"
    if isinstance(value, str) and not value.strip():
        return "EMPTY"
    return "SET"


def _check(
    name: str,
    status: str,
    detail: str,
    *,
    duration_ms: int = 0,
    data: dict[str, Any] | None = None,
    recommendation: str | None = None,
) -> dict[str, Any]:
    return {
        "name": name,
        "status": status,
        "detail": detail[:1000],
        "duration_ms": int(duration_ms),
        "data": _safe_value(data or {}),
        "recommendation": recommendation,
    }


async def _timed(
    name: str,
    callback: Callable[[], Awaitable[dict[str, Any]]],
    *,
    timeout_seconds: float = 20.0,
) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        result = await asyncio.wait_for(callback(), timeout=timeout_seconds)
        result = dict(result)
        result.setdefault("name", name)
        result.setdefault("status", "PASS")
        result.setdefault("detail", "OK")
        result["duration_ms"] = int((time.perf_counter() - started) * 1000)
        return _safe_value(result)
    except asyncio.TimeoutError:
        return _check(
            name,
            "FAIL",
            f"Проверка превысила {timeout_seconds:.0f} секунд.",
            duration_ms=int((time.perf_counter() - started) * 1000),
            recommendation="Проверьте сеть и доступность внешнего сервиса.",
        )
    except Exception as exc:
        logger.exception("Diagnostic check failed: %s", name)
        return _check(
            name,
            "FAIL",
            f"{exc.__class__.__name__}: {_safe_error(exc)}",
            duration_ms=int((time.perf_counter() - started) * 1000),
        )


async def _database_check(db: AsyncSession) -> dict[str, Any]:
    await db.execute(text("SELECT 1"))
    revision: str | None = None
    try:
        revision = (await db.execute(text("SELECT version_num FROM alembic_version"))).scalar_one_or_none()
    except Exception as exc:
        revision = f"ERROR:{exc.__class__.__name__}"

    tables: dict[str, bool] = {}
    for table_name in _REQUIRED_TABLES:
        value = (
            await db.execute(
                text("SELECT to_regclass(:table_name)"),
                {"table_name": table_name},
            )
        ).scalar_one_or_none()
        tables[table_name] = bool(value)
    missing = [name for name, exists in tables.items() if not exists]
    status = "PASS" if not missing else "FAIL"
    detail = (
        f"PostgreSQL отвечает; Alembic={revision or 'не определён'}; обязательные таблицы доступны."
        if not missing
        else f"PostgreSQL отвечает, но отсутствуют таблицы: {', '.join(missing)}"
    )
    return _check(
        "database",
        status,
        detail,
        data={"alembic_revision": revision, "tables": tables},
        recommendation=("Запустить миграции после проверки deployment." if missing else None),
    )


async def _redis_check() -> dict[str, Any]:
    if not settings.redis_url:
        return _check("redis", "FAIL", "REDIS_URL отсутствует.")
    client = Redis.from_url(
        settings.redis_url,
        socket_connect_timeout=4,
        socket_timeout=4,
        decode_responses=True,
    )
    try:
        pong = await client.ping()
        return _check("redis", "PASS", "Redis отвечает.", data={"ping": bool(pong)})
    finally:
        close = getattr(client, "aclose", None)
        if close:
            await close()
        else:
            await client.close()


async def _telegram_check() -> dict[str, Any]:
    if not settings.telegram_bot_token:
        return _check("telegram", "FAIL", "TELEGRAM_BOT_TOKEN отсутствует.")
    base = f"https://api.telegram.org/bot{settings.telegram_bot_token}"
    async with httpx.AsyncClient(timeout=12.0) as client:
        me_response, webhook_response = await asyncio.gather(
            client.get(f"{base}/getMe"),
            client.get(f"{base}/getWebhookInfo"),
        )
    me_response.raise_for_status()
    webhook_response.raise_for_status()
    me = (me_response.json() or {}).get("result") or {}
    webhook = (webhook_response.json() or {}).get("result") or {}
    current_url = str(webhook.get("url") or "")
    expected = f"{settings.webhook_base_url.rstrip('/')}/webhook/telegram"
    url_ok = bool(current_url and current_url == expected)
    pending = int(webhook.get("pending_update_count") or 0)
    last_error = webhook.get("last_error_message")
    status = "PASS" if url_ok and not last_error else "WARN"
    detail = (
        f"Bot @{me.get('username') or 'unknown'}; webhook {'совпадает' if url_ok else 'не совпадает'}; "
        f"ожидающих обновлений: {pending}."
    )
    if last_error:
        detail += f" Последняя ошибка: {sanitize_text(str(last_error), limit=200)}"
    return _check(
        "telegram",
        status,
        detail,
        data={
            "bot_id": me.get("id"),
            "username": me.get("username"),
            "webhook_matches_expected": url_ok,
            "pending_update_count": pending,
            "last_error_date": webhook.get("last_error_date"),
            "last_error_message": last_error,
            "max_connections": webhook.get("max_connections"),
        },
        recommendation=("Перерегистрировать Telegram webhook после проверки WEBHOOK_BASE_URL." if not url_ok else None),
    )


async def _kommo_check() -> dict[str, Any]:
    if not settings.kommo_base_url or not settings.kommo_access_token:
        return _check("kommo", "FAIL", "KOMMO_BASE_URL или KOMMO_ACCESS_TOKEN отсутствуют.")
    account = await kommo_service._request(
        "GET",
        "/api/v4/account",
        params={"with": "amojo_id"},
    )
    return _check(
        "kommo",
        "PASS",
        f"Kommo API отвечает: {account.get('name') or account.get('subdomain') or 'аккаунт доступен'}.",
        data={
            "account_id": account.get("id"),
            "name": account.get("name"),
            "subdomain": account.get("subdomain"),
            "country": account.get("country"),
            "amojo_id_present": bool(account.get("amojo_id")),
        },
    )


def _duplicate_numbers(rows: list[Any]) -> dict[str, list[int]]:
    positions: dict[str, list[int]] = {}
    for row in rows:
        number = str(getattr(row, "lead_number", "") or "").strip()
        if number:
            positions.setdefault(number, []).append(int(getattr(row, "row_number", 0) or 0))
    return {number: values for number, values in positions.items() if len(values) > 1}


async def _sheets_check() -> dict[str, Any]:
    if not settings.google_sheets_spreadsheet_id or not settings.google_sheets_worksheet_name:
        return _check("google_sheets", "FAIL", "Google Sheets spreadsheet ID или worksheet name отсутствует.")
    rows = await asyncio.to_thread(google_sheets_service.get_rows, force_refresh=True)
    blanks = [
        row
        for row in rows
        if str(getattr(row, "product", "") or "").strip()
        and not str(getattr(row, "lead_number", "") or "").strip()
    ]
    duplicates = _duplicate_numbers(rows)
    status = "PASS" if not duplicates else "WARN"
    detail = (
        f"Таблица читается: {len(rows)} строк; новых строк без Y: {len(blanks)}; "
        f"дубликатов Y: {len(duplicates)}."
    )
    return _check(
        "google_sheets",
        status,
        detail,
        data={
            "rows_count": len(rows),
            "blank_y_with_product": len(blanks),
            "duplicate_y": duplicates,
            "lead_number_column": settings.google_sheets_lead_number_column,
            "status_column": settings.google_sheets_status_column,
            "comment_column": settings.google_sheets_comment_column,
            "writes_enabled": bool(settings.google_sheets_write_enabled),
        },
        recommendation=("Исправить дубли Y до следующей пакетной синхронизации." if duplicates else None),
    )


async def _drive_check() -> dict[str, Any]:
    status_data = await drive_diagnostics.run_drive_status(probe_write=False)
    failed = [item for item in status_data.get("checks") or [] if not item.get("ok")]
    status = "PASS" if not failed else "WARN"
    detail = (
        "Drive доступен для чтения; root и projects папки проверены."
        if not failed
        else f"Drive: {len(failed)} проблем в read-only проверке."
    )
    return _check(
        "google_drive",
        status,
        detail,
        data=status_data,
        recommendation=("Проверить service account, membership Shared Drive и folder IDs." if failed else None),
    )


async def _notion_check() -> dict[str, Any]:
    if not settings.notion_api_token:
        return _check("notion", "FAIL", "NOTION_API_TOKEN отсутствует.")
    me = await notion_service._request("GET", "/users/me")
    databases: dict[str, Any] = {}
    failures = 0
    for label, raw_id, required in (
        ("tasks", settings.notion_tasks_database_id, True),
        ("clients", settings.notion_clients_database_id, False),
        ("leads", settings.notion_leads_database_id, False),
        ("calls", settings.notion_calls_database_id, False),
    ):
        if not str(raw_id or "").strip():
            databases[label] = {"configured": False, "required": required}
            if required:
                failures += 1
            continue
        resolved, attempts = await notion_service.resolve_accessible_database_id(raw_id, label=label)
        ok = bool(resolved)
        databases[label] = {
            "configured": True,
            "required": required,
            "accessible": ok,
            "attempts": attempts,
        }
        if required and not ok:
            failures += 1
    status = "PASS" if failures == 0 else "WARN"
    return _check(
        "notion",
        status,
        f"Notion API отвечает; интеграция: {me.get('name') or me.get('id') or 'unknown'}; обязательных проблем: {failures}.",
        data={"bot_name": me.get("name"), "bot_type": me.get("type"), "databases": databases},
        recommendation=("Подключить Buy Bring Bot к недоступным базам Notion." if failures else None),
    )


async def _whatsapp_check() -> dict[str, Any]:
    phone_id = str(
        settings.whatsapp_phone_number_id
        or os.getenv("WHATSAPP_PHONE_NUMBER_ID", "")
    ).strip()
    token = str(
        settings.whatsapp_access_token
        or os.getenv("WHATSAPP_ACCESS_TOKEN", "")
    ).strip()
    version = os.getenv("WHATSAPP_GRAPH_API_VERSION", "v25.0").strip() or "v25.0"
    if not phone_id or not token:
        return _check("whatsapp_cloud", "FAIL", "WHATSAPP_PHONE_NUMBER_ID или WHATSAPP_ACCESS_TOKEN отсутствуют.")
    url = f"https://graph.facebook.com/{version}/{phone_id}"
    async with httpx.AsyncClient(timeout=12.0) as client:
        response = await client.get(
            url,
            params={"fields": "display_phone_number,verified_name,quality_rating,platform_type"},
            headers={"Authorization": f"Bearer {token}"},
        )
    if response.status_code >= 400:
        try:
            payload = response.json()
            message = ((payload.get("error") or {}).get("message") or "Meta Graph API error")
        except Exception:
            message = response.text[:200]
        return _check(
            "whatsapp_cloud",
            "FAIL",
            f"Meta Graph API HTTP {response.status_code}: {sanitize_text(str(message), limit=250)}",
            data={"http_status": response.status_code, "graph_version": version},
        )
    payload = response.json() or {}
    return _check(
        "whatsapp_cloud",
        "PASS",
        f"Meta принимает токен; номер {payload.get('display_phone_number') or phone_id} доступен.",
        data={
            "http_status": response.status_code,
            "phone_id_accessible": True,
            "display_phone_number": payload.get("display_phone_number"),
            "verified_name": payload.get("verified_name"),
            "quality_rating": payload.get("quality_rating"),
            "platform_type": payload.get("platform_type"),
            "graph_version": version,
        },
    )


async def _project_check(db: AsyncSession, project_query: str) -> dict[str, Any]:
    try:
        lead = await agent_tools.resolve_lead(
            lead_id=None,
            query=project_query,
            context={},
        )
    except agent_tools.LeadResolutionError as exc:
        candidates = [
            {
                "id": item.get("id"),
                "name": item.get("name"),
                "internal_number": extract_internal_lead_number(item),
            }
            for item in exc.candidates[:8]
        ]
        return _check(
            "project",
            "FAIL",
            f"Проект {project_query!r} не найден однозначно: {_safe_error(exc)}",
            data={"candidates": candidates},
        )

    lead_id = int(lead.get("id") or 0)
    if not lead_id:
        return _check("project", "FAIL", "У найденной сделки отсутствует Kommo ID.")
    details = await kommo_service.get_lead_details(lead_id)
    link = await project_link_service.get_by_kommo_lead_id(db, lead_id)
    contacts = list(details.get("contacts") or [])
    contact = contacts[0] if contacts else {}
    phones = list(contact.get("phones") or [])
    emails = list(contact.get("emails") or [])
    chat_context = details.get("chat_context")
    if not isinstance(chat_context, dict):
        chat_context = await kommo_chat_service.get_lead_chat_context(lead_id)

    issues: list[str] = []
    if not phones:
        issues.append("В Kommo-контакте нет телефона")
    if not emails:
        issues.append("В Kommo-контакте нет email")
    if link is None:
        issues.append("ProjectLink не создан")
    elif not link.drive_folder_id:
        issues.append("ProjectLink не содержит Drive folder ID")
    if not chat_context.get("available"):
        reason = str(chat_context.get("reason") or "unknown")
        issues.append(f"История внешнего чата недоступна: {reason}")

    drive_folder_probe: dict[str, Any] | None = None
    if link is not None and link.drive_folder_id:
        try:
            meta = await drive_diagnostics.google_drive_service.verify_folder_access(
                link.drive_folder_id
            )
            drive_folder_probe = {
                "ok": True,
                "name": meta.get("name"),
                "mime_type": meta.get("mimeType"),
            }
        except Exception as exc:
            info = drive_diagnostics.classify_drive_exception(exc)
            drive_folder_probe = {
                "ok": False,
                "category": info.category,
                "detail": info.message,
                "hint": info.user_hint,
            }
            issues.append(f"Drive folder недоступна: {info.category}")

    status = "PASS" if not issues else "WARN"
    detail = (
        f"Проект №{extract_internal_lead_number(details) or '—'} / Kommo {lead_id}: проблем не найдено."
        if not issues
        else f"Проект Kommo {lead_id}: найдено проблем — {len(issues)}."
    )
    return _check(
        "project",
        status,
        detail,
        data={
            "query": project_query,
            "kommo_lead_id": lead_id,
            "internal_number": extract_internal_lead_number(details),
            "name": details.get("name"),
            "pipeline": details.get("pipeline_name"),
            "status": details.get("status_name"),
            "client_name": contact.get("name"),
            "phones_count": len(phones),
            "emails_count": len(emails),
            "tasks_count": len(details.get("tasks") or []),
            "notes_count": len(details.get("notes") or []),
            "project_link": (
                {
                    "project_key": link.project_key,
                    "country_code": link.country_code,
                    "drive_folder_id_present": bool(link.drive_folder_id),
                    "drive_folder_name": link.drive_folder_name,
                    "drive_folder_url": link.drive_folder_url,
                    "notion_page_id_present": bool(link.notion_project_page_id),
                    "notion_project_url": link.notion_project_url,
                    "status": link.status,
                }
                if link is not None
                else None
            ),
            "drive_folder_probe": drive_folder_probe,
            "chat_context": {
                "enabled": chat_context.get("enabled"),
                "available": chat_context.get("available"),
                "reason": chat_context.get("reason"),
                "messages_count": len(chat_context.get("messages") or []),
                "talks_count": len(chat_context.get("talks") or []),
            },
            "issues": issues,
        },
        recommendation=("Исправить перечисленные расхождения проекта и повторить /diag с тем же номером." if issues else None),
    )


async def _recent_events_check(
    db: AsyncSession,
    *,
    telegram_user_id: int | None,
    minutes: int = 60,
) -> dict[str, Any]:
    since = datetime.now(timezone.utc) - timedelta(minutes=max(5, min(minutes, 1440)))
    query = (
        select(IntegrationEvent)
        .where(IntegrationEvent.created_at >= since)
        .order_by(desc(IntegrationEvent.created_at))
        .limit(150)
    )
    if telegram_user_id:
        query = query.where(
            (IntegrationEvent.telegram_user_id == telegram_user_id)
            | (IntegrationEvent.telegram_user_id.is_(None))
        )
    events = list((await db.execute(query)).scalars().all())
    statuses = Counter(str(item.status or "unknown") for item in events)
    services = Counter(str(item.service or "unknown") for item in events)
    errors = [item for item in events if str(item.status).casefold() == "error"]
    compact_events = [
        {
            "id": item.id,
            "created_at": item.created_at.isoformat() if item.created_at else None,
            "service": item.service,
            "operation": item.operation,
            "status": item.status,
            "external_id": item.external_id,
            "duration_ms": item.duration_ms,
            "error_message": sanitize_text(item.error_message, limit=500),
        }
        for item in events
    ]
    status = "PASS" if not errors else "WARN"
    return _check(
        "recent_integration_events",
        status,
        f"За последние {minutes} минут: {len(events)} событий, ошибок: {len(errors)}.",
        data={
            "window_minutes": minutes,
            "status_counts": dict(statuses),
            "service_counts": dict(services),
            "events": compact_events,
        },
        recommendation=("Разобрать ошибки из JSON-пакета по trace ID и времени." if errors else None),
    )


def _config_check() -> dict[str, Any]:
    variables = {
        "DATABASE_URL": settings.database_url,
        "REDIS_URL": settings.redis_url,
        "TELEGRAM_BOT_TOKEN": settings.telegram_bot_token,
        "TELEGRAM_WEBHOOK_SECRET": settings.telegram_webhook_secret,
        "WEBHOOK_BASE_URL": settings.webhook_base_url,
        "KOMMO_BASE_URL": settings.kommo_base_url,
        "KOMMO_ACCESS_TOKEN": settings.kommo_access_token,
        "GOOGLE_SHEETS_SPREADSHEET_ID": settings.google_sheets_spreadsheet_id,
        "GOOGLE_SHEETS_WORKSHEET_NAME": settings.google_sheets_worksheet_name,
        "NOTION_API_TOKEN": settings.notion_api_token,
        "NOTION_TASKS_DATABASE_ID": settings.notion_tasks_database_id,
        "GOOGLE_DRIVE_ROOT_FOLDER_ID": settings.google_drive_root_folder_id,
        "GOOGLE_DRIVE_PROJECTS_FOLDER_ID": settings.google_drive_projects_folder_id,
        "WHATSAPP_ACCESS_TOKEN": settings.whatsapp_access_token or os.getenv("WHATSAPP_ACCESS_TOKEN"),
        "WHATSAPP_PHONE_NUMBER_ID": settings.whatsapp_phone_number_id or os.getenv("WHATSAPP_PHONE_NUMBER_ID"),
        "WHATSAPP_VERIFY_TOKEN": os.getenv("WHATSAPP_VERIFY_TOKEN"),
        "WHATSAPP_APP_SECRET": os.getenv("WHATSAPP_APP_SECRET"),
    }
    states = {name: _env_state(value) for name, value in variables.items()}
    missing = [name for name, state in states.items() if state != "SET"]
    status = "PASS" if not missing else "WARN"
    return _check(
        "configuration",
        status,
        "Все ключевые переменные заданы." if not missing else f"Не полностью настроены: {', '.join(missing)}",
        data={
            "variables": states,
            "feature_flags": {
                "agent_enabled": bool(settings.agent_enabled),
                "google_sheets_write_enabled": bool(settings.google_sheets_write_enabled),
                "google_drive_enabled": bool(settings.google_drive_enabled),
                "notion_auto_sync": bool(settings.notion_auto_sync),
                "whatsapp_enabled": bool(settings.whatsapp_enabled),
                "lead_status_sync_enabled": bool(settings.lead_status_sync_enabled),
            },
        },
    )


def _overall_status(checks: list[dict[str, Any]]) -> str:
    statuses = {str(item.get("status") or "").upper() for item in checks}
    if "FAIL" in statuses:
        return "FAIL"
    if "WARN" in statuses or "SKIP" in statuses:
        return "PASS WITH WARNINGS"
    return "PASS"


async def run_system_diagnostics(
    db: AsyncSession,
    *,
    telegram_user_id: int | None = None,
    project_query: str | None = None,
    recent_minutes: int = 60,
) -> dict[str, Any]:
    trace_id = new_trace_id()
    started_at = datetime.now(timezone.utc)
    logger.info(
        "diagnostic_start trace_id=%s telegram_user_id=%s project=%s",
        trace_id,
        telegram_user_id,
        project_query or "-",
    )

    checks: list[dict[str, Any]] = [_config_check()]
    checks.append(await _timed("database", lambda: _database_check(db), timeout_seconds=12))

    external_checks = await asyncio.gather(
        _timed("redis", _redis_check, timeout_seconds=8),
        _timed("telegram", _telegram_check, timeout_seconds=15),
        _timed("kommo", _kommo_check, timeout_seconds=20),
        _timed("google_sheets", _sheets_check, timeout_seconds=25),
        _timed("google_drive", _drive_check, timeout_seconds=25),
        _timed("notion", _notion_check, timeout_seconds=30),
        _timed("whatsapp_cloud", _whatsapp_check, timeout_seconds=15),
    )
    checks.extend(external_checks)

    if project_query:
        checks.append(
            await _timed(
                "project",
                lambda: _project_check(db, project_query),
                timeout_seconds=35,
            )
        )
    checks.append(
        await _timed(
            "recent_integration_events",
            lambda: _recent_events_check(
                db,
                telegram_user_id=telegram_user_id,
                minutes=recent_minutes,
            ),
            timeout_seconds=12,
        )
    )

    finished_at = datetime.now(timezone.utc)
    report = {
        "schema_version": 1,
        "trace_id": trace_id,
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "duration_ms": int((finished_at - started_at).total_seconds() * 1000),
        "overall_status": _overall_status(checks),
        "project_query": project_query,
        "checks": checks,
        "safety": {
            "external_writes_performed": False,
            "secrets_included": False,
            "local_audit_event_written": True,
        },
    }

    await audit.record_event(
        db,
        service="system_diagnostics",
        operation="read_only_audit",
        status="ok" if report["overall_status"] == "PASS" else "warning",
        external_id=trace_id,
        telegram_user_id=telegram_user_id,
        duration_ms=report["duration_ms"],
        payload={"project_query": project_query, "recent_minutes": recent_minutes},
        result={
            "overall_status": report["overall_status"],
            "checks": [
                {"name": item.get("name"), "status": item.get("status")}
                for item in checks
            ],
        },
    )
    logger.info(
        "diagnostic_complete trace_id=%s status=%s duration_ms=%s",
        trace_id,
        report["overall_status"],
        report["duration_ms"],
    )
    return _safe_value(report)


def format_diagnostic_summary(report: dict[str, Any]) -> str:
    marks = {"PASS": "✅", "WARN": "⚠️", "FAIL": "❌", "SKIP": "⏭"}
    lines = [
        "<b>🧪 B&BS — системная диагностика</b>",
        "",
        f"Trace ID: <code>{report.get('trace_id')}</code>",
        f"Общий статус: <b>{report.get('overall_status')}</b>",
        f"Время: {int(report.get('duration_ms') or 0) / 1000:.1f} сек.",
    ]
    if report.get("project_query"):
        lines.append(f"Проект: <b>{report.get('project_query')}</b>")
    lines.extend(["", "<b>Проверки</b>"])
    for item in report.get("checks") or []:
        status = str(item.get("status") or "WARN").upper()
        detail = str(item.get("detail") or "")
        if len(detail) > 180:
            detail = detail[:177] + "…"
        lines.append(
            f"{marks.get(status, '•')} <b>{item.get('name')}</b> — {status} · {detail}"
        )
    lines.extend(
        [
            "",
            "Ни один внешний сервис не изменён. Полный отчёт приложен в Markdown и JSON.",
        ]
    )
    return "\n".join(lines)[:4000]


def render_diagnostic_markdown(report: dict[str, Any]) -> bytes:
    lines = [
        "# B&BS System Diagnostic Report",
        "",
        f"- Trace ID: `{report.get('trace_id')}`",
        f"- Overall status: **{report.get('overall_status')}**",
        f"- Started: {report.get('started_at')}",
        f"- Finished: {report.get('finished_at')}",
        f"- Duration: {report.get('duration_ms')} ms",
        f"- Project query: {report.get('project_query') or '—'}",
        "",
    ]
    for item in report.get("checks") or []:
        lines.extend(
            [
                f"## {item.get('name')} — {item.get('status')}",
                "",
                str(item.get("detail") or ""),
                "",
            ]
        )
        if item.get("recommendation"):
            lines.extend([f"**Recommendation:** {item.get('recommendation')}", ""])
        data = item.get("data") or {}
        if data:
            lines.extend(
                [
                    "```json",
                    json.dumps(data, ensure_ascii=False, indent=2, default=str),
                    "```",
                    "",
                ]
            )
    lines.extend(
        [
            "## Safety",
            "",
            "- No external writes were performed.",
            "- Secret values are not included.",
            "- One sanitised local IntegrationEvent was written for traceability.",
            "",
        ]
    )
    return "\n".join(lines).encode("utf-8")


def render_diagnostic_json(report: dict[str, Any]) -> bytes:
    return json.dumps(
        sanitize_value(report),
        ensure_ascii=False,
        indent=2,
        default=str,
    ).encode("utf-8")
