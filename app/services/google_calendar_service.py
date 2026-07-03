"""Google Calendar API integration with service-account and OAuth refresh modes."""

from __future__ import annotations

import base64
import json
import logging
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar"
CALENDAR_EVENTS_SCOPE = "https://www.googleapis.com/auth/calendar.events"


class GoogleCalendarError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


def auth_mode() -> str:
    return (settings.google_calendar_auth_mode or "service_account").strip().lower()


def configured_calendar_id() -> str:
    return (settings.google_calendar_id or "").strip()


def calendar_timezone() -> str:
    return (settings.google_calendar_timezone or settings.manager_timezone or "Europe/Warsaw").strip()


def calendar_zoneinfo() -> ZoneInfo:
    try:
        return ZoneInfo(calendar_timezone())
    except Exception:
        return ZoneInfo("Europe/Warsaw")


def ensure_timezone_aware(dt: datetime, tz: ZoneInfo | None = None) -> datetime:
    """Attach calendar timezone when datetime has no tzinfo (e.g. after Redis round-trip)."""
    if dt.tzinfo is not None:
        return dt
    return dt.replace(tzinfo=tz or calendar_zoneinfo())


def google_calendar_datetime_fields(
    dt: datetime | str,
    tz: ZoneInfo | None = None,
) -> dict[str, str]:
    """Build Google Calendar start/end fields.

    Google expects wall-clock local time in dateTime plus a separate timeZone field.
    Including a UTC offset in dateTime together with timeZone can shift the event.
    """
    zone = tz or calendar_zoneinfo()
    if isinstance(dt, str):
        parsed = datetime.fromisoformat(dt)
    else:
        parsed = dt
    local = ensure_timezone_aware(parsed, zone).astimezone(zone)
    return {
        "dateTime": local.strftime("%Y-%m-%dT%H:%M:%S"),
        "timeZone": str(zone),
    }


def is_configured() -> bool:
    if not configured_calendar_id():
        return False
    mode = auth_mode()
    if mode == "service_account":
        return bool(
            settings.google_service_account_json.strip()
            or settings.google_service_account_json_base64.strip()
        )
    if mode == "oauth_refresh_token":
        return bool(
            settings.google_client_id.strip()
            and settings.google_client_secret.strip()
            and settings.google_refresh_token.strip()
        )
    return False


def _load_service_account_info() -> dict[str, Any]:
    raw = settings.google_service_account_json.strip()
    if not raw:
        encoded = settings.google_service_account_json_base64.strip()
        if encoded:
            try:
                raw = base64.b64decode(encoded).decode("utf-8")
            except (ValueError, UnicodeDecodeError) as exc:
                raise GoogleCalendarError(
                    "Не удалось декодировать GOOGLE_SERVICE_ACCOUNT_JSON_BASE64."
                ) from exc
    if not raw:
        raise GoogleCalendarError(
            "Google service account не задан. "
            "Добавьте GOOGLE_SERVICE_ACCOUNT_JSON или GOOGLE_SERVICE_ACCOUNT_JSON_BASE64."
        )
    try:
        if raw.startswith("{"):
            return json.loads(raw)
        decoded = base64.b64decode(raw).decode("utf-8")
        return json.loads(decoded)
    except (json.JSONDecodeError, ValueError) as exc:
        raise GoogleCalendarError(
            "Не удалось прочитать JSON сервисного аккаунта Google."
        ) from exc


def _build_credentials():
    mode = auth_mode()
    if mode == "service_account":
        from google.oauth2 import service_account

        info = _load_service_account_info()
        if not info.get("private_key"):
            raise GoogleCalendarError("В JSON сервисного аккаунта отсутствует private_key.")
        return service_account.Credentials.from_service_account_info(
            info,
            scopes=[CALENDAR_SCOPE],
        )
    if mode == "oauth_refresh_token":
        from google.oauth2.credentials import Credentials

        return Credentials(
            token=None,
            refresh_token=settings.google_refresh_token.strip(),
            token_uri="https://oauth2.googleapis.com/token",
            client_id=settings.google_client_id.strip(),
            client_secret=settings.google_client_secret.strip(),
            scopes=[CALENDAR_SCOPE],
        )
    raise GoogleCalendarError(
        f"Неизвестный GOOGLE_CALENDAR_AUTH_MODE: {settings.google_calendar_auth_mode}"
    )


def _calendar_service():
    try:
        from googleapiclient.discovery import build
    except ImportError as exc:
        raise GoogleCalendarError(
            "Пакет google-api-python-client не установлен."
        ) from exc
    return build("calendar", "v3", credentials=_build_credentials(), cache_discovery=False)


