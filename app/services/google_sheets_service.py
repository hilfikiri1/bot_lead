"""Read-only Google Sheets access for the internal lead registry."""

from __future__ import annotations

import base64
import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any

from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets.readonly"
_cache_rows: list["SpreadsheetRow"] | None = None
_cache_loaded_at: float = 0.0


class GoogleSheetsError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class SpreadsheetRow:
    row_number: int
    phone: str | None
    email: str | None
    client_name: str | None
    company: str | None
    product: str | None
    lead_number: str | None


def _has_service_account_credentials() -> bool:
    return bool(
        settings.google_sheets_service_account_json.strip()
        or settings.google_service_account_json.strip()
        or settings.google_service_account_json_base64.strip()
    )


def is_configured() -> bool:
    return bool(
        settings.google_sheets_spreadsheet_id.strip()
        and settings.google_sheets_worksheet_name.strip()
        and _has_service_account_credentials()
    )


def column_letter_to_index(column: str) -> int:
    clean = re.sub(r"[^A-Za-z]", "", column or "").upper()
    if not clean:
        raise GoogleSheetsError("Некорректная буква колонки Google Sheets.")
    value = 0
    for char in clean:
        value = value * 26 + (ord(char) - ord("A") + 1)
    return value - 1


def _load_service_account_info() -> dict[str, Any]:
    raw = settings.google_sheets_service_account_json.strip()
    if not raw:
        raw = settings.google_service_account_json.strip()
    if not raw:
        encoded = settings.google_service_account_json_base64.strip()
        if encoded:
            try:
                raw = base64.b64decode(encoded).decode("utf-8")
            except (ValueError, UnicodeDecodeError) as exc:
                raise GoogleSheetsError(
                    "Не удалось декодировать GOOGLE_SERVICE_ACCOUNT_JSON_BASE64."
                ) from exc
    if not raw:
        raise GoogleSheetsError(
            "Service account не задан. Добавьте GOOGLE_SHEETS_SERVICE_ACCOUNT_JSON "
            "или GOOGLE_SERVICE_ACCOUNT_JSON / GOOGLE_SERVICE_ACCOUNT_JSON_BASE64."
        )
    try:
        if raw.startswith("{"):
            return json.loads(raw)
        decoded = base64.b64decode(raw).decode("utf-8")
        return json.loads(decoded)
    except (json.JSONDecodeError, ValueError) as exc:
        raise GoogleSheetsError(
            "Не удалось прочитать JSON сервисного аккаунта Google."
        ) from exc


def service_account_email() -> str | None:
    try:
        return str(_load_service_account_info().get("client_email") or "").strip() or None
    except GoogleSheetsError:
        return None


def _sheets_service():
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    credentials = service_account.Credentials.from_service_account_info(
        _load_service_account_info(),
        scopes=[SHEETS_SCOPE],
    )
    return build("sheets", "v4", credentials=credentials, cache_discovery=False)


def _cell_value(row: list[Any], column_letter: str) -> str | None:
    if not column_letter.strip():
        return None
    index = column_letter_to_index(column_letter)
    if index >= len(row):
        return None
    value = row[index]
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _parse_rows(values: list[list[Any]]) -> list[SpreadsheetRow]:
    header_row = max(1, int(settings.google_sheets_header_row or 1))
    parsed: list[SpreadsheetRow] = []
    for offset, raw_row in enumerate(values[header_row:], start=header_row + 1):
        product = _cell_value(raw_row, settings.google_sheets_product_column)
        lead_number = _cell_value(raw_row, settings.google_sheets_lead_number_column)
        if not product and not lead_number:
            continue
        parsed.append(
            SpreadsheetRow(
                row_number=offset,
                phone=_cell_value(raw_row, settings.google_sheets_phone_column),
                email=_cell_value(raw_row, settings.google_sheets_email_column),
                client_name=_cell_value(
                    raw_row, settings.google_sheets_client_name_column
                ),
                company=_cell_value(raw_row, settings.google_sheets_company_column),
                product=product,
                lead_number=lead_number,
            )
        )
    return parsed


