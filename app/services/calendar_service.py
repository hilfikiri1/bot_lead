"""Calendar integration for Google Calendar or iCloud Calendar.

The provider is selected with CALENDAR_PROVIDER.  iCloud uses CalDAV with an
Apple app-specific password and does not require any credentials file inside
Railway.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urljoin
from uuid import uuid4
from xml.etree import ElementTree as ET
from zoneinfo import ZoneInfo

import httpx

from app.config import get_settings
from app.services.gmail_service import _get_credentials

logger = logging.getLogger(__name__)
settings = get_settings()

DAV_NS = "DAV:"
CALDAV_NS = "urn:ietf:params:xml:ns:caldav"
APPLE_NS = "http://apple.com/ns/ical/"
HTTP_TIMEOUT = 30.0


class CalendarIntegrationError(RuntimeError):
    """Safe calendar error suitable for displaying in Telegram."""


def provider_label() -> str:
    provider = (settings.calendar_provider or "icloud").strip().lower()
    if provider == "google":
        return "Google Calendar"
    if provider == "icloud":
        return "iCloud Calendar"
    return provider or "Calendar"


def create_event(
    title: str,
    description: str,
    start_time_iso: str | None,
    duration_minutes: int = 15,
    calendar_id: str | None = None,
) -> str:
    """Create an event in the configured calendar provider and return its ID."""
    start_dt, end_dt = _resolve_event_times(start_time_iso, duration_minutes)
    provider = (settings.calendar_provider or "icloud").strip().lower()

    if provider == "icloud":
        return _create_icloud_event(
            title=title,
            description=description,
            start_dt=start_dt,
            end_dt=end_dt,
            calendar_name=calendar_id or settings.icloud_calendar_name,
        )
    if provider == "google":
        return _create_google_event(
            title=title,
            description=description,
            start_dt=start_dt,
            end_dt=end_dt,
            calendar_id=calendar_id or settings.google_calendar_id,
        )

    raise CalendarIntegrationError(
        "Неизвестный CALENDAR_PROVIDER. Используйте icloud или google."
    )


def _resolve_event_times(
    start_time_iso: str | None,
    duration_minutes: int,
) -> tuple[datetime, datetime]:
    manager_tz = ZoneInfo(settings.manager_timezone)

    if start_time_iso:
        try:
            start_dt = datetime.fromisoformat(start_time_iso)
        except ValueError:
            logger.warning(
                "Could not parse start_time '%s', defaulting to tomorrow 10:00",
                start_time_iso,
            )
            start_dt = _default_start()
    else:
        start_dt = _default_start()

    if start_dt.tzinfo is None:
        start_dt = start_dt.replace(tzinfo=manager_tz)
    else:
        start_dt = start_dt.astimezone(manager_tz)

    duration_minutes = max(5, min(int(duration_minutes or 15), 8 * 60))
    return start_dt, start_dt + timedelta(minutes=duration_minutes)


def _create_google_event(
    *,
    title: str,
    description: str,
    start_dt: datetime,
    end_dt: datetime,
    calendar_id: str,
) -> str:
    # Import lazily so an iCloud-only deployment does not require Google
    # credentials at module import time.
    try:
        from googleapiclient.discovery import build
        from googleapiclient.errors import HttpError
    except ImportError as exc:
        raise CalendarIntegrationError(
            "Google Calendar provider недоступен: пакет google-api-python-client не установлен."
        ) from exc

    event_body = {
        "summary": title,
        "description": description,
        "start": {
            "dateTime": start_dt.isoformat(),
            "timeZone": settings.manager_timezone,
        },
        "end": {
            "dateTime": end_dt.isoformat(),
            "timeZone": settings.manager_timezone,
        },
        "reminders": {
            "useDefault": False,
            "overrides": [
                {"method": "popup", "minutes": 10},
                {"method": "email", "minutes": 30},
            ],
        },
    }

    try:
        service = build("calendar", "v3", credentials=_get_credentials())
        event = service.events().insert(calendarId=calendar_id, body=event_body).execute()
        event_id = event["id"]
        logger.info("Google Calendar event created: %s", event_id)
        return event_id
    except FileNotFoundError as exc:
        raise CalendarIntegrationError(
            "Google Calendar не настроен: отсутствует credentials/google_oauth.json. "
            "Переключите CALENDAR_PROVIDER на icloud или добавьте Google OAuth."
        ) from exc
    except HttpError as exc:
        logger.error("Google Calendar API error: %s", exc)
        raise CalendarIntegrationError("Google Calendar отклонил создание события.") from exc


def _icloud_credentials() -> tuple[str, str]:
    username = (settings.icloud_username or "").strip()
    password = (settings.icloud_app_specific_password or "").strip().replace(" ", "")
    if not username:
        raise CalendarIntegrationError("ICLOUD_USERNAME не задан в Railway Variables.")
    if not password:
        raise CalendarIntegrationError(
            "ICLOUD_APP_SPECIFIC_PASSWORD не задан. Нужен специальный пароль приложения Apple."
        )
    return username, password


def _request_icloud(
    method: str,
    url: str,
    *,
    content: bytes | str | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[httpx.Response, str]:
    username, password = _icloud_credentials()
    current_url = url
    request_headers = {
        "User-Agent": "BuyBringTelegramAssistant/1.0",
        **(headers or {}),
    }

    with httpx.Client(timeout=HTTP_TIMEOUT, follow_redirects=False) as client:
        for _ in range(6):
            try:
                response = client.request(
                    method,
                    current_url,
                    auth=(username, password),
                    content=content,
                    headers=request_headers,
                )
            except httpx.TimeoutException as exc:
                raise CalendarIntegrationError("iCloud Calendar не ответил вовремя.") from exc
            except httpx.ConnectError as exc:
                raise CalendarIntegrationError(
                    "Не удалось соединиться с iCloud Calendar."
                ) from exc

            if response.status_code in {301, 302, 303, 307, 308}:
                location = response.headers.get("location")
                if not location:
                    break
                current_url = urljoin(current_url, location)
                continue
            return response, current_url

    raise CalendarIntegrationError("Слишком много перенаправлений iCloud CalDAV.")


def _ensure_caldav_success(response: httpx.Response, operation: str) -> None:
    if response.status_code in {200, 201, 204, 207}:
        return
    if response.status_code in {401, 403}:
        raise CalendarIntegrationError(
            "iCloud отклонил авторизацию. Проверьте ICLOUD_USERNAME и специальный пароль приложения."
        )
    preview = response.text.replace("\n", " ")[:300]
    raise CalendarIntegrationError(
        f"iCloud CalDAV: не удалось выполнить «{operation}» "
        f"(HTTP {response.status_code}). {preview}"
    )


def _propfind(url: str, xml_body: str, *, depth: int = 0) -> tuple[bytes, str]:
    response, final_url = _request_icloud(
        "PROPFIND",
        url,
        content=xml_body.encode("utf-8"),
        headers={
            "Depth": str(depth),
            "Content-Type": "application/xml; charset=utf-8",
        },
    )
    _ensure_caldav_success(response, "поиск календаря")
    return response.content, final_url


def _first_property_href(xml_bytes: bytes, property_tag: str) -> str | None:
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        raise CalendarIntegrationError("iCloud вернул некорректный XML.") from exc

    for propstat in root.findall(f".//{{{DAV_NS}}}propstat"):
        status = propstat.findtext(f"{{{DAV_NS}}}status") or ""
        if " 200 " not in status:
            continue
        prop = propstat.find(f"{{{DAV_NS}}}prop")
        if prop is None:
            continue
        node = prop.find(property_tag)
        if node is None:
            continue
        href = node.findtext(f"{{{DAV_NS}}}href")
        if href:
            return href.strip()
    return None


def _discover_calendar_home() -> str:
    base_url = (settings.icloud_caldav_url or "https://caldav.icloud.com/").strip()
    if not base_url.endswith("/"):
        base_url += "/"

    principal_xml = """<?xml version="1.0" encoding="utf-8" ?>