def _http_error_message(exc: Exception) -> tuple[str, int | None]:
    status_code = getattr(exc, "resp", None)
    code = getattr(status_code, "status", None) if status_code else None
    text = str(exc).lower()
    if code == 401 or "401" in text:
        return "Google Calendar отклонил авторизацию. Проверьте credentials.", 401
    if code == 403 or "403" in text or "forbidden" in text:
        return (
            "Недостаточно прав. Предоставьте сервисному аккаунту разрешение "
            f"изменять события в календаре «{settings.google_calendar_name}»."
        ), 403
    if code == 404 or "404" in text or "not found" in text:
        return (
            "Календарь не найден. Проверьте GOOGLE_CALENDAR_ID. "
            "Для сервисного аккаунта значение `primary` обычно не подходит."
        ), 404
    if code == 429 or "429" in text:
        return "Превышен лимит запросов Google Calendar. Повторите позже.", 429
    if code and 500 <= int(code) <= 599:
        return f"Внутренняя ошибка Google Calendar (HTTP {code}).", int(code)
    return "Google Calendar отклонил запрос.", code


def get_calendar_metadata() -> dict[str, Any]:
    calendar_id = configured_calendar_id()
    if not calendar_id:
        raise GoogleCalendarError(
            "GOOGLE_CALENDAR_ID не задан. Скопируйте ID календаря из настроек Google Calendar."
        )
    try:
        service = _calendar_service()
        meta = service.calendars().get(calendarId=calendar_id).execute()
    except Exception as exc:
        message, status = _http_error_message(exc)
        raise GoogleCalendarError(message, status_code=status) from exc
    return {
        "id": meta.get("id"),
        "summary": meta.get("summary") or settings.google_calendar_name,
        "time_zone": meta.get("timeZone") or calendar_timezone(),
        "access_role": meta.get("accessRole"),
    }


def service_account_email() -> str | None:
    try:
        info = _load_service_account_info()
        email = str(info.get("client_email") or "").strip()
        return email or None
    except GoogleCalendarError:
        return None


def _access_role_label(role: str | None) -> str:
    mapping = {
        "owner": "владелец",
        "writer": "редактор",
        "reader": "только чтение",
        "freebusyreader": "только занятость",
        "none": "нет доступа",
    }
    return mapping.get(str(role or "").lower(), str(role or "—"))


def diagnose_google_calendar(*, include_write_probe: bool = False) -> dict[str, Any]:
    result: dict[str, Any] = {
        "provider": "Google Calendar",
        "auth_mode": auth_mode(),
        "configured": is_configured(),
        "calendar_id_set": bool(configured_calendar_id()),
        "calendar_name": settings.google_calendar_name,
        "timezone": calendar_timezone(),
        "api_auth": False,
        "read_ok": False,
        "write_ok": False,
        "calendar_summary": None,
        "access_role": None,
        "service_account_email": service_account_email(),
        "error": None,
    }
    if not is_configured():
        result["error"] = (
            "Google Calendar не настроен. "
            "Добавьте GOOGLE_CALENDAR_ID и данные сервисного аккаунта в Railway."
        )
        return result
    try:
        meta = get_calendar_metadata()
        result["api_auth"] = True
        result["read_ok"] = True
        result["calendar_summary"] = meta.get("summary")
        result["access_role"] = meta.get("access_role")
        role = str(meta.get("access_role") or "").lower()
        if role == "reader":
            result["error"] = (
                "Google API видит роль «только чтение». "
                "Проверьте, что в JSON Railway тот же email, что в доступе календаря, "
                "и выбрано «Вносить изменения в мероприятия»."
            )
    except GoogleCalendarError as exc:
        result["error"] = str(exc)
        return result

    if include_write_probe and result["read_ok"]:
        from datetime import timedelta

        tz = calendar_zoneinfo()
        start = datetime.now(tz=tz) + timedelta(hours=2)
        end = start + timedelta(minutes=5)
        try:
            probe = create_event(
                title="BBS Bot write test",
                description="Временная проверка записи. Будет удалена.",
                start_dt=start,
                end_dt=end,
                reminder_minutes=0,
            )
            delete_event(probe["event_id"])
            result["write_ok"] = True
            result["error"] = None
        except GoogleCalendarError as exc:
            result["write_ok"] = False
            result["error"] = str(exc)
    elif str(result.get("access_role") or "").lower() in {"owner", "writer"}:
        result["write_ok"] = True
        result["error"] = None
    return result


