"""Build calendar event titles, descriptions, and parse natural-language schedules."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from app.config import get_settings

settings = get_settings()

EVENT_TYPE_LABELS = {
    "call": "Созвон с клиентом",
    "meeting": "Встреча с клиентом",
    "message": "Написать клиенту",
    "proposal": "Отправить предложение",
    "other": "Другое",
}

EVENT_TYPE_PREFIX = {
    "call": "Созвон",
    "meeting": "Встреча",
    "message": "Написать",
    "proposal": "Предложение",
    "other": "Событие",
}

CALENDAR_EVENT_TYPES = {"call", "meeting"}
TASK_ONLY_EVENT_TYPES = {"message", "proposal"}


@dataclass(frozen=True)
class ScheduledEventDraft:
    event_type: str
    title: str
    description: str
    start_at: datetime
    duration_minutes: int
    reminder_minutes: int
    kommo_lead_id: int | None = None
    lead_name: str | None = None
    lead_url: str | None = None
    contact_name: str | None = None
    contact_phone: str | None = None
    contact_email: str | None = None
    product_hint: str | None = None
    notes: str | None = None
    discussion_points: list[str] | None = None

    @property
    def end_at(self) -> datetime:
        return self.start_at + timedelta(minutes=self.duration_minutes)

    @property
    def needs_calendar_event(self) -> bool:
        return self.event_type in CALENDAR_EVENT_TYPES

    @property
    def needs_kommo_task(self) -> bool:
        return self.event_type in CALENDAR_EVENT_TYPES or self.event_type in TASK_ONLY_EVENT_TYPES

    def start_iso(self) -> str:
        return self.start_at.isoformat()

    def end_iso(self) -> str:
        return self.end_at.isoformat()


def ensure_timezone_aware(dt: datetime, tz: ZoneInfo | None = None) -> datetime:
    if dt.tzinfo is not None:
        return dt
    return dt.replace(tzinfo=tz or manager_timezone())


def manager_timezone() -> ZoneInfo:
    try:
        return ZoneInfo(settings.google_calendar_timezone or settings.manager_timezone)
    except Exception:
        return ZoneInfo("Europe/Warsaw")


def default_duration_minutes() -> int:
    return max(5, int(settings.google_calendar_default_duration_minutes or 30))


def default_reminder_minutes() -> int:
    return max(0, int(settings.google_calendar_default_reminder_minutes or 30))


def build_event_title(event_type: str, lead_name: str | None) -> str:
    prefix = EVENT_TYPE_PREFIX.get(event_type, "Событие")
    clean_lead = (lead_name or "").strip()
    if clean_lead:
        return f"{prefix}: {clean_lead}"[:255]
    return prefix[:255]


def build_event_description(
    *,
    lead_name: str | None,
    kommo_lead_id: int | None,
    lead_url: str | None,
    contact_name: str | None = None,
    contact_phone: str | None = None,
    contact_email: str | None = None,
    product_hint: str | None = None,
    notes: str | None = None,
    discussion_points: list[str] | None = None,
) -> str:
    lines = [
        f"Сделка: {lead_name or '—'}",
        f"Kommo ID: {kommo_lead_id or '—'}",
        f"Клиент: {contact_name or '—'}",
        f"Телефон: {contact_phone or '—'}",
    ]
    if contact_email:
        lines.append(f"Email: {contact_email}")
    if product_hint:
        lines.append(f"Запрос: {product_hint}")
    if notes:
        lines.append("")
        lines.append("Последний разговор:")
        lines.append(notes[:1200])
    if discussion_points:
        lines.append("")
        lines.append("Что нужно обсудить:")
        for point in discussion_points[:8]:
            clean = str(point).strip()
            if clean:
                lines.append(f"– {clean}")
    if lead_url:
        lines.extend(["", "Kommo:", lead_url])
    lines.append("")
    lines.append("Создано через Telegram BBS Assistant.")
    return "\n".join(lines)


def format_date_ru(value: datetime) -> str:
    months = {
        1: "января",
        2: "февраля",
        3: "марта",
        4: "апреля",
        5: "мая",
        6: "июня",
        7: "июля",
        8: "августа",
        9: "сентября",
        10: "октября",
        11: "ноября",
        12: "декабря",
    }
    local = value.astimezone(manager_timezone())
    return f"{local.day} {months[local.month]} {local.year}"


def format_time_range(start: datetime, end: datetime) -> str:
    tz = manager_timezone()
    start_local = start.astimezone(tz)
    end_local = end.astimezone(tz)
    return (
        f"{format_date_ru(start_local)}, "
        f"{start_local.strftime('%H:%M')}–{end_local.strftime('%H:%M')}"
    )


def format_reminder_label(minutes: int) -> str:
    if minutes <= 0:
        return "без напоминания"
    if minutes == 10:
        return "за 10 минут"
    if minutes == 30:
        return "за 30 минут"
    if minutes == 60:
        return "за 1 час"
    if minutes == 1440:
        return "за 1 день"
    return f"за {minutes} минут"


def build_idempotency_key(
    *,
    telegram_user_id: int,
    source_id: str,
    kommo_lead_id: int | None,
    event_type: str,
    start_iso: str,
) -> str:
    raw = f"{telegram_user_id}:{source_id}:{kommo_lead_id or 0}:{event_type}:{start_iso}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


_WEEKDAYS = {
    "понедельник": 0,
    "вторник": 1,
    "среда": 2,
    "среду": 2,
    "четверг": 3,
    "пятница": 4,
    "пятницу": 4,
    "суббота": 5,
    "субботу": 5,
    "воскресенье": 6,
}


def parse_natural_datetime(
    text: str,
    *,
    now: datetime | None = None,
    duration_minutes: int | None = None,
) -> tuple[datetime, int]:
    """Parse Russian/Polish-ish schedule phrases. Returns (start_at, duration_minutes)."""
    raw = " ".join((text or "").strip().split())
    if not raw:
        raise ValueError("Дата и время не указаны.")

    tz = manager_timezone()
    now = now or datetime.now(tz=tz)
    lowered = raw.casefold().replace("ё", "е")
    duration = duration_minutes if duration_minutes is not None else default_duration_minutes()

    duration_match = re.search(
        r"(?:на|длительностью|длительность)\s+(\d{1,3})\s*(?:минуты|минуту|минут|мин)",
        lowered,
    )
    if duration_match:
        duration = int(duration_match.group(1))
        raw = re.sub(
            r"(?:на|длительностью|длительность)\s+\d{1,3}\s*(?:минуты|минуту|минут|мин)",
            "",
            raw,
            flags=re.I,
        ).strip()
        lowered = raw.casefold().replace("ё", "е")

    hour_match = re.search(r"на\s+(\d{1,2})\s*(?:час|часа|часов)", lowered)
    if hour_match:
        duration = int(hour_match.group(1)) * 60

    if "через два часа" in lowered or "через 2 часа" in lowered:
        start = now + timedelta(hours=2)
        if start <= now:
            raise ValueError("Дата и время должны быть в будущем.")
        return start, duration

    if "через час" in lowered:
        start = now + timedelta(hours=1)
        return start, duration

    for prefix, days in (("сегодня", 0), ("завтра", 1), ("послезавтра", 2)):
        if prefix in lowered:
            match = re.search(rf"{prefix}(.*)$", lowered)
            time_part = (match.group(1) if match else "").strip(" ,в")
            time_part = time_part.replace("в ", "").strip()
            if not time_part:
                raise ValueError(f"Укажите время после слова «{prefix}».")
            parsed_time = _parse_time_fragment(time_part)
            target = (now + timedelta(days=days)).date()
            start = datetime.combine(target, parsed_time, tzinfo=tz)
            if start <= now:
                raise ValueError("Дата и время должны быть в будущем.")
            return start, duration

    for word, weekday in _WEEKDAYS.items():
        if word in lowered:
            time_part = re.sub(rf".*?{word}", "", lowered).strip(" ,в")
            time_part = time_part.replace("в ", "").strip()
            if not time_part:
                raise ValueError("Укажите время после дня недели.")
            parsed_time = _parse_time_fragment(time_part)
            days_ahead = (weekday - now.weekday()) % 7
            if days_ahead == 0:
                days_ahead = 7
            target = (now + timedelta(days=days_ahead)).date()
            start = datetime.combine(target, parsed_time, tzinfo=tz)
            if start <= now:
                start += timedelta(days=7)
            return start, duration

    for fmt in ("%d.%m.%Y %H:%M", "%Y-%m-%d %H:%M", "%d.%m %H:%M", "%d.%m.%Y %H:%M:%S"):
        try:
            parsed = datetime.strptime(raw, fmt)
        except ValueError:
            continue
        if fmt == "%d.%m %H:%M":
            parsed = parsed.replace(year=now.year)
            candidate = parsed.replace(tzinfo=tz)
            if candidate <= now:
                candidate = candidate.replace(year=now.year + 1)
            parsed = candidate.replace(tzinfo=None)
        start = parsed.replace(tzinfo=tz)
        if start <= now:
            raise ValueError("Дата и время должны быть в будущем.")
        return start, duration

    time_only_match = re.fullmatch(r"\d{1,2}(:\d{2})?", raw.strip())
    if time_only_match:
        parsed_time = _parse_time_fragment(raw)
        start = datetime.combine(now.date(), parsed_time, tzinfo=tz)
        if start <= now:
            start += timedelta(days=1)
        return start, duration

    raise ValueError(
        "Не удалось распознать дату. Пример: завтра в 10:00, встреча в среду в 15:30."
    )


def _parse_time_fragment(value: str) -> Any:
    clean = value.strip().lower().replace("в ", "")
    clean = re.sub(r"\s+.*$", "", clean)
    if re.fullmatch(r"\d{1,2}:\d{2}", clean):
        hour, minute = clean.split(":", 1)
        return datetime.strptime(f"{hour}:{minute}", "%H:%M").time()
    if re.fullmatch(r"\d{1,2}", clean):
        return datetime.strptime(clean, "%H").time()
    word_map = {
        "десять": "10:00",
        "одиннадцать": "11:00",
        "двенадцать": "12:00",
        "тринадцать": "13:00",
        "четырнадцать": "14:00",
        "пятнадцать": "15:00",
        "шестнадцать": "16:00",
        "девять": "09:00",
    }
    for word, mapped in word_map.items():
        if word in clean:
            return datetime.strptime(mapped, "%H:%M").time()
    raise ValueError("Не удалось распознать время.")


def quick_datetime(choice: str, *, now: datetime | None = None, base_date: datetime | None = None) -> datetime:
    tz = manager_timezone()
    now = now or datetime.now(tz=tz)
    base = (base_date or now).astimezone(tz)
    day_offsets = {
        "today": 0,
        "tomorrow": 1,
        "dayafter": 2,
        "today17": 0,
        "tomorrow10": 1,
        "tomorrow15": 1,
    }
    time_map = {
        "today17": (17, 0),
        "tomorrow10": (10, 0),
        "tomorrow15": (15, 0),
        "time09": (9, 0),
        "time10": (10, 0),
        "time11": (11, 0),
        "time12": (12, 0),
        "time14": (14, 0),
        "time15": (15, 0),
        "time16": (16, 0),
    }
    if choice in time_map:
        hour, minute = time_map[choice]
        target = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        return target
    if choice in day_offsets:
        days = day_offsets[choice]
        hour, minute = (10, 0) if choice != "today17" else (17, 0)
        target = (base + timedelta(days=days)).replace(
            hour=hour, minute=minute, second=0, microsecond=0
        )
        if target <= now:
            target += timedelta(days=1)
        return target
    raise ValueError("Неизвестный быстрый срок.")


def draft_from_lead_details(
    *,
    event_type: str,
    lead_details: dict[str, Any],
    start_at: datetime,
    duration_minutes: int,
    reminder_minutes: int,
    custom_title: str | None = None,
) -> ScheduledEventDraft:
    contacts = lead_details.get("contacts") or []
    contact = contacts[0] if contacts else {}
    phones = contact.get("phones") or []
    emails = contact.get("emails") or []
    lead_name = str(lead_details.get("name") or "—")
    title = custom_title or build_event_title(event_type, lead_name)
    description = build_event_description(
        lead_name=lead_name,
        kommo_lead_id=int(lead_details.get("id") or 0) or None,
        lead_url=str(lead_details.get("url") or "") or None,
        contact_name=str(contact.get("name") or "") or None,
        contact_phone=", ".join(phones) if phones else None,
        contact_email=", ".join(emails) if emails else None,
        product_hint=lead_name,
    )
    return ScheduledEventDraft(
        event_type=event_type,
        title=title,
        description=description,
        start_at=ensure_timezone_aware(start_at).astimezone(manager_timezone()),
        duration_minutes=duration_minutes,
        reminder_minutes=reminder_minutes,
        kommo_lead_id=int(lead_details.get("id") or 0) or None,
        lead_name=lead_name,
        lead_url=str(lead_details.get("url") or "") or None,
        contact_name=str(contact.get("name") or "") or None,
        contact_phone=", ".join(phones) if phones else None,
        contact_email=", ".join(emails) if emails else None,
        product_hint=lead_name,
    )