<d:propfind xmlns:d="DAV:">
  <d:prop><d:current-user-principal /></d:prop>
</d:propfind>"""
    principal_response, final_base_url = _propfind(base_url, principal_xml, depth=0)
    principal_href = _first_property_href(
        principal_response,
        f"{{{DAV_NS}}}current-user-principal",
    )
    if not principal_href:
        raise CalendarIntegrationError(
            "iCloud не вернул current-user-principal для этого аккаунта."
        )
    principal_url = urljoin(final_base_url, principal_href)

    home_xml = f"""<?xml version="1.0" encoding="utf-8" ?>
<d:propfind xmlns:d="DAV:" xmlns:c="{CALDAV_NS}">
  <d:prop><c:calendar-home-set /></d:prop>
</d:propfind>"""
    home_response, final_principal_url = _propfind(principal_url, home_xml, depth=0)
    home_href = _first_property_href(
        home_response,
        f"{{{CALDAV_NS}}}calendar-home-set",
    )
    if not home_href:
        raise CalendarIntegrationError("iCloud не вернул адрес хранилища календарей.")
    return urljoin(final_principal_url, home_href)


def _discover_calendar_url(calendar_name: str | None) -> tuple[str, str]:
    home_url = _discover_calendar_home()
    calendars_xml = f"""<?xml version="1.0" encoding="utf-8" ?>
