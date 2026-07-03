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
        resolve_spreadsheet_id()
        and settings.google_sheets_worksheet_name.strip()
        and _has_service_account_credentials()
    )


def extract_spreadsheet_id(value: str) -> str | None:
    """Extract spreadsheet ID from a raw ID or Google Sheets URL."""
    raw = (value or "").strip()
    if not raw:
        return None
    if "/spreadsheets/d/" in raw:
        match = re.search(r"/spreadsheets/d/([a-zA-Z0-9-_]+)", raw)
        if match:
            return match.group(1)
    if re.fullmatch(r"[a-zA-Z0-9-_]{20,}", raw):
        return raw
    return None


def resolve_spreadsheet_id() -> str:
    return (
        extract_spreadsheet_id(settings.google_sheets_spreadsheet_id)
        or settings.google_sheets_spreadsheet_id.strip()
    )


def _normalize_title(value: str) -> str:
    return " ".join((value or "").casefold().split())


def resolve_worksheet_name(titles: list[str], configured: str) -> str | None:
    """Match worksheet tab name case-insensitively."""
    clean = configured.strip()
    if not clean:
        return None
    if clean in titles:
        return clean
    wanted = _normalize_title(clean)
    for title in titles:
        if _normalize_title(title) == wanted:
            return title
    for title in titles:
        if wanted in _normalize_title(title) or _normalize_title(title) in wanted:
            return title
    return None


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


def _list_worksheet_titles(service: Any, spreadsheet_id: str) -> tuple[list[str], str | None]:
    try:
        metadata = service.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
    except Exception as exc:
        return [], str(exc)
    titles: list[str] = []
    for sheet in metadata.get("sheets") or []:
        title = ((sheet or {}).get("properties") or {}).get("title")
        if title:
            titles.append(str(title))
    document_title = str(metadata.get("properties", {}).get("title") or "").strip()
    return titles, document_title or None


def _raise_sheets_access_error(
    exc: Exception,
    *,
    spreadsheet_id: str,
    worksheet: str,
    service: Any | None = None,
) -> None:
    message = str(exc)
    email = service_account_email()
    share_hint = (
        f"Расшарьте таблицу на <code>{email}</code> с правом <b>Читатель</b> (Viewer)."
        if email
        else "Расшарьте таблицу на email service account с правом Читатель (Viewer)."
    )
    titles: list[str] = []
    document_title: str | None = None
    if service is not None:
        titles, document_title = _list_worksheet_titles(service, spreadsheet_id)

    if "403" in message or "permission" in message.lower():
        hint = (
            f"\n\nДоступные листы: {', '.join(titles[:8])}" if titles else ""
        )
        raise GoogleSheetsError(
            "Google Sheets отклонил доступ (403).\n"
            f"Таблица: <code>{spreadsheet_id}</code>\n"
            f"{share_hint}{hint}\n\n"
            "Также включите <b>Google Sheets API</b> в Google Cloud Console "
            "для проекта сервисного аккаунта."
        ) from exc

    range_error = (
        "unable to parse range" in message.lower()
        or "invalid" in message.lower()
        or "400" in message
    )
    not_found = "404" in message or "not found" in message.lower()

    if titles and worksheet not in titles:
        resolved = resolve_worksheet_name(titles, worksheet)
        preview = "\n".join(f"• <code>{title}</code>" for title in titles[:12])
        doc_hint = (
            f"\nНазвание файла: <b>{document_title}</b>"
            if document_title and _normalize_title(document_title) == _normalize_title(worksheet)
            else ""
        )
        raise GoogleSheetsError(
            "Лист Google Sheets не найден.\n"
            f"Задано в <code>GOOGLE_SHEETS_WORKSHEET_NAME</code>: "
            f"<code>{worksheet}</code>{doc_hint}\n\n"
            "Нужно имя <b>вкладки внизу</b> таблицы, а не название файла.\n\n"
            f"Доступные вкладки:\n{preview}\n\n"
            + (
                f"Похожая вкладка: <code>{resolved}</code>\n"
                if resolved and resolved != worksheet
                else ""
            )
            + share_hint
        ) from exc

    if not_found or range_error:
        if titles:
            preview = "\n".join(f"• <code>{title}</code>" for title in titles[:12])
            raise GoogleSheetsError(
                "Не удалось прочитать лист Google Sheets.\n"
                f"ID: <code>{spreadsheet_id}</code>\n"
                f"Лист: <code>{worksheet}</code>\n\n"
                f"Доступные вкладки:\n{preview}\n\n"
                f"{share_hint}"
            ) from exc
        raise GoogleSheetsError(
            "Таблица Google Sheets не найдена или недоступна.\n"
            f"ID: <code>{spreadsheet_id}</code>\n"
            f"Лист: <code>{worksheet}</code>\n\n"
            f"{share_hint}\n\n"
            "Проверьте <code>GOOGLE_SHEETS_SPREADSHEET_ID</code> — это часть URL "
            "между <code>/d/</code> и <code>/edit</code>.\n"
            "Запустите <code>/sheets_test</code> для диагностики."
        ) from exc
    raise GoogleSheetsError(
        f"Не удалось прочитать Google Sheets.\n<code>{message[:300]}</code>"
    ) from exc


