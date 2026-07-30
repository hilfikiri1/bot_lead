"""Google Sheets access for the internal lead registry.

Reads are always available when the integration is configured. Writes are
guarded by a separate environment flag and are only called after an explicit
Telegram confirmation.
"""

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

SHEETS_READ_SCOPE = "https://www.googleapis.com/auth/spreadsheets.readonly"
SHEETS_WRITE_SCOPE = "https://www.googleapis.com/auth/spreadsheets"
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
    lead_status: str | None = None
    marketing_comment: str | None = None
    budget: str | None = None
    contact_channel: str | None = None
    region: str | None = None
    facebook_lead_id: str | None = None


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


def is_write_enabled() -> bool:
    return is_configured() and settings.google_sheets_write_enabled


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


def _sheets_service(*, readonly: bool = True):
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    credentials = service_account.Credentials.from_service_account_info(
        _load_service_account_info(),
        scopes=[SHEETS_READ_SCOPE if readonly else SHEETS_WRITE_SCOPE],
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
                lead_status=_cell_value(
                    raw_row, settings.google_sheets_status_column
                ),
                marketing_comment=_cell_value(
                    raw_row, settings.google_sheets_comment_column
                ),
                budget=_cell_value(raw_row, settings.google_sheets_budget_column),
                contact_channel=_cell_value(
                    raw_row, settings.google_sheets_channel_column
                ),
                region=_cell_value(raw_row, settings.google_sheets_region_column),
                facebook_lead_id=_cell_value(
                    raw_row, settings.google_sheets_facebook_lead_id_column
                )
                if settings.google_sheets_facebook_lead_id_column.strip()
                else None,
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


def _normalized_cell_text(value: Any) -> str:
    return " ".join(str(value or "").split()).casefold()


def _quoted_sheet_name(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _row_fingerprint(row: SpreadsheetRow) -> tuple[str, str, str, str]:
    return (
        _normalized_cell_text(row.phone),
        _normalized_cell_text(row.email),
        _normalized_cell_text(row.client_name),
        _normalized_cell_text(row.product),
    )


def write_internal_lead_number(
    *,
    row_number: int,
    expected_row_fingerprint: tuple[str, str, str, str] | None,
    new_number: str,
) -> dict[str, Any]:
    """Write the internal lead number for one row and re-read it to verify.

    Touches only the configured internal-ID column (``Y`` by default). Columns
    ``W`` and ``X`` are never referenced by this function under any
    circumstances.
    """
    if not settings.google_sheets_write_enabled:
        raise GoogleSheetsError(
            "Запись в Google Sheets отключена. Установите "
            "GOOGLE_SHEETS_WRITE_ENABLED=true, чтобы разрешить запись номера."
        )
    if not is_configured():
        raise GoogleSheetsError("Google Sheets не настроен.")

    number_column = settings.google_sheets_lead_number_column.strip().upper()
    column_letter_to_index(number_column)
    new_number = str(new_number).strip()
    if not new_number:
        raise GoogleSheetsError("Пустой внутренний номер лида.")

    current_rows = get_rows(force_refresh=True)
    row = next((item for item in current_rows if item.row_number == row_number), None)
    if row is None:
        return {"written": False, "reason": "row_missing", "verified": False}

    if expected_row_fingerprint and _row_fingerprint(row) != tuple(
        expected_row_fingerprint
    ):
        return {"written": False, "reason": "row_changed", "verified": False}

    current_number = str(row.lead_number or "").strip()
    if current_number and current_number != new_number:
        return {
            "written": False,
            "reason": "row_already_has_different_number",
            "current_number": current_number,
            "verified": False,
        }
    if current_number == new_number:
        # Already written by a previous attempt: idempotent no-op.
        return {"written": False, "reason": "already_written", "verified": True}

    worksheet = settings.google_sheets_worksheet_name.strip()
    sheet_ref = _quoted_sheet_name(worksheet)
    try:
        service = _sheets_service(readonly=False)
        (
            service.spreadsheets()
            .values()
            .update(
                spreadsheetId=settings.google_sheets_spreadsheet_id.strip(),
                range=f"{sheet_ref}!{number_column}{row_number}",
                valueInputOption="USER_ENTERED",
                body={"values": [[new_number]]},
            )
            .execute()
        )
    except Exception as exc:
        message = str(exc)
        if "403" in message or "permission" in message.lower():
            email = service_account_email()
            share_hint = (
                f"Дайте аккаунту {email} право Editor."
                if email
                else "Дайте service account право Editor."
            )
            raise GoogleSheetsError(
                "Google Sheets отклонил запись номера. " + share_hint
            ) from exc
        raise GoogleSheetsError(
            "Не удалось записать внутренний номер в Google Sheets."
        ) from exc

    clear_cache()
    verify_rows = get_rows(force_refresh=True)
    verify_row = next(
        (item for item in verify_rows if item.row_number == row_number), None
    )
    verified = bool(
        verify_row and str(verify_row.lead_number or "").strip() == new_number
    )
    return {"written": True, "verified": verified, "row_number": row_number}


def apply_lead_registry_updates(updates: list[dict[str, Any]]) -> dict[str, Any]:
    """Write confirmed lead numbers/comments without touching marketing status.

    Every target row is re-read and checked against the preview. Number writes
    are allowed only when the current Y value still equals ``old_lead_number``;
    comment writes follow the same rule for X. Column W is never included in
    the request.
    """
    if not settings.google_sheets_write_enabled:
        raise GoogleSheetsError(
            "Запись в Google Sheets отключена. Для разрешения подтверждаемых "
            "обновлений установите GOOGLE_SHEETS_WRITE_ENABLED=true в Railway."
        )
    if not is_configured():
        raise GoogleSheetsError("Google Sheets не настроен.")
    if not updates:
        return {"updated_count": 0, "updated": [], "skipped": []}
    if len(updates) > 500:
        raise GoogleSheetsError("Слишком много изменений за один запуск.")

    number_column = settings.google_sheets_lead_number_column.strip().upper()
    comment_column = settings.google_sheets_comment_column.strip().upper()
    column_letter_to_index(number_column)
    column_letter_to_index(comment_column)
    current_rows = get_rows(force_refresh=True)
    rows_by_position = {row.row_number: row for row in current_rows}

    safe_cells: list[dict[str, Any]] = []
    updated_rows: dict[int, dict[str, Any]] = {}
    skipped: list[dict[str, Any]] = []
    for item in updates:
        row_number = int(item.get("row_number") or 0)
        row = rows_by_position.get(row_number)
        if row is None:
            skipped.append(
                {
                    "row_number": row_number,
                    "reason": "row_missing",
                }
            )
            continue

        expected_fingerprint = tuple(item.get("row_fingerprint") or ())
        if expected_fingerprint and _row_fingerprint(row) != expected_fingerprint:
            skipped.append(
                {
                    "row_number": row_number,
                    "reason": "row_changed",
                }
            )
            continue

        old_number = str(item.get("old_lead_number") or "").strip()
        new_number = str(item.get("new_lead_number") or "").strip()
        current_number = str(row.lead_number or "").strip()
        old_comment = str(item.get("old_comment") or "").strip()
        new_comment = " ".join(str(item.get("new_comment") or "").split())
        current_comment = str(row.marketing_comment or "").strip()

        if current_number != old_number:
            skipped.append(
                {
                    "row_number": row_number,
                    "lead_number": current_number,
                    "reason": "lead_number_changed_manually",
                }
            )
            continue
        if _normalized_cell_text(current_comment) != _normalized_cell_text(old_comment):
            skipped.append(
                {
                    "row_number": row_number,
                    "lead_number": current_number,
                    "reason": "comment_changed_manually",
                }
            )
            continue

        row_result = {
            **item,
            "row_number": row_number,
            "new_lead_number": new_number,
            "new_comment": new_comment,
        }
        if new_number and new_number != current_number:
            safe_cells.append(
                {
                    "range": f"{number_column}{row_number}",
                    "value": new_number,
                }
            )
            updated_rows[row_number] = row_result
        if new_comment and new_comment != current_comment:
            safe_cells.append(
                {
                    "range": f"{comment_column}{row_number}",
                    "value": new_comment,
                }
            )
            updated_rows[row_number] = row_result

    if not safe_cells:
        return {"updated_count": 0, "updated": [], "skipped": skipped}

    worksheet = settings.google_sheets_worksheet_name.strip()
    sheet_ref = _quoted_sheet_name(worksheet)
    data = [
        {
            "range": f"{sheet_ref}!{cell['range']}",
            "majorDimension": "ROWS",
            "values": [[cell["value"]]],
        }
        for cell in safe_cells
    ]

    try:
        service = _sheets_service(readonly=False)
        (
            service.spreadsheets()
            .values()
            .batchUpdate(
                spreadsheetId=settings.google_sheets_spreadsheet_id.strip(),
                body={"valueInputOption": "USER_ENTERED", "data": data},
            )
            .execute()
        )
    except Exception as exc:
        message = str(exc)
        if "403" in message or "permission" in message.lower():
            email = service_account_email()
            share_hint = (
                f"Дайте аккаунту {email} право Editor."
                if email
                else "Дайте service account право Editor."
            )
            raise GoogleSheetsError(
                "Google Sheets отклонил запись. " + share_hint
            ) from exc
        raise GoogleSheetsError(
            "Не удалось обновить номера и комментарии в Google Sheets."
        ) from exc

    clear_cache()
    return {
        "updated_count": len(updated_rows),
        "updated_cells_count": len(safe_cells),
        "updated": list(updated_rows.values()),
        "skipped": skipped,
    }
