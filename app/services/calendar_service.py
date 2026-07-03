"""Calendar integration for Google Calendar or iCloud Calendar.

The provider is selected with CALENDAR_PROVIDER.  iCloud uses CalDAV with an
Apple app-specific password and does not require any credentials file inside
Railway.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote, urljoin
from uuid import uuid4
from xml.etree import ElementTree as ET
from zoneinfo import ZoneInfo

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

DAV_NS = "DAV:"
CALDAV_NS = "urn:ietf:params:xml:ns:caldav"
APPLE_NS = "http://apple.com/ns/ical/"
HTTP_TIMEOUT = 30.0
_calendar_url_cache: dict[str, tuple[str, str, float]] = {}
_CACHE_TTL_SECONDS = 3600


class CalendarIntegrationError(RuntimeError):
    """Safe calendar error suitable for displaying in Telegram."""


def provider_label() -> str:
    provider = (settings.calendar_provider or "icloud").strip().lower()
    if provider == "google":
        return "Google Calendar"
    if provider == "icloud":
        return "iCloud Calendar"
    return provider or "Calendar"


def _clean_env_value(value: str | None) -> str:
    return str(value or "").strip().strip('"').strip("'").strip()


def _normalize_caldav_url(href: str, base_url: str) -> str:
    clean_href = href.strip()
    if clean_href.startswith("http://") or clean_href.startswith("https://"):
        return clean_href.rstrip("/") + "/"
    return urljoin(base_url.rstrip("/") + "/", clean_href.lstrip("/")).rstrip("/") + "/"


def _icloud_password_candidates() -> list[str]:
    raw = _clean_env_value(settings.icloud_app_specific_password).replace(" ", "")
    if not raw:
        return []
    candidates = [raw]
    compact = raw.replace("-", "")
    if compact and compact not in candidates:
        candidates.append(compact)
    if len(compact) == 16:
        dashed = "-".join(compact[i : i + 4] for i in range(0, 16, 4))
        if dashed not in candidates:
            candidates.append(dashed)
    return candidates


def build_ics_content(
    *,
    title: str,
    description: str,
    start_dt: datetime,
    end_dt: datetime,
    uid: str | None = None,
    reminder_minutes: int | None = None,
) -> tuple[str, str]:
    """Build an RFC5545 calendar file and return ``(uid, ics_text)``."""
    event_uid = uid or f"{uuid4().hex}@buybringsolutions"
    now_utc = datetime.now(tz=timezone.utc)
    start_utc = start_dt.astimezone(timezone.utc)
    end_utc = end_dt.astimezone(timezone.utc)
    reminder = max(0, int(reminder_minutes if reminder_minutes is not None else 10))
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Buy & Bring Solutions//Telegram Assistant//RU",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "BEGIN:VEVENT",
        f"UID:{event_uid}",
        f"DTSTAMP:{now_utc.strftime('%Y%m%dT%H%M%SZ')}",
        f"DTSTART:{start_utc.strftime('%Y%m%dT%H%M%SZ')}",
        f"DTEND:{end_utc.strftime('%Y%m%dT%H%M%SZ')}",
        f"SUMMARY:{_ics_escape(title)}",
        f"DESCRIPTION:{_ics_escape(description)}",
        "STATUS:CONFIRMED",
        "TRANSP:OPAQUE",
    ]
    if reminder > 0:
        lines.extend(
            [
                "BEGIN:VALARM",
                f"TRIGGER:-PT{reminder}M",
                "ACTION:DISPLAY",
                f"DESCRIPTION:{_ics_escape(title)}",
                "END:VALARM",
            ]
        )
    lines.extend(["END:VEVENT", "END:VCALENDAR", ""])
    return event_uid, "\r\n".join(lines)


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
        created = _create_google_event(
            title=title,
            description=description,
            start_dt=start_dt,
            end_dt=end_dt,
            calendar_id=calendar_id or settings.google_calendar_id,
        )
        return str(created.get("event_id") or "")

    raise CalendarIntegrationError(
        "Неизвестный CALENDAR_PROVIDER. Используйте icloud или google."
    )


def _resolve_event_times(
    start_time_iso: str | None,
    duration_minutes: int,
) -> tuple[datetime, datetime]:
    from app.services.calendar_event_builder import ensure_timezone_aware, manager_timezone

    calendar_tz = manager_timezone()

    if start_time_iso:
        try:
            start_dt = ensure_timezone_aware(datetime.fromisoformat(start_time_iso), calendar_tz)
        except ValueError:
            logger.warning(
                "Could not parse start_time '%s', defaulting to tomorrow 10:00",
                start_time_iso,
            )
            start_dt = _default_start()
    else:
        start_dt = _default_start()

    start_dt = start_dt.astimezone(calendar_tz)
    duration_minutes = max(5, min(int(duration_minutes or 15), 8 * 60))
    return start_dt, start_dt + timedelta(minutes=duration_minutes)


def _create_google_event(
    *,
    title: str,
    description: str,
    start_dt: datetime,
    end_dt: datetime,
    calendar_id: str,
    reminder_minutes: int | None = None,
) -> dict[str, Any]:
    from app.services import google_calendar_service

    if not google_calendar_service.is_configured():
        raise CalendarIntegrationError(
            "Google Calendar не настроен. "
            "Добавьте GOOGLE_CALENDAR_ID и данные сервисного аккаунта в Railway."
        )
    try:
        return google_calendar_service.create_event(
            title=title,
            description=description,
            start_dt=start_dt,
            end_dt=end_dt,
            reminder_minutes=reminder_minutes,
        )
    except google_calendar_service.GoogleCalendarError as exc:
        raise CalendarIntegrationError(str(exc)) from exc


def _icloud_credentials() -> tuple[str, list[str]]:
    username = _clean_env_value(settings.icloud_username)
    passwords = _icloud_password_candidates()
    if not username:
        raise CalendarIntegrationError("ICLOUD_USERNAME не задан в Railway Variables.")
    if not passwords:
        raise CalendarIntegrationError(
            "ICLOUD_APP_SPECIFIC_PASSWORD не задан. Нужен специальный пароль приложения Apple."
        )
    return username, passwords


def _request_icloud(
    method: str,
    url: str,
    *,
    content: bytes | str | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[httpx.Response, str]:
    username, passwords = _icloud_credentials()
    last_auth_error: CalendarIntegrationError | None = None

    for password in passwords:
        current_url = url
        request_headers = {
            "User-Agent": "BuyBringTelegramAssistant/1.0",
            "Accept": "application/xml, text/xml, text/calendar, */*",
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
                    raise CalendarIntegrationError(
                        "iCloud Calendar не ответил вовремя."
                    ) from exc
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

                if response.status_code in {401, 403}:
                    last_auth_error = CalendarIntegrationError(
                        "iCloud отклонил авторизацию. Проверьте ICLOUD_USERNAME и "
                        "специальный пароль приложения."
                    )
                    break
                return response, current_url

        if last_auth_error is not None:
            continue

    if last_auth_error is not None:
        raise last_auth_error
    raise CalendarIntegrationError("Слишком много перенаправлений iCloud CalDAV.")


def _ensure_caldav_success(response: httpx.Response, operation: str) -> None:
    if response.status_code in {200, 201, 204, 207}:
        return
    if response.status_code in {401, 403}:
        raise CalendarIntegrationError(
            "iCloud отклонил авторизацию. Проверьте ICLOUD_USERNAME и специальный пароль приложения."
        )
    preview = response.text.replace("\n", " ")[:300]
    logger.warning(
        "iCloud CalDAV operation failed: operation=%s status=%s url=%s body=%s",
        operation,
        response.status_code,
        str(response.request.url),
        preview or "<empty>",
    )
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


def _extract_property_href(xml_bytes: bytes, property_tag: str) -> str | None:
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
        for href_node in node.findall(f".//{{{DAV_NS}}}href"):
            if href_node.text and href_node.text.strip():
                return href_node.text.strip()
    return None


def _first_property_href(xml_bytes: bytes, property_tag: str) -> str | None:
    return _extract_property_href(xml_bytes, property_tag)


def _caldav_discovery_candidates() -> list[str]:
    candidates: list[str] = []
    for candidate in (
        "https://caldav.icloud.com/",
        "https://caldav.icloud.com/.well-known/caldav",
        _clean_env_value(settings.icloud_caldav_url),
    ):
        if candidate and candidate not in candidates:
            candidates.append(candidate)
    return candidates


def _principal_propfind_bodies() -> list[str]:
    return [
        """<?xml version="1.0" encoding="utf-8" ?>
