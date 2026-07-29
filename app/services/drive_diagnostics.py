"""Fine-grained Google Drive error classification and /drive_status diagnostics."""

from __future__ import annotations

import html
import json
from dataclasses import dataclass
from typing import Any

from googleapiclient.errors import HttpError

from app.config import get_settings
from app.services import google_credentials, google_drive_service

settings = get_settings()


@dataclass(frozen=True)
class DriveErrorInfo:
    category: str
    message: str
    status_code: int | None = None
    retryable: bool = False
    user_hint: str = ""


_CATEGORY_MESSAGES = {
    "api_disabled": (
        "Drive API выключен для service account / GCP проекта.",
        "Включите Google Drive API в Google Cloud Console.",
    ),
    "auth_failed": (
        "Не удалось авторизоваться в Google Drive.",
        "Проверьте GOOGLE_SERVICE_ACCOUNT_JSON / _BASE64.",
    ),
    "no_folder_access": (
        "Нет доступа к конкретной папке Google Drive.",
        "Откройте доступ к папке для email service account.",
    ),
    "no_shared_drive_membership": (
        "Service account не состоит в Shared Drive.",
        "Добавьте service account в участники Shared Drive.",
    ),
    "quota_exceeded": (
        "Превышена квота Google Drive API.",
        "Подождите и повторите запрос позже.",
    ),
    "not_found": (
        "Файл или папка Google Drive не найдены (удалены или неверный ID).",
        "Проверьте GOOGLE_DRIVE_*_FOLDER_ID.",
    ),
    "invalid_id": (
        "Неверный идентификатор файла или папки Google Drive.",
        "Проверьте формат folder ID в переменных окружения.",
    ),
    "transient": (
        "Временная ошибка Google Drive.",
        "Повторите запрос через несколько минут.",
    ),
    "disabled": (
        "Google Drive отключён в конфигурации.",
        "Установите GOOGLE_DRIVE_ENABLED=true.",
    ),
    "unknown": (
        "Неизвестная ошибка Google Drive API.",
        "Проверьте журнал интеграций (/errors).",
    ),
}


def classify_http_error(exc: HttpError) -> DriveErrorInfo:
    status = int(getattr(exc.resp, "status", 0) or 0)
    body = ""
    try:
        raw = exc.content.decode("utf-8") if isinstance(exc.content, (bytes, bytearray)) else str(exc.content or "")
        body = raw.casefold()
        parsed = json.loads(raw) if raw.strip().startswith("{") else {}
        errors = ((parsed.get("error") or {}).get("errors") or [])
        if errors:
            body += " " + " ".join(
                str(item.get("reason") or "") + " " + str(item.get("message") or "")
                for item in errors
            ).casefold()
        message = str((parsed.get("error") or {}).get("message") or raw)[:400]
    except Exception:
        message = str(exc)[:400]
        body = message.casefold()

    if status == 403:
        if any(token in body for token in ("accessnotconfigured", "drive api has not been used", "api has not been used")):
            category = "api_disabled"
        elif any(token in body for token in ("shared drive", "teamdrive", "not a member", "membership")):
            category = "no_shared_drive_membership"
        elif any(token in body for token in ("userratelimitexceeded", "ratelimitexceeded", "quota")):
            category = "quota_exceeded"
        else:
            category = "no_folder_access"
        retryable = category == "quota_exceeded"
    elif status == 404:
        category = "not_found"
        retryable = False
    elif status == 400 and any(token in body for token in ("invalid", "fileid", "folder")):
        category = "invalid_id"
        retryable = False
    elif status in {401, 403} and "invalid_grant" in body:
        category = "auth_failed"
        retryable = False
    elif status in {429, 500, 502, 503, 504}:
        category = "transient" if status != 429 else "quota_exceeded"
        retryable = True
    else:
        category = "unknown"
        retryable = status in {429, 500, 502, 503, 504}

    default_msg, hint = _CATEGORY_MESSAGES[category]
    return DriveErrorInfo(
        category=category,
        message=default_msg if status in {403, 404} else f"{default_msg} ({message})",
        status_code=status,
        retryable=retryable,
        user_hint=hint,
    )


def classify_drive_exception(exc: Exception) -> DriveErrorInfo:
    if isinstance(exc, google_drive_service.GoogleDriveError):
        if "отключён" in str(exc).casefold() or "disabled" in str(exc).casefold():
            msg, hint = _CATEGORY_MESSAGES["disabled"]
            return DriveErrorInfo("disabled", msg, status_code=None, retryable=False, user_hint=hint)
        if isinstance(getattr(exc, "__cause__", None), HttpError):
            return classify_http_error(exc.__cause__)  # type: ignore[arg-type]
        if exc.status_code == 403:
            msg, hint = _CATEGORY_MESSAGES["no_folder_access"]
            return DriveErrorInfo("no_folder_access", msg, 403, False, hint)
        if exc.status_code == 404:
            msg, hint = _CATEGORY_MESSAGES["not_found"]
            return DriveErrorInfo("not_found", msg, 404, False, hint)
        msg, hint = _CATEGORY_MESSAGES["unknown"]
        return DriveErrorInfo("unknown", str(exc)[:300] or msg, exc.status_code, bool(exc.retryable), hint)
    if isinstance(exc, HttpError):
        return classify_http_error(exc)
    if isinstance(exc, google_credentials.GoogleCredentialsError):
        msg, hint = _CATEGORY_MESSAGES["auth_failed"]
        return DriveErrorInfo("auth_failed", msg, None, False, hint)
    msg, hint = _CATEGORY_MESSAGES["unknown"]
    return DriveErrorInfo("unknown", sanitize_public_message(str(exc)) or msg, None, False, hint)