def format_diagnostic_report(info: dict[str, Any]) -> str:
    if info.get("error") and not info.get("read_ok"):
        return f"❌ {info['error']}"

    auth_label = (
        "сервисный аккаунт"
        if info.get("auth_mode") == "service_account"
        else "OAuth refresh token"
    )
    lines = [
        "🧪 <b>ПРОВЕРКА GOOGLE CALENDAR</b>",
        "",
        f"Провайдер: Google Calendar",
        f"Авторизация: {auth_label}",
        f"Подключение к API: {'✅' if info.get('api_auth') else '❌'}",
        f"Календарь: {info.get('calendar_summary') or info.get('calendar_name') or '—'}",
        f"Calendar ID: {'настроен' if info.get('calendar_id_set') else 'не задан'}",
        f"Чтение: {'✅' if info.get('read_ok') else '❌'}",
        f"Создание событий: {'✅' if info.get('write_ok') else '❌'}",
        f"Роль в календаре: {_access_role_label(info.get('access_role'))} "
        f"(<code>{info.get('access_role') or '—'}</code>)",
        f"Часовой пояс: {info.get('timezone') or '—'}",
    ]
    email = info.get("service_account_email")
    if email:
        lines.append(f"Service account: <code>{email}</code>")
    if info.get("error") or not info.get("write_ok"):
        if info.get("error"):
            lines.extend(["", f"⚠️ {info['error']}"])
        elif not info.get("write_ok"):
            lines.extend(["", "⚠️ Запись не проверена. Запустите /calendar_test_write"])
        if not info.get("write_ok") and email:
            lines.append(
                "Расшарьте календарь «B&BS Work» на этот email с правом "
                "<b>Вносить изменения в мероприятия</b>."
            )
    return "\n".join(lines)


def create_event(
    *,
    title: str,
    description: str,
    start_dt: datetime | None = None,
    end_dt: datetime | None = None,
    start_iso: str | None = None,
    end_iso: str | None = None,
    reminder_minutes: int | None = None,
) -> dict[str, Any]:
    calendar_id = configured_calendar_id()
    if not calendar_id:
        raise GoogleCalendarError("GOOGLE_CALENDAR_ID не задан.")

    zone = calendar_zoneinfo()
    if start_dt is None:
        if not start_iso:
            raise GoogleCalendarError("Не указано время начала события.")
        start_dt = ensure_timezone_aware(datetime.fromisoformat(start_iso), zone)
    if end_dt is None:
        if not end_iso:
            raise GoogleCalendarError("Не указано время окончания события.")
        end_dt = ensure_timezone_aware(datetime.fromisoformat(end_iso), zone)
    start_dt = ensure_timezone_aware(start_dt, zone)
    end_dt = ensure_timezone_aware(end_dt, zone)

    reminder = (
        reminder_minutes
        if reminder_minutes is not None
        else int(settings.google_calendar_default_reminder_minutes or 30)
    )
    event_body: dict[str, Any] = {
        "summary": title[:1024],
        "description": description[:8000],
        "start": google_calendar_datetime_fields(start_dt, zone),
        "end": google_calendar_datetime_fields(end_dt, zone),
    }
    if reminder > 0:
        event_body["reminders"] = {
            "useDefault": False,
            "overrides": [{"method": "popup", "minutes": int(reminder)}],
        }
    else:
        event_body["reminders"] = {"useDefault": False, "overrides": []}

    send_updates = (settings.google_calendar_send_updates or "none").strip().lower()
    if send_updates not in {"all", "externalOnly", "none"}:
        send_updates = "none"

    try:
        service = _calendar_service()
        created = (
            service.events()
            .insert(
                calendarId=calendar_id,
                body=event_body,
                sendUpdates=send_updates,
            )
            .execute()
        )
    except Exception as exc:
        message, status = _http_error_message(exc)
        raise GoogleCalendarError(message, status_code=status) from exc

    event_id = created.get("id") or ""
    logger.info("Google Calendar event created: %s", event_id)
    return {
        "event_id": event_id,
        "event_url": created.get("htmlLink"),
        "provider": "google",
    }


def delete_event(event_id: str) -> None:
    calendar_id = configured_calendar_id()
    if not calendar_id or not event_id:
        return
    try:
        service = _calendar_service()
        service.events().delete(calendarId=calendar_id, eventId=event_id).execute()
        logger.info("Google Calendar event deleted: %s", event_id)
    except Exception as exc:
        message, status = _http_error_message(exc)
        raise GoogleCalendarError(message, status_code=status) from exc


def get_event(event_id: str) -> dict[str, Any]:
    calendar_id = configured_calendar_id()
    if not calendar_id:
        raise GoogleCalendarError("GOOGLE_CALENDAR_ID не задан.")
    try:
        service = _calendar_service()
        return service.events().get(calendarId=calendar_id, eventId=event_id).execute()
    except Exception as exc:
        message, status = _http_error_message(exc)
        raise GoogleCalendarError(message, status_code=status) from exc