<d:propfind xmlns:d="DAV:">
  <d:prop><d:current-user-principal /></d:prop>
</d:propfind>""",
        """<?xml version="1.0" encoding="utf-8"?>
<propfind xmlns="DAV:">
  <prop><current-user-principal/></prop>
</propfind>""",
    ]


def _discover_calendar_home() -> str:
    failures: list[str] = []
    principal_href: str | None = None
    final_base_url: str | None = None

    for candidate in _caldav_discovery_candidates():
        for body in _principal_propfind_bodies():
            try:
                principal_response, final_base_url = _propfind(
                    candidate, body, depth=0
                )
                principal_href = _extract_property_href(
                    principal_response,
                    f"{{{DAV_NS}}}current-user-principal",
                )
                if principal_href:
                    break
                failures.append(
                    f"{candidate}: ответ получен, но current-user-principal пустой"
                )
            except CalendarIntegrationError as exc:
                failures.append(f"{candidate}: {exc}")
                logger.warning(
                    "iCloud CalDAV discovery failed for %s: %s", candidate, exc
                )
        if principal_href:
            break
    else:
        details = "\n".join(f"• {item}" for item in failures[:6])
        raise CalendarIntegrationError(
            "iCloud CalDAV не смог определить адрес календарей.\n\n"
            f"{details}\n\n"
            "Проверьте:\n"
            "1. <code>ICLOUD_USERNAME</code> — полный Apple ID (email)\n"
            "2. <code>ICLOUD_APP_SPECIFIC_PASSWORD</code> — новый пароль приложения "
            "с appleid.apple.com (не обычный пароль iCloud)\n"
            "3. Очистите <code>ICLOUD_CALDAV_URL</code> или оставьте "
            "<code>https://caldav.icloud.com/</code>\n"
            "4. На Apple ID должна быть включена двухфакторная аутентификация"
        )

    assert final_base_url is not None
    assert principal_href is not None
    principal_url = _normalize_caldav_url(principal_href, final_base_url)

    home_xml = f"""<?xml version="1.0" encoding="utf-8" ?>
