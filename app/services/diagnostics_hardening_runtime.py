"""Hardening for production diagnostics and recoverable Drive project creation.

This runtime is installed after the base diagnostic runtime. It keeps diagnostics
read-only while making their output actionable, and makes confirmed Drive project
creation resilient to partial failures.
"""
from __future__ import annotations

import json
import os
import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import desc, select

from app.agent import project_drive
from app.agent.security import sanitize_text
from app.api import diagnostics as diagnostics_api
from app.config import get_settings
from app.models.integration_event import IntegrationEvent
from app.services import (
    diagnostic_runtime,
    drive_diagnostics,
    google_drive_service,
    kommo_service,
    operator_experience_runtime,
    project_link_service,
    system_diagnostics,
)

settings = get_settings()
_INSTALLED = False
_SECRET_STATES = {"SET", "EMPTY", "MISSING"}
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


def _safe_value(value: Any, *, depth: int = 0) -> Any:
    """Sanitize recursively without collapsing useful diagnostic event fields."""
    if depth > 10:
        return "…"
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            key_folded = key_text.casefold()
            if key_text == "secrets_included" and isinstance(item, bool):
                result[key_text] = item
            elif any(part in key_folded for part in _SECRET_KEY_PARTS):
                if isinstance(item, str) and item in _SECRET_STATES:
                    result[key_text] = item
                else:
                    result[key_text] = "***"
            else:
                result[key_text] = _safe_value(item, depth=depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        return [_safe_value(item, depth=depth + 1) for item in list(value)[:150]]
    if isinstance(value, str):
        return sanitize_text(value, limit=4000) or ""
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return sanitize_text(str(value), limit=1000) or value.__class__.__name__


def _render_diagnostic_json(report: dict[str, Any]) -> bytes:
    return json.dumps(
        _safe_value(report),
        ensure_ascii=False,
        indent=2,
        default=str,
    ).encode("utf-8")


def _env_state(value: Any) -> str:
    if value is None:
        return "MISSING"
    if isinstance(value, str) and not value.strip():
        return "EMPTY"
    return "SET"


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
        "WHATSAPP_ACCESS_TOKEN": settings.whatsapp_access_token
        or os.getenv("WHATSAPP_ACCESS_TOKEN"),
        "WHATSAPP_PHONE_NUMBER_ID": settings.whatsapp_phone_number_id
        or os.getenv("WHATSAPP_PHONE_NUMBER_ID"),
        "WHATSAPP_VERIFY_TOKEN": os.getenv("WHATSAPP_VERIFY_TOKEN"),
        "WHATSAPP_APP_SECRET": os.getenv("WHATSAPP_APP_SECRET"),
    }
    states = {name: _env_state(value) for name, value in variables.items()}
    optional = set()
    if not settings.whatsapp_enabled:
        optional.update(
            {
                "WHATSAPP_ACCESS_TOKEN",
                "WHATSAPP_PHONE_NUMBER_ID",
                "WHATSAPP_VERIFY_TOKEN",
                "WHATSAPP_APP_SECRET",
            }
        )
    missing_required = [
        name for name, state in states.items() if state != "SET" and name not in optional
    ]
    missing_optional = [
        name for name, state in states.items() if state != "SET" and name in optional
    ]
    if missing_required:
        status = "WARN"
        detail = f"Не полностью настроены обязательные переменные: {', '.join(missing_required)}"
    elif missing_optional:
        status = "PASS"
        detail = (
            "Основные переменные заданы. WhatsApp Cloud API отключён; "
            f"не заданы необязательные переменные: {', '.join(missing_optional)}"
        )
    else:
        status = "PASS"
        detail = "Все активные интеграции настроены."
    return system_diagnostics._check(
        "configuration",
        status,
        detail,
        data={
            "variables": states,
            "optional_when_disabled": sorted(optional),
            "feature_flags": {
                "agent_enabled": bool(settings.agent_enabled),
                "google_sheets_write_enabled": bool(
                    settings.google_sheets_write_enabled
                ),
                "google_drive_enabled": bool(settings.google_drive_enabled),
                "notion_auto_sync": bool(settings.notion_auto_sync),
                "whatsapp_enabled": bool(settings.whatsapp_enabled),
                "lead_status_sync_enabled": bool(settings.lead_status_sync_enabled),
            },
        },
    )