def _list_worksheet_titles(service: Any, spreadsheet_id: str) -> list[str]:
    try:
        metadata = service.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
    except Exception:
        return []
    titles: list[str] = []
    for sheet in metadata.get("sheets") or []:
        title = ((sheet or {}).get("properties") or {}).get("title")
        if title:
            titles.append(str(title))
    return titles


def _raise_sheets_access_error(
    exc: Exception,
    *,
    spreadsheet_id: str,
    worksheet: str,
) -> None:
    message = str(exc)
    email = service_account_email()
    share_hint = (
        f"Расшарьте таблицу на <code>{email}</code> (Viewer)."
        if email
        else "Расшарьте таблицу на email service account (Viewer)."
    )
    if "403" in message or "permission" in message.lower():
        raise GoogleSheetsError(
            "Google Sheets отклонил доступ.\n"
            f"Таблица: <code>{spreadsheet_id}</code>\n"
            f"{share_hint}"
        ) from exc
    if "404" in message or "not found" in message.lower():
        service = _sheets_service()
        titles = _list_worksheet_titles(service, spreadsheet_id)
        if titles and worksheet not in titles:
            preview = ", ".join(titles[:8])
            suffix = "…" if len(titles) > 8 else ""
            raise GoogleSheetsError(
                "Лист Google Sheets не найден.\n"
                f"Задано: <code>{worksheet}</code>\n"
                f"Доступные листы: {preview}{suffix}\n"
                f"Таблица: <code>{spreadsheet_id}</code>\n"
                f"{share_hint}"
            ) from exc
        raise GoogleSheetsError(
            "Таблица Google Sheets не найдена или недоступна.\n"
            f"ID: <code>{spreadsheet_id}</code>\n"
            f"Лист: <code>{worksheet}</code>\n"
            f"{share_hint}"
        ) from exc
    raise GoogleSheetsError("Не удалось прочитать Google Sheets.") from exc


def _fetch_rows_from_api() -> list[SpreadsheetRow]:
    worksheet = settings.google_sheets_worksheet_name.strip()
    spreadsheet_id = settings.google_sheets_spreadsheet_id.strip()
    if not worksheet or not spreadsheet_id:
        raise GoogleSheetsError("Google Sheets spreadsheet ID или worksheet не заданы.")

    service = _sheets_service()
    try:
        result = (
            service.spreadsheets()
            .values()
            .get(spreadsheetId=spreadsheet_id, range=f"{worksheet}!A:ZZ")
            .execute()
        )
    except Exception as exc:
        _raise_sheets_access_error(
            exc, spreadsheet_id=spreadsheet_id, worksheet=worksheet
        )

    values = result.get("values") or []
    if not values:
        raise GoogleSheetsError(
            f"Лист <code>{worksheet}</code> пуст. Проверьте имя листа и строку заголовка."
        )
    rows = _parse_rows(values)
    if not rows:
        raise GoogleSheetsError(
            "В таблице нет строк с данными в колонках P/Y. Проверьте настройки колонок."
        )
    return rows


def get_rows(*, force_refresh: bool = False) -> list[SpreadsheetRow]:
    global _cache_rows, _cache_loaded_at
    ttl = max(30, int(settings.google_sheets_cache_ttl_seconds or 300))
    now = time.time()
    if (
        not force_refresh
        and _cache_rows is not None
        and now - _cache_loaded_at < ttl
    ):
        return list(_cache_rows)

    rows = _fetch_rows_from_api()
    _cache_rows = rows
    _cache_loaded_at = now
    logger.info("Google Sheets cache refreshed: %d rows", len(rows))
    return list(rows)


def clear_cache() -> None:
    global _cache_rows, _cache_loaded_at
    _cache_rows = None
    _cache_loaded_at = 0.0


def get_row_by_number(row_number: int) -> SpreadsheetRow | None:
    for row in get_rows():
        if row.row_number == row_number:
            return row
    return None