<d:propfind xmlns:d="DAV:" xmlns:c="{CALDAV_NS}">
  <d:prop><c:calendar-home-set /></d:prop>
</d:propfind>"""
    home_response, final_principal_url = _propfind(principal_url, home_xml, depth=0)
    home_href = _extract_property_href(
        home_response,
        f"{{{CALDAV_NS}}}calendar-home-set",
    )
    if not home_href:
        raise CalendarIntegrationError(
            "iCloud не вернул calendar-home-set. "
            "Создайте новый пароль приложения Apple и обновите Railway."
        )
    return _normalize_caldav_url(home_href, final_principal_url)


def list_icloud_calendars() -> list[tuple[str, str]]:
    """Return display names and CalDAV URLs for all iCloud calendars."""
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
            calendars.append((display_name, _normalize_caldav_url(href, final_home_url)))
    return calendars


def diagnose_icloud_calendar() -> dict[str, Any]:
    """Structured iCloud calendar diagnostics for Telegram."""
    username = _clean_env_value(settings.icloud_username)
    masked_user = ""
    if username:
        local, _, domain = username.partition("@")
        masked_user = f"{local[:2]}***@{domain}" if domain else f"{local[:2]}***"

    info: dict[str, Any] = {
        "username_set": bool(username),
        "username_masked": masked_user,
        "password_set": bool(_icloud_password_candidates()),
        "calendar_name": _clean_env_value(settings.icloud_calendar_name) or "—",
        "caldav_url": _clean_env_value(settings.icloud_caldav_url) or "auto",
        "provider": provider_label(),
        "calendars": [],
        "selected_calendar": None,
        "discovery_errors": [],
        "error": None,
    }
    try:
        calendars = list_icloud_calendars()
        info["calendars"] = [name for name, _ in calendars]
        calendar_url, calendar_name = _discover_calendar_url(
            settings.icloud_calendar_name
        )
        info["selected_calendar"] = {
            "name": calendar_name,
            "url": calendar_url,
        }
    except CalendarIntegrationError as exc:
        info["error"] = str(exc)
        if "•" in str(exc):
            info["discovery_errors"] = [
                line.strip("• ").strip()
                for line in str(exc).splitlines()
                if line.strip().startswith("•")
            ]
    return info


def _discover_calendar_url(calendar_name: str | None) -> tuple[str, str]:
    cache_key = (calendar_name or "__default__").casefold()
    cached = _calendar_url_cache.get(cache_key)
    now = datetime.now(tz=timezone.utc).timestamp()
    if cached and now - cached[2] < _CACHE_TTL_SECONDS:
        return cached[0], cached[1]

    direct_url = _clean_env_value(settings.icloud_calendar_url)
    if direct_url:
        normalized = direct_url.rstrip("/") + "/"
        result = (normalized, calendar_name or "Указанный календарь")
        _calendar_url_cache[cache_key] = (result[0], result[1], now)
        return result

    calendars = list_icloud_calendars()

    if not calendars:
        raise CalendarIntegrationError("В аккаунте iCloud не найдено доступных календарей.")

    requested = (calendar_name or "").strip().casefold()
    if requested:
        for name, calendar_url in calendars:
            normalized_name = name.casefold()
            if normalized_name == requested or requested in normalized_name:
                result = (calendar_url.rstrip("/") + "/", name)
                _calendar_url_cache[cache_key] = (result[0], result[1], now)
                return result
        available = ", ".join(name for name, _ in calendars[:10])
        raise CalendarIntegrationError(
            f"Календарь «{calendar_name}» не найден в iCloud. Доступны: {available}"
        )

    result = (calendars[0][1].rstrip("/") + "/", calendars[0][0])
    _calendar_url_cache[cache_key] = (result[0], result[1], now)
    return result


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
    uid, ics = build_ics_content(
        title=title,
        description=description,
        start_dt=start_dt,
        end_dt=end_dt,
    )

    event_url = urljoin(calendar_url, f"{quote(uid, safe='@')}.ics")
    put_headers = {
        "Content-Type": "text/calendar; charset=utf-8",
        "If-None-Match": "*",
    }
    response, _ = _request_icloud(
        "PUT",
        event_url,
        content=ics.encode("utf-8"),
        headers=put_headers,
    )
    if response.status_code in {409, 412, 423}:
        response, _ = _request_icloud(
            "PUT",
            event_url,
            content=ics.encode("utf-8"),
            headers={"Content-Type": "text/calendar; charset=utf-8"},
        )
    _ensure_caldav_success(response, "создание события")
    logger.info(
        "iCloud Calendar event created: uid=%s calendar=%s",
        uid,
        resolved_calendar_name,
    )
    return uid


def test_icloud_connection() -> str:
    """Validate iCloud credentials and calendar discovery."""
    info = diagnose_icloud_calendar()
    if info.get("error"):
        raise CalendarIntegrationError(str(info["error"]))
    selected = info.get("selected_calendar") or {}
    calendars = info.get("calendars") or []
    lines = [
        f"Apple ID: {info.get('username_masked') or '—'}",
        f"Пароль приложения: {'задан' if info.get('password_set') else 'НЕ задан'}",
        f"Искомый календарь: {info.get('calendar_name')}",
        f"Выбран: {selected.get('name') or '—'}",
        f"Всего календарей в iCloud: {len(calendars)}",
    ]
    if calendars:
        lines.append("Список: " + ", ".join(calendars[:12]))
    return "\n".join(lines)


def create_event_with_fallback(
    title: str,
    description: str,
    start_time_iso: str | None,
    duration_minutes: int = 15,
    calendar_id: str | None = None,
    reminder_minutes: int | None = None,
) -> dict[str, Any]:
    """Create a calendar event or return an .ics fallback when provider fails."""
    start_dt, end_dt = _resolve_event_times(start_time_iso, duration_minutes)
    uid, ics_content = build_ics_content(
        title=title,
        description=description,
        start_dt=start_dt,
        end_dt=end_dt,
        uid=None,
        reminder_minutes=reminder_minutes,
    )
    try:
        provider = (settings.calendar_provider or "google").strip().lower()
        if provider == "google":
            created = _create_google_event(
                title=title,
                description=description,
                start_dt=start_dt,
                end_dt=end_dt,
                calendar_id=calendar_id or settings.google_calendar_id,
                reminder_minutes=reminder_minutes,
            )
            return {
                "success": True,
                "event_id": created.get("event_id"),
                "event_url": created.get("event_url"),
                "provider": provider_label(),
                "ics_content": None,
                "error": None,
            }
        event_id = create_event(
            title,
            description,
            start_time_iso,
            duration_minutes=duration_minutes,
            calendar_id=calendar_id,
        )
        return {
            "success": True,
            "event_id": event_id,
            "event_url": None,
            "provider": provider_label(),
            "ics_content": None,
            "error": None,
        }
    except CalendarIntegrationError as exc:
        return {
            "success": False,
            "event_id": uid,
            "event_url": None,
            "provider": provider_label(),
            "ics_content": ics_content,
            "error": str(exc),
        }


async def create_scheduled_event_async(draft: Any) -> dict[str, Any]:
    """Async wrapper for scheduled event creation with .ics fallback."""
    import asyncio

    return await asyncio.to_thread(
        create_event_with_fallback,
        draft.title,
        draft.description,
        draft.start_iso(),
        draft.duration_minutes,
        None,
        draft.reminder_minutes,
    )


def test_google_connection(*, include_write_probe: bool = False) -> str:
    from app.services import google_calendar_service

    info = google_calendar_service.diagnose_google_calendar(
        include_write_probe=include_write_probe
    )
    if info.get("error") and not info.get("read_ok"):
        raise CalendarIntegrationError(str(info["error"]))
    return google_calendar_service.format_diagnostic_report(info)


def _default_start() -> datetime:
    from app.services.calendar_event_builder import manager_timezone

    tomorrow = datetime.now(tz=manager_timezone()) + timedelta(days=1)
    return tomorrow.replace(hour=10, minute=0, second=0, microsecond=0)