async def _whatsapp_check() -> dict[str, Any]:
    phone_id = str(
        settings.whatsapp_phone_number_id
        or os.getenv("WHATSAPP_PHONE_NUMBER_ID", "")
    ).strip()
    token = str(
        settings.whatsapp_access_token or os.getenv("WHATSAPP_ACCESS_TOKEN", "")
    ).strip()
    if not phone_id or not token:
        data = {
            "feature_enabled": bool(settings.whatsapp_enabled),
            "phone_number_id": _env_state(phone_id),
            "access_token": _env_state(token),
        }
        if not settings.whatsapp_enabled:
            return system_diagnostics._check(
                "whatsapp_cloud",
                "SKIP",
                "WhatsApp Cloud API отключён; отсутствие токена не блокирует остальные функции.",
                data=data,
                recommendation=(
                    "После верификации номера добавьте WHATSAPP_ACCESS_TOKEN и включите "
                    "WHATSAPP_ENABLED=true."
                ),
            )
        return system_diagnostics._check(
            "whatsapp_cloud",
            "FAIL",
            "WhatsApp включён, но WHATSAPP_PHONE_NUMBER_ID или WHATSAPP_ACCESS_TOKEN отсутствуют.",
            data=data,
        )
    return await _ORIGINAL_WHATSAPP_CHECK()


def _drive_literal(value: str) -> str:
    return str(value or "").replace("\\", "\\\\").replace("'", "\\'")


def _normalise_folder(data: dict[str, Any]) -> dict[str, Any]:
    capabilities = dict(data.get("capabilities") or {})
    return {
        "id": data.get("id"),
        "name": data.get("name"),
        "mimeType": data.get("mimeType"),
        "webViewLink": data.get("webViewLink"),
        "url": data.get("webViewLink"),
        "parents": list(data.get("parents") or []),
        "driveId": data.get("driveId"),
        "capabilities": capabilities,
        "accessible": True,
        "can_add_children": bool(capabilities.get("canAddChildren")),
        "can_list_children": bool(capabilities.get("canListChildren")),
        "can_edit": bool(capabilities.get("canEdit")),
    }


async def _verify_folder_access(folder_id: str) -> dict[str, Any]:
    service = google_drive_service._build_service()

    def _call():
        return (
            service.files()
            .get(
                fileId=folder_id,
                fields=(
                    "id,name,mimeType,webViewLink,parents,driveId,"
                    "capabilities(canAddChildren,canListChildren,canEdit)"
                ),
                supportsAllDrives=True,
            )
            .execute()
        )

    try:
        return _normalise_folder(await google_drive_service._run(_call))
    except Exception as exc:
        if exc.__class__.__name__ == "HttpError":
            raise google_drive_service._http_error(exc) from exc
        raise


async def _find_named_folder(*, parent_id: str, name: str) -> dict[str, Any] | None:
    service = google_drive_service._build_service()
    safe_parent = _drive_literal(parent_id)
    safe_name = _drive_literal(name)

    def _call():
        query = (
            f"'{safe_parent}' in parents and "
            "mimeType='application/vnd.google-apps.folder' and "
            f"trashed=false and name='{safe_name}'"
        )
        return (
            service.files()
            .list(
                q=query,
                fields=(
                    "files(id,name,mimeType,webViewLink,parents,driveId,"
                    "capabilities(canAddChildren,canListChildren,canEdit))"
                ),
                pageSize=10,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            )
            .execute()
        )

    try:
        files = (await google_drive_service._run(_call)).get("files") or []
        return _normalise_folder(files[0]) if files else None
    except Exception as exc:
        if exc.__class__.__name__ == "HttpError":
            raise google_drive_service._http_error(exc) from exc
        raise


async def _list_child_folders(parent_id: str, *, limit: int = 500) -> list[dict[str, Any]]:
    service = google_drive_service._build_service()
    safe_parent = _drive_literal(parent_id)

    def _call():
        query = (
            f"'{safe_parent}' in parents and "
            "mimeType='application/vnd.google-apps.folder' and trashed=false"
        )
        return (
            service.files()
            .list(
                q=query,
                fields=(
                    "files(id,name,mimeType,webViewLink,parents,driveId,"
                    "capabilities(canAddChildren,canListChildren,canEdit))"
                ),
                pageSize=max(1, min(limit, 1000)),
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            )
            .execute()
        )

    try:
        files = (await google_drive_service._run(_call)).get("files") or []
        return [_normalise_folder(item) for item in files]
    except Exception as exc:
        if exc.__class__.__name__ == "HttpError":
            raise google_drive_service._http_error(exc) from exc
        raise


