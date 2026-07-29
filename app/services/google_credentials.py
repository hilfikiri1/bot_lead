"""Shared Google service account credential loading."""

from __future__ import annotations

import base64
import json
from typing import Any

from app.config import get_settings

settings = get_settings()


class GoogleCredentialsError(RuntimeError):
    pass


def load_service_account_info() -> dict[str, Any]:
    raw = settings.google_service_account_json.strip()
    if not raw:
        encoded = settings.google_service_account_json_base64.strip()
        if encoded:
            try:
                raw = base64.b64decode(encoded).decode("utf-8")
            except (ValueError, UnicodeDecodeError) as exc:
                raise GoogleCredentialsError(
                    "Не удалось декодировать GOOGLE_SERVICE_ACCOUNT_JSON_BASE64."
                ) from exc
    if not raw:
        raise GoogleCredentialsError(
            "Google service account не задан. "
            "Добавьте GOOGLE_SERVICE_ACCOUNT_JSON или GOOGLE_SERVICE_ACCOUNT_JSON_BASE64."
        )
    try:
        if raw.startswith("{"):
            return json.loads(raw)
        decoded = base64.b64decode(raw).decode("utf-8")
        return json.loads(decoded)
    except (json.JSONDecodeError, ValueError) as exc:
        raise GoogleCredentialsError(
            "Не удалось прочитать JSON сервисного аккаунта Google."
        ) from exc


def service_account_configured() -> bool:
    return bool(
        settings.google_service_account_json.strip()
        or settings.google_service_account_json_base64.strip()
    )