def sanitize_public_message(value: str) -> str:
    text = str(value or "")
    # Never leak credentials / keys in user-facing text.
    for token in ("-----BEGIN", "private_key", "client_email", "Bearer ", "ya29.", "AIza"):
        if token.casefold() in text.casefold():
            return "Скрыто: сообщение содержало чувствительные данные."
    return text[:400]


async def run_drive_status(*, probe_write: bool = False) -> dict[str, Any]:
    """Safe read-only diagnostics. Write probe is only for confirmed actions."""
    status: dict[str, Any] = {
        "enabled": bool(settings.google_drive_enabled),
        "configured": False,
        "auth_ok": False,
        "api_ok": False,
        "root_ok": False,
        "projects_ok": False,
        "can_list": False,
        "write_probe": None,
        "checks": [],
        "errors": [],
    }
    if not settings.google_drive_enabled:
        status["checks"].append({"name": "enabled", "ok": False, "detail": "GOOGLE_DRIVE_ENABLED=false"})
        return status

    status["checks"].append({"name": "enabled", "ok": True, "detail": "GOOGLE_DRIVE_ENABLED=true"})
    try:
        info = google_credentials.load_service_account_info()
        email = str(info.get("client_email") or "")
        # Expose only the SA email local-part domain hint without private key.
        status["auth_ok"] = bool(email)
        status["checks"].append(
            {
                "name": "auth",
                "ok": bool(email),
                "detail": f"service account configured ({email.split('@')[-1] if '@' in email else 'ok'})",
            }
        )
    except Exception as exc:
        info_err = classify_drive_exception(exc)
        status["errors"].append(info_err.category)
        status["checks"].append({"name": "auth", "ok": False, "detail": info_err.message})
        return status

    root_id = settings.google_drive_root_folder_id.strip()
    projects_id = settings.google_drive_projects_folder_id.strip()
    status["configured"] = bool(root_id and projects_id)
    status["checks"].append(
        {
            "name": "config",
            "ok": status["configured"],
            "detail": "folder IDs set" if status["configured"] else "GOOGLE_DRIVE_*_FOLDER_ID missing",
        }
    )

    for label, folder_id, key in (
        ("root", root_id, "root_ok"),
        ("projects", projects_id, "projects_ok"),
    ):
        if not folder_id:
            status["checks"].append({"name": label, "ok": False, "detail": "folder id empty"})
            continue
        try:
            meta = await google_drive_service.verify_folder_access(folder_id)
            status[key] = True
            status["api_ok"] = True
            status["checks"].append(
                {
                    "name": label,
                    "ok": True,
                    "detail": f"доступна: {str(meta.get('name') or folder_id)[:80]}",
                }
            )
            if label == "projects":
                files = await google_drive_service.list_project_files(folder_id, limit=5)
                status["can_list"] = True
                status["checks"].append(
                    {"name": "list", "ok": True, "detail": f"чтение OK ({len(files)} элементов)"}
                )
        except Exception as exc:
            info_err = classify_drive_exception(exc)
            status["errors"].append(info_err.category)
            status["checks"].append(
                {
                    "name": label,
                    "ok": False,
                    "detail": f"{info_err.category}: {info_err.message}",
                }
            )

    if probe_write and projects_id and status.get("projects_ok"):
        # Write probe is intentionally not implemented as automatic upload here;
        # executor stages a confirmed probe action separately.
        status["write_probe"] = "requires_confirmation"

    return status


def format_drive_status(status: dict[str, Any]) -> str:
    lines = ["<b>📁 Google Drive — диагностика</b>", ""]
    for check in status.get("checks") or []:
        mark = "✅" if check.get("ok") else "❌"
        lines.append(
            f"{mark} {html.escape(str(check.get('name') or '—'))}: "
            f"{html.escape(str(check.get('detail') or ''))}"
        )
    errors = status.get("errors") or []
    if errors:
        lines.extend(["", "<b>Категории ошибок</b>"])
        for category in errors:
            msg, hint = _CATEGORY_MESSAGES.get(category, _CATEGORY_MESSAGES["unknown"])
            lines.append(f"• <code>{html.escape(category)}</code> — {html.escape(msg)}")
            lines.append(f"  {html.escape(hint)}")
    lines.extend(
        [
            "",
            "Тестовая запись в Drive выполняется только после отдельного подтверждения.",
            "Секреты и ключи в отчёт не выводятся.",
        ]
    )
    return "\n".join(lines)