async def _find_project_folder(
    *, parent_id: str, project_key: str
) -> dict[str, Any] | None:
    service = google_drive_service._build_service()
    safe_parent = _drive_literal(parent_id)
    safe_key = _drive_literal(project_key)

    def _call():
        query = (
            f"'{safe_parent}' in parents and "
            "mimeType='application/vnd.google-apps.folder' and trashed=false and "
            f"name contains '{safe_key}'"
        )
        return (
            service.files()
            .list(
                q=query,
                fields=(
                    "files(id,name,mimeType,webViewLink,parents,driveId,"
                    "capabilities(canAddChildren,canListChildren,canEdit))"
                ),
                pageSize=50,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            )
            .execute()
        )

    try:
        files = (await google_drive_service._run(_call)).get("files") or []
    except Exception as exc:
        if exc.__class__.__name__ == "HttpError":
            raise google_drive_service._http_error(exc) from exc
        raise
    normalized = [_normalise_folder(item) for item in files]
    exact = [item for item in normalized if str(item.get("name") or "") == project_key]
    if exact:
        return exact[0]
    prefixes = (
        f"{project_key} ",
        f"{project_key}—",
        f"{project_key} —",
        f"{project_key}-",
    )
    matches = [
        item
        for item in normalized
        if str(item.get("name") or "").startswith(prefixes)
    ]
    return matches[0] if len(matches) == 1 else None