<d:propfind xmlns:d="DAV:" xmlns:c="{CALDAV_NS}" xmlns:a="{APPLE_NS}">
  <d:prop>
    <d:displayname />
    <d:resourcetype />
    <a:calendar-color />
  </d:prop>
</d:propfind>"""
    response_bytes, final_home_url = _propfind(home_url, calendars_xml, depth=1)

    try:
        root = ET.fromstring(response_bytes)
    except ET.ParseError as exc:
        raise CalendarIntegrationError("iCloud вернул некорректный список календарей.") from exc

    calendars: list[tuple[str, str]] = []
    for response_node in root.findall(f"{{{DAV_NS}}}response"):
        href = response_node.findtext(f"{{{DAV_NS}}}href")
        if not href:
            continue
        for propstat in response_node.findall(f"{{{DAV_NS}}}propstat"):
            status = propstat.findtext(f"{{{DAV_NS}}}status") or ""
            if " 200 " not in status:
                continue
            prop = propstat.find(f"{{{DAV_NS}}}prop")
            if prop is None:
                continue
            resource_type = prop.find(f"{{{DAV_NS}}}resourcetype")
            if resource_type is None or resource_type.find(
                f"{{{CALDAV_NS}}}calendar"
            ) is None:
                continue
            display_name = (
                prop.findtext(f"{{{DAV_NS}}}displayname") or "Без названия"
            ).strip()
            calendars.append((display_name, urljoin(final_home_url, href)))

    if not calendars:
        raise CalendarIntegrationError("В аккаунте iCloud не найдено доступных календарей.")

    requested = (calendar_name or "").strip().casefold()
    if requested:
        for name, calendar_url in calendars:
            if name.casefold() == requested:
                return calendar_url.rstrip("/") + "/", name
        available = ", ".join(name for name, _ in calendars[:10])
        raise CalendarIntegrationError(
            f"Календарь «{calendar_name}» не найден в iCloud. Доступны: {available}"
        )

    return calendars[0][1].rstrip("/") + "/", calendars[0][0]


def _ics_escape(value: str) -> str:
    return (
        str(value or "")
        .replace("\\", "\\\\")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
        .replace(",", "\\,")
        .replace(";", "\\;")
    )


def _create_icloud_event(
    *,
    title: str,
    description: str,
    start_dt: datetime,
    end_dt: datetime,
    calendar_name: str | None,
) -> str:
    calendar_url, resolved_calendar_name = _discover_calendar_url(calendar_name)
    uid = f"{uuid4().hex}@buybringsolutions"
    now_utc = datetime.now(tz=timezone.utc)
    start_utc = start_dt.astimezone(timezone.utc)
    end_utc = end_dt.astimezone(timezone.utc)

    ics = "\r\n".join(
        [
            "BEGIN:VCALENDAR",
            "VERSION:2.0",
            "PRODID:-//Buy & Bring Solutions//Telegram Assistant//RU",
            "CALSCALE:GREGORIAN",
            "METHOD:PUBLISH",
            "BEGIN:VEVENT",
            f"UID:{uid}",
            f"DTSTAMP:{now_utc.strftime('%Y%m%dT%H%M%SZ')}",
            f"DTSTART:{start_utc.strftime('%Y%m%dT%H%M%SZ')}",
            f"DTEND:{end_utc.strftime('%Y%m%dT%H%M%SZ')}",
            f"SUMMARY:{_ics_escape(title)}",
            f"DESCRIPTION:{_ics_escape(description)}",
            "STATUS:CONFIRMED",
            "TRANSP:OPAQUE",
            "BEGIN:VALARM",
            "TRIGGER:-PT10M",
            "ACTION:DISPLAY",
            f"DESCRIPTION:{_ics_escape(title)}",
            "END:VALARM",
            "END:VEVENT",
            "END:VCALENDAR",
            "",
        ]
    )

    event_url = urljoin(calendar_url, f"{quote(uid, safe='@')}.ics")
    response, _ = _request_icloud(
        "PUT",
        event_url,
        content=ics.encode("utf-8"),
        headers={
            "Content-Type": "text/calendar; charset=utf-8",
            "If-None-Match": "*",
        },
    )
    _ensure_caldav_success(response, "создание события")
    logger.info(
        "iCloud Calendar event created: uid=%s calendar=%s",
        uid,
        resolved_calendar_name,
    )
    return uid


def _default_start() -> datetime:
    manager_tz = ZoneInfo(settings.manager_timezone)
    tomorrow = datetime.now(tz=manager_tz) + timedelta(days=1)
    return tomorrow.replace(hour=10, minute=0, second=0, microsecond=0)