def diagnose_google_sheets(*, include_sample_rows: bool = False) -> str:
    """Human-readable Google Sheets diagnostics for Telegram."""
    spreadsheet_id = resolve_spreadsheet_id()
    worksheet = settings.google_sheets_worksheet_name.strip()
    email = service_account_email()
    lines = [
        "🧪 <b>ПРОВЕРКА GOOGLE SHEETS</b>",
        "",
        f"Spreadsheet ID: {'✅ задан' if spreadsheet_id else '❌ не задан'}",
        f"Worksheet: <code>{worksheet or '—'}</code>",
        f"Service account: <code>{email or '—'}</code>",
    ]
    if not is_configured():
        lines.extend(
            [
                "",
                "❌ Google Sheets не полностью настроен.",
                "Задайте <code>GOOGLE_SHEETS_SPREADSHEET_ID</code>, "
                "<code>GOOGLE_SHEETS_WORKSHEET_NAME</code> и service account JSON.",
            ]
        )
        return "\n".join(lines)

    service = _sheets_service()
    titles, document_title = _list_worksheet_titles(service, spreadsheet_id)
    if not titles:
        lines.extend(
            [
                "",
                "❌ Таблица недоступна по этому ID.",
                f"ID: <code>{spreadsheet_id}</code>",
                "",
                f"Расшарьте файл на <code>{email}</code> (Читатель / Viewer).",
                "Проверьте ID в URL: <code>.../d/ID/edit</code>",
            ]
        )
        return "\n".join(lines)

    lines.append(f"Файл: <b>{document_title or '—'}</b>")
    lines.append(f"Вкладки ({len(titles)}): " + ", ".join(f"<code>{t}</code>" for t in titles[:6]))
    resolved = resolve_worksheet_name(titles, worksheet)
    if resolved and resolved != worksheet:
        lines.append(
            f"⚠️ Вкладка <code>{worksheet}</code> не найдена. "
            f"Похожая: <code>{resolved}</code>"
        )
    elif worksheet not in titles:
        lines.append(f"❌ Вкладка <code>{worksheet}</code> не найдена.")
    else:
        lines.append(f"✅ Вкладка <code>{worksheet}</code> найдена.")

    try:
        clear_cache()
        rows = get_rows(force_refresh=True) if include_sample_rows else _fetch_rows_from_api()
        lines.append(f"✅ Чтение: {len(rows)} строк с данными в колонках P/Y")
        if include_sample_rows and rows:
            sample = rows[0]
            lines.append(
                f"Пример: строка {sample.row_number}, "
                f"ID <code>{sample.lead_number or '—'}</code>, "
                f"товар <code>{(sample.product or '—')[:40]}</code>"
            )
    except GoogleSheetsError as exc:
        lines.append(f"❌ Чтение: {exc}")
    return "\n".join(lines)


def _fetch_rows_from_api() -> list[SpreadsheetRow]:
    configured_worksheet = settings.google_sheets_worksheet_name.strip()
    spreadsheet_id = resolve_spreadsheet_id()
    if not configured_worksheet or not spreadsheet_id:
        raise GoogleSheetsError("Google Sheets spreadsheet ID или worksheet не заданы.")

    service = _sheets_service()
    titles, _ = _list_worksheet_titles(service, spreadsheet_id)
    worksheet = (
        resolve_worksheet_name(titles, configured_worksheet)
        if titles
        else configured_worksheet
    ) or configured_worksheet

    try:
        result = (
            service.spreadsheets()
            .values()
            .get(spreadsheetId=spreadsheet_id, range=f"{worksheet}!A:ZZ")
            .execute()
        )
    except Exception as exc:
        _raise_sheets_access_error(
            exc,
            spreadsheet_id=spreadsheet_id,
            worksheet=configured_worksheet,
            service=service,
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