def _clean(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()


async def _find_legacy_project_folder(
    *,
    parent_id: str,
    internal_number: str | None,
    lead_name: str | None,
    kommo_lead_id: int,
) -> dict[str, Any] | None:
    folders = await _list_child_folders(parent_id)
    internal = str(internal_number or "").strip()
    padded = f"{int(internal):04d}" if internal.isdigit() else ""
    lead_folded = _clean(lead_name).casefold()
    scored: list[tuple[int, dict[str, Any]]] = []
    for folder in folders:
        name = _clean(folder.get("name"))
        folded = name.casefold()
        score = 0
        if padded and re.search(rf"(?:^|[-_]){re.escape(padded)}(?:\D|$)", name):
            score += 70
        if internal and re.search(rf"(?:^|\D){re.escape(internal)}(?:\D|$)", name):
            score += 25
        if str(kommo_lead_id) in name:
            score += 100
        if lead_folded and lead_folded in folded:
            score += 60
        if score:
            scored.append((score, folder))
    if not scored:
        return None
    scored.sort(key=lambda item: item[0], reverse=True)
    if scored[0][0] < 80:
        return None
    if len(scored) > 1 and scored[0][0] == scored[1][0]:
        return None
    return scored[0][1]


async def _ensure_country_folder(*, root_id: str, country_code: str) -> dict[str, Any]:
    code = (country_code or "OTHER").upper()
    name = google_drive_service.COUNTRY_FOLDER_NAMES.get(
        code, google_drive_service.COUNTRY_FOLDER_NAMES["OTHER"]
    )
    existing = await _find_named_folder(parent_id=root_id, name=name)
    if existing:
        return existing
    created = await google_drive_service._create_folder(name=name, parent_id=root_id)
    return _normalise_folder(created)


async def _move_folder(
    *, folder: dict[str, Any], target_parent_id: str
) -> tuple[dict[str, Any], bool, str | None]:
    current_parents = [str(value) for value in folder.get("parents") or [] if value]
    if target_parent_id in current_parents:
        return folder, False, None
    service = google_drive_service._build_service()

    def _call():
        request = service.files().update(
            fileId=str(folder["id"]),
            addParents=target_parent_id,
            fields=(
                "id,name,mimeType,webViewLink,parents,driveId,"
                "capabilities(canAddChildren,canListChildren,canEdit)"
            ),
            supportsAllDrives=True,
        )
        if current_parents:
            request.uri += "&removeParents=" + ",".join(current_parents)
        return request.execute()

    try:
        return _normalise_folder(await google_drive_service._run(_call)), True, None
    except Exception as exc:
        return folder, False, sanitize_text(str(exc), limit=240)


def _pipeline_country(lead: dict[str, Any] | None) -> str | None:
    if not lead:
        return None
    text = " ".join(
        str(lead.get(key) or "")
        for key in ("pipeline_name", "pipeline", "country", "country_code")
    ).casefold()
    mapping = {
        "PL": ("польш", "polska", "poland"),
        "UA": ("украин", "україн", "ukraine"),
        "DE": ("герман", "niemcy", "germany"),
        "LV": ("латви", "latvia"),
        "LT": ("литв", "lithuania"),
        "BY": ("беларус", "belarus"),
    }
    for code, tokens in mapping.items():
        if any(token in text for token in tokens):
            return code
    return None


def _infer_country_code(
    *,
    lead: dict[str, Any] | None = None,
    explicit: str | None = None,
    saved: str | None = None,
) -> str:
    result = _ORIGINAL_INFER_COUNTRY(lead=lead, explicit=explicit, saved=saved)
    if result and result != "OTHER":
        return result
    return _pipeline_country(lead) or result or "OTHER"


async def _build_drive_project_preview(
    db: Any,
    *,
    lead: dict[str, Any],
    country_code: str | None = None,
) -> dict[str, Any]:
    data = await _ORIGINAL_BUILD_PREVIEW(
        db, lead=lead, country_code=country_code
    )
    root_id = settings.google_drive_projects_folder_id.strip()
    country = str(data.get("country_code") or "OTHER").upper()
    country_name = google_drive_service.COUNTRY_FOLDER_NAMES.get(
        country, google_drive_service.COUNTRY_FOLDER_NAMES["OTHER"]
    )
    data["country_folder_name"] = country_name
    if not root_id:
        return data
    try:
        folder = await _find_named_folder(parent_id=root_id, name=country_name)
    except Exception as exc:
        data.setdefault("warnings", []).append(
            f"Не удалось проверить папку страны: {exc.__class__.__name__}"
        )
        return data
    if folder:
        data["parent_folder_id"] = folder.get("id")
        data["country_folder_id"] = folder.get("id")
        data["country_folder_will_be_created"] = False
    else:
        data["parent_folder_id"] = root_id
        data["country_folder_id"] = None
        data["country_folder_will_be_created"] = True
        data.setdefault("warnings", []).append(
            f"Папка страны «{country_name}» будет создана после подтверждения"
        )
    return data


async def _execute_drive_project(
    db: Any,
    *,
    payload: dict[str, Any],
) -> dict[str, Any]:
    kommo_id = int(payload["kommo_lead_id"])
    project_key = str(payload["project_key"])
    folder_name = str(payload.get("folder_name") or project_key)
    root_id = settings.google_drive_projects_folder_id.strip()
    country = str(payload.get("country_code") or "OTHER").upper()
    internal = str(payload.get("internal_lead_number") or "").strip() or None
    lead_name = str(payload.get("kommo_lead_name") or payload.get("project_name") or "")
    if not root_id:
        raise ValueError("GOOGLE_DRIVE_PROJECTS_FOLDER_ID не задан")

    country_folder = await _ensure_country_folder(root_id=root_id, country_code=country)
    parent_id = str(country_folder["id"])
    existing = await project_link_service.get_by_kommo_lead_id(db, kommo_id)
    recovered_orphan = False
    moved_to_country = False
    move_warning: str | None = None

    if existing and existing.drive_folder_id:
        folder = await _verify_folder_access(existing.drive_folder_id)
    elif payload.get("existing_drive_folder_id"):
        folder = await _verify_folder_access(str(payload["existing_drive_folder_id"]))
    else:
        folder = await _find_project_folder(parent_id=parent_id, project_key=project_key)
        if folder is None:
            folder = await _find_project_folder(parent_id=root_id, project_key=project_key)
        if folder is None:
            folder = await _find_legacy_project_folder(
                parent_id=parent_id,
                internal_number=internal,
                lead_name=lead_name,
                kommo_lead_id=kommo_id,
            )
        if folder is None:
            folder = await _find_legacy_project_folder(
                parent_id=root_id,
                internal_number=internal,
                lead_name=lead_name,
                kommo_lead_id=kommo_id,
            )
        if folder is not None:
            recovered_orphan = True
            folder, moved_to_country, move_warning = await _move_folder(
                folder=folder,
                target_parent_id=parent_id,
            )
        else:
            created = await google_drive_service.create_project_folder(
                project_key=project_key,
                parent_id=parent_id,
                display_name=folder_name,
            )
            folder = _normalise_folder(created)

    folder_id = str(folder.get("id") or "")
    if not folder_id:
        raise RuntimeError("Google Drive не вернул ID папки проекта")

    # Persist the link before creating subfolders. If a later Drive operation fails,
    # the already-created folder remains discoverable and linked on the next retry.
    link = await project_link_service.upsert_link(
        db,
        project_key=project_key,
        kommo_lead_id=kommo_id,
        internal_lead_number=internal,
        kommo_lead_name=payload.get("kommo_lead_name"),
        country_code=country,
        client_name=payload.get("client_name"),
        project_name=payload.get("project_name"),
        drive_folder_id=folder_id,
        drive_folder_url=str(folder.get("webViewLink") or folder.get("url") or ""),
        drive_folder_name=str(folder.get("name") or folder_name),
        metadata={
            "folder_link_phase": "linked",
            "country_folder_id": parent_id,
            "recovered_orphan": recovered_orphan,
            "moved_to_country": moved_to_country,
            "move_warning": move_warning,
        },
    )

    subfolders = await google_drive_service.ensure_project_subfolders(folder_id)
    link = await project_link_service.upsert_link(
        db,
        project_key=project_key,
        kommo_lead_id=kommo_id,
        internal_lead_number=internal,
        kommo_lead_name=payload.get("kommo_lead_name"),
        country_code=country,
        client_name=payload.get("client_name"),
        project_name=payload.get("project_name"),
        drive_folder_id=folder_id,
        drive_folder_url=str(folder.get("webViewLink") or folder.get("url") or ""),
        drive_folder_name=str(folder.get("name") or folder_name),
        metadata={
            "folder_link_phase": "complete",
            "subfolder_count": len(subfolders),
            "country_folder_id": parent_id,
            "recovered_orphan": recovered_orphan,
            "moved_to_country": moved_to_country,
            "move_warning": move_warning,
        },
    )
    warnings = [move_warning] if move_warning else []
    return {
        "project_key": link.project_key,
        "drive_folder_id": link.drive_folder_id,
        "drive_folder_url": link.drive_folder_url,
        "subfolder_count": len(subfolders),
        "kommo_url": payload.get("kommo_url"),
        "country_folder": country_folder.get("name"),
        "recovered_orphan": recovered_orphan,
        "moved_to_country": moved_to_country,
        "warnings": warnings,
    }


async def _drive_check() -> dict[str, Any]:
    result = await _ORIGINAL_DRIVE_CHECK()
    data = dict(result.get("data") or {})
    capabilities: dict[str, Any] = {}
    for label, folder_id in (
        ("root", settings.google_drive_root_folder_id.strip()),
        ("projects", settings.google_drive_projects_folder_id.strip()),
    ):
        if not folder_id:
            continue
        try:
            meta = await _verify_folder_access(folder_id)
            capabilities[label] = {
                "name": meta.get("name"),
                "drive_id_present": bool(meta.get("driveId")),
                "can_add_children": bool(meta.get("can_add_children")),
                "can_list_children": bool(meta.get("can_list_children")),
                "can_edit": bool(meta.get("can_edit")),
            }
        except Exception as exc:
            capabilities[label] = {
                "error": exc.__class__.__name__,
                "detail": sanitize_text(str(exc), limit=240),
            }
    data["folder_capabilities"] = capabilities
    result["data"] = data
    projects = capabilities.get("projects") or {}
    if projects and not projects.get("error") and not projects.get("can_add_children"):
        result["status"] = "FAIL"
        result["detail"] = (
            "Drive читается, но service account не может добавлять файлы/папки "
            "в projects folder."
        )
        result["recommendation"] = (
            "Добавьте service account участником Shared Drive с правом Contributor/Content manager."
        )
    elif result.get("status") == "PASS":
        result["detail"] = (
            "Drive доступен; projects folder читается и разрешает добавление файлов/папок."
        )
    return result


def _custom_contact_candidates(details: dict[str, Any]) -> dict[str, list[str]]:
    phones: set[str] = set()
    emails: set[str] = set()
    entities = [details] + list(details.get("contacts") or [])
    for entity in entities:
        for marker, value in operator_experience_runtime._custom_values(entity):
            marker_folded = marker.casefold()
            if any(
                token in marker_folded
                for token in ("phone", "telefon", "numer", "телефон", "номер")
            ):
                digits = re.sub(r"\D", "", value)
                if 9 <= len(digits) <= 15:
                    phones.add(digits)
            if any(token in marker_folded for token in ("email", "e-mail", "почт")):
                email = value.strip().casefold()
                if "@" in email:
                    emails.add(email)
    note_text = "\n".join(
        str(item.get("text") or "") for item in details.get("notes") or []
    )
    for match in re.finditer(r"(?<!\d)(?:\+?48)?\d{9}(?!\d)", note_text):
        digits = re.sub(r"\D", "", match.group(0))
        if len(digits) in {9, 11}:
            phones.add(digits)
    for match in re.finditer(
        r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", note_text, flags=re.I
    ):
        emails.add(match.group(0).casefold())
    return {"phones": sorted(phones), "emails": sorted(emails)}


async def _find_orphan_for_project(
    *, details: dict[str, Any], lead_id: int
) -> dict[str, Any] | None:
    root_id = settings.google_drive_projects_folder_id.strip()
    if not root_id:
        return None
    internal = project_link_service.build_project_key(
        country_code=_infer_country_code(lead=details),
        internal_lead_number=(
            re.match(r"^\s*(\d+)", str(details.get("name") or "")).group(1)
            if re.match(r"^\s*(\d+)", str(details.get("name") or ""))
            else None
        ),
        kommo_lead_id=lead_id,
    )
    country = _infer_country_code(lead=details)
    country_name = google_drive_service.COUNTRY_FOLDER_NAMES.get(
        country, google_drive_service.COUNTRY_FOLDER_NAMES["OTHER"]
    )
    country_folder = await _find_named_folder(parent_id=root_id, name=country_name)
    parents = [str(country_folder.get("id"))] if country_folder else []
    parents.append(root_id)
    for parent_id in dict.fromkeys(parents):
        folder = await _find_project_folder(parent_id=parent_id, project_key=internal)
        if folder:
            return folder
        folder = await _find_legacy_project_folder(
            parent_id=parent_id,
            internal_number=(
                re.match(r"^\s*(\d+)", str(details.get("name") or "")).group(1)
                if re.match(r"^\s*(\d+)", str(details.get("name") or ""))
                else None
            ),
            lead_name=str(details.get("name") or ""),
            kommo_lead_id=lead_id,
        )
        if folder:
            return folder
    return None


async def _project_check(db: Any, project_query: str) -> dict[str, Any]:
    result = await _ORIGINAL_PROJECT_CHECK(db, project_query)
    data = dict(result.get("data") or {})
    lead_id = int(data.get("kommo_lead_id") or 0)
    if not lead_id:
        return result
    details = await kommo_service.get_lead_details(lead_id)
    candidates = _custom_contact_candidates(details)
    data["contact_sources"] = {
        "contact_phone_count": int(data.get("phones_count") or 0),
        "contact_email_count": int(data.get("emails_count") or 0),
        "fallback_phone_count": len(candidates["phones"]),
        "fallback_email_count": len(candidates["emails"]),
        "fallback_phone_last4": [value[-4:] for value in candidates["phones"]],
        "fallback_email_domains": sorted(
            {value.split("@", 1)[1] for value in candidates["emails"] if "@" in value}
        ),
    }
    issues = list(data.get("issues") or [])
    if not data.get("phones_count") and candidates["phones"]:
        issues = [item for item in issues if item != "В Kommo-контакте нет телефона"]
        issues.append("Телефон найден в заявке/примечании, но не перенесён в контакт Kommo")
    if not data.get("emails_count") and candidates["emails"]:
        issues = [item for item in issues if item != "В Kommo-контакте нет email"]
        issues.append("Email найден в заявке/примечании, но не перенесён в контакт Kommo")

    if data.get("project_link") is None:
        try:
            orphan = await _find_orphan_for_project(details=details, lead_id=lead_id)
        except Exception as exc:
            orphan = None
            data["orphan_folder_search_error"] = {
                "type": exc.__class__.__name__,
                "detail": sanitize_text(str(exc), limit=240),
            }
        if orphan:
            issues = [item for item in issues if item != "ProjectLink не создан"]
            issues.append("Drive-папка существует, но ProjectLink не сохранён")
            data["orphan_drive_folder"] = {
                "id": orphan.get("id"),
                "name": orphan.get("name"),
                "parents": orphan.get("parents"),
                "can_add_children": orphan.get("can_add_children"),
                "url": orphan.get("webViewLink") or orphan.get("url"),
            }

    probe = data.get("drive_folder_probe")
    if isinstance(probe, dict) and probe.get("ok") and probe.get("id"):
        try:
            meta = await _verify_folder_access(str(probe["id"]))
            probe["can_add_children"] = meta.get("can_add_children")
        except Exception:
            pass

    data["issues"] = list(dict.fromkeys(issues))
    result["data"] = data
    if data["issues"]:
        result["status"] = "WARN"
        result["detail"] = f"Проект Kommo {lead_id}: найдено проблем — {len(data['issues'])}."
    else:
        result["status"] = "PASS"
        result["detail"] = f"Проект Kommo {lead_id}: проблем не найдено."
    return result


async def _recent_events_check(
    db: Any,
    *,
    telegram_user_id: int | None,
    minutes: int = 60,
) -> dict[str, Any]:
    since = datetime.now(timezone.utc) - timedelta(
        minutes=max(5, min(minutes, 1440))
    )
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
    failures = [
        item
        for item in events
        if str(item.status or "").casefold() in {"error", "failed", "warning"}
    ]
    compact_events = []
    for item in events:
        event = {
            "id": item.id,
            "created_at": item.created_at.isoformat() if item.created_at else None,
            "service": item.service,
            "operation": item.operation,
            "status": item.status,
            "external_id": item.external_id,
            "duration_ms": item.duration_ms,
            "error_message": sanitize_text(item.error_message, limit=800),
        }
        if str(item.status or "").casefold() in {"error", "failed", "warning"}:
            event["payload"] = _safe_value(item.payload or {})
            event["result"] = _safe_value(item.result or {})
        compact_events.append(event)
    status = "PASS" if not failures else "WARN"
    return system_diagnostics._check(
        "recent_integration_events",
        status,
        (
            f"За последние {minutes} минут: {len(events)} событий, "
            f"ошибок/предупреждений: {len(failures)}."
        ),
        data={
            "window_minutes": minutes,
            "status_counts": dict(statuses),
            "service_counts": dict(services),
            "events": compact_events,
        },
        recommendation=(
            "Использовать точные service/operation/error_message из JSON-пакета."
            if failures
            else None
        ),
    )


def install_diagnostics_hardening_runtime() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True

    system_diagnostics._safe_value = _safe_value
    system_diagnostics._config_check = _config_check
    system_diagnostics._whatsapp_check = _whatsapp_check
    system_diagnostics._drive_check = _drive_check
    system_diagnostics._project_check = _project_check
    system_diagnostics._recent_events_check = _recent_events_check
    system_diagnostics.render_diagnostic_json = _render_diagnostic_json

    google_drive_service.verify_folder_access = _verify_folder_access
    google_drive_service.find_project_folder = _find_project_folder
    google_drive_service.find_country_folder = _find_named_folder
    google_drive_service.ensure_country_folder = _ensure_country_folder

    project_link_service.infer_country_code = _infer_country_code
    project_drive.build_drive_project_preview = _build_drive_project_preview
    project_drive.execute_drive_project = _execute_drive_project

    # Both modules imported diagnostic functions by name before this runtime was
    # installed, so update their aliases explicitly.
    diagnostic_runtime.run_system_diagnostics = system_diagnostics.run_system_diagnostics
    diagnostic_runtime.render_diagnostic_json = _render_diagnostic_json
    diagnostics_api.run_system_diagnostics = system_diagnostics.run_system_diagnostics


_ORIGINAL_WHATSAPP_CHECK = system_diagnostics._whatsapp_check
_ORIGINAL_DRIVE_CHECK = system_diagnostics._drive_check
_ORIGINAL_PROJECT_CHECK = system_diagnostics._project_check
_ORIGINAL_INFER_COUNTRY = project_link_service.infer_country_code
_ORIGINAL_BUILD_PREVIEW = project_drive.build_drive_project_preview
