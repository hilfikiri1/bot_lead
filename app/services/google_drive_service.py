"""Google Drive project folder management for B&BS Agent v4."""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaInMemoryUpload

from app.config import get_settings
from app.services.google_credentials import GoogleCredentialsError, load_service_account_info

logger = logging.getLogger(__name__)
settings = get_settings()

DRIVE_SCOPE = "https://www.googleapis.com/auth/drive"
DRIVE_FILE_SCOPE = "https://www.googleapis.com/auth/drive.file"

PROJECT_SUBFOLDERS = (
    "01 Запрос клиента",
    "02 Техническое задание",
    "03 Поставщики и RFQ",
    "04 Прайсы фабрик",
    "05 Фото, видео и образцы",
    "06 Расчёты и сравнение",
    "07 Коммерческие предложения",
    "08 Сертификаты и проверка",
    "09 Логистика и таможня",
    "10 Договоры, инвойсы и оплата",
    "99 Архив проекта",
)

COUNTRY_FOLDER_NAMES = {
    "PL": "Польша",
    "UA": "Украина",
    "DE": "Германия",
    "BY": "Беларусь",
    "LT": "Литва",
    "LV": "Латвия",
    "EE": "Эстония",
    "OTHER": "07 Другие страны",
}

_RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class GoogleDriveError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None, retryable: bool = False):
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable


def is_enabled() -> bool:
    return bool(settings.google_drive_enabled)


def _build_service():
    if not is_enabled():
        raise GoogleDriveError("Google Drive отключён (GOOGLE_DRIVE_ENABLED=false).")
    try:
        info = load_service_account_info()
    except GoogleCredentialsError as exc:
        raise GoogleDriveError(str(exc), retryable=False) from exc
    credentials = service_account.Credentials.from_service_account_info(
        info, scopes=[DRIVE_SCOPE]
    )
    return build("drive", "v3", credentials=credentials, cache_discovery=False)


async def _run(func, *args, **kwargs):
    return await asyncio.to_thread(func, *args, **kwargs)


def _http_error(exc: HttpError) -> GoogleDriveError:
    status = int(getattr(exc.resp, "status", 0) or 0)
    retryable = status in _RETRYABLE_STATUS
    if status == 403:
        return GoogleDriveError(
            "Нет доступа к Google Drive. Проверьте права service account на корневую папку.",
            status_code=403,
            retryable=False,
        )
    if status == 404:
        return GoogleDriveError("Папка Google Drive не найдена.", status_code=404, retryable=False)
    return GoogleDriveError(
        f"Ошибка Google Drive HTTP {status}.",
        status_code=status,
        retryable=retryable,
    )


def get_drive_status() -> dict[str, Any]:
    if not is_enabled():
        return {"enabled": False, "configured": False, "message": "GOOGLE_DRIVE_ENABLED=false"}
    configured = bool(
        settings.google_drive_root_folder_id.strip()
        and settings.google_drive_projects_folder_id.strip()
    )
    return {
        "enabled": True,
        "configured": configured,
        "root_folder_id": bool(settings.google_drive_root_folder_id.strip()),
        "projects_folder_id": bool(settings.google_drive_projects_folder_id.strip()),
        "template_folder_id": bool(settings.google_drive_project_template_folder_id.strip()),
    }


async def verify_folder_access(folder_id: str) -> dict[str, Any]:
    service = _build_service()

    def _call():
        return (
            service.files()
            .get(fileId=folder_id, fields="id,name,mimeType,webViewLink", supportsAllDrives=True)
            .execute()
        )

    try:
        data = await _run(_call)
        return {
            "id": data.get("id"),
            "name": data.get("name"),
            "url": data.get("webViewLink"),
            "accessible": True,
        }
    except HttpError as exc:
        raise _http_error(exc) from exc


async def find_project_folder(*, parent_id: str, project_key: str) -> dict[str, Any] | None:
    service = _build_service()
    safe_key = project_key.replace("'", "\\'")

    def _call():
        query = (
            f"'{parent_id}' in parents and mimeType='application/vnd.google-apps.folder' "
            f"and trashed=false and name='{safe_key}'"
        )
        return (
            service.files()
            .list(
                q=query,
                fields="files(id,name,webViewLink)",
                pageSize=5,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            )
            .execute()
        )

    try:
        result = await _run(_call)
        files = result.get("files") or []
        return files[0] if files else None
    except HttpError as exc:
        raise _http_error(exc) from exc


async def list_project_files(folder_id: str, *, limit: int = 20) -> list[dict[str, Any]]:
    service = _build_service()

    def _call():
        query = f"'{folder_id}' in parents and trashed=false"
        return (
            service.files()
            .list(
                q=query,
                fields="files(id,name,mimeType,webViewLink,modifiedTime,size)",
                pageSize=max(1, min(limit, 100)),
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            )
            .execute()
        )

    try:
        result = await _run(_call)
        return list(result.get("files") or [])
    except HttpError as exc:
        raise _http_error(exc) from exc


async def ensure_project_subfolders(parent_folder_id: str) -> list[dict[str, Any]]:
    existing = await list_project_files(parent_folder_id, limit=100)
    existing_names = {str(item.get("name") or "") for item in existing}
    created: list[dict[str, Any]] = []
    for name in PROJECT_SUBFOLDERS:
        if name in existing_names:
            match = next((x for x in existing if x.get("name") == name), None)
            if match:
                created.append(match)
            continue
        folder = await _create_folder(name=name, parent_id=parent_folder_id)
        created.append(folder)
    return created


async def _create_folder(*, name: str, parent_id: str) -> dict[str, Any]:
    service = _build_service()
    body = {
        "name": name[:255],
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [parent_id],
    }

    def _call():
        return (
            service.files()
            .create(body=body, fields="id,name,webViewLink", supportsAllDrives=True)
            .execute()
        )

    try:
        return await _run(_call)
    except HttpError as exc:
        raise _http_error(exc) from exc


async def create_project_folder(
    *,
    project_key: str,
    parent_id: str,
    display_name: str | None = None,
) -> dict[str, Any]:
    existing = await find_project_folder(parent_id=parent_id, project_key=project_key)
    if existing:
        return existing
    folder_name = display_name or project_key
    return await _create_folder(name=folder_name, parent_id=parent_id)


async def upload_file(
    *,
    parent_folder_id: str,
    filename: str,
    content: bytes,
    mime_type: str = "application/octet-stream",
) -> dict[str, Any]:
    service = _build_service()
    media = MediaInMemoryUpload(content, mimetype=mime_type, resumable=False)
    body = {"name": filename[:255], "parents": [parent_folder_id]}

    def _call():
        return (
            service.files()
            .create(
                body=body,
                media_body=media,
                fields="id,name,webViewLink,mimeType,size",
                supportsAllDrives=True,
            )
            .execute()
        )

    try:
        return await _run(_call)
    except HttpError as exc:
        raise _http_error(exc) from exc


def sanitize_filename(value: str) -> str:
    cleaned = re.sub(r"[^\w.\- ()\[\]]+", "_", value or "", flags=re.UNICODE)
    return cleaned.strip("._ ")[:200] or "file"
