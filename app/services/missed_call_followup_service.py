"""Missed-call follow-up tasks for the Poland pipeline.

When a lead moves to ``НЕДОЗВОН`` (or a call result is recorded as no-answer):
* 1st missed call today → create a Kommo task to call again today;
* 2nd+ missed call today → create a Kommo task to call tomorrow.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from app.config import get_settings
from app.services import kommo_service
from app.services.lead_intake import business_hours

logger = logging.getLogger(__name__)
settings = get_settings()

_MISSED_STATUS_TOKENS = (
    "недозвон",
    "не дозвон",
    "no answer",
    "no_answer",
    "missed call",
    "nie odebrał",
    "nie odebral",
)
_MISSED_NOTE_RE = re.compile(
    r"(недозвон|не\s+ответил|результат\s+звонка:\s*не\s+ответил|"
    r"no[_ ]?answer|missed\s*call|bbs-missed-call)",
    re.IGNORECASE,
)
_MARKER_RE = re.compile(
    r"\[BBS-MISSED-CALL:(?P<day>\d{4}-\d{2}-\d{2}):(?P<n>\d+)\]",
    re.IGNORECASE,
)


def _tz() -> ZoneInfo:
    try:
        return business_hours.timezone()
    except Exception:
        return ZoneInfo(settings.manager_timezone or "Europe/Warsaw")


def is_missed_call_status(name: str | None) -> bool:
    lowered = " ".join(str(name or "").casefold().split())
    return any(token in lowered for token in _MISSED_STATUS_TOKENS)


def _today_key(moment: datetime | None = None) -> str:
    return (moment or datetime.now(_tz())).strftime("%Y-%m-%d")


def _note_day(note: dict[str, Any]) -> str | None:
    raw = note.get("created_at") or note.get("updated_at")
    if raw is None:
        return None
    try:
        if isinstance(raw, (int, float)):
            dt = datetime.fromtimestamp(int(raw), tz=_tz())
        else:
            dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00")).astimezone(_tz())
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return None


def count_missed_attempts_today(notes: list[dict[str, Any]], *, day: str | None = None) -> int:
    """Count already-recorded missed-call notes for the manager's local day."""
    wanted = day or _today_key()
    marked_max = 0
    unmarked = 0
    for note in notes:
        text = str(note.get("text") or "")
        match = _MARKER_RE.search(text)
        if match and match.group("day") == wanted:
            marked_max = max(marked_max, int(match.group("n")))
            continue
        if _note_day(note) == wanted and _MISSED_NOTE_RE.search(text):
            unmarked += 1
    return max(marked_max, unmarked)


def _due_timestamp(*, attempt: int, now: datetime | None = None) -> int:
    moment = now or datetime.now(_tz())
    start, end = business_hours.business_window()
    if attempt <= 1:
        due = moment + timedelta(hours=3)
        end_cap = moment.replace(
            hour=end.hour, minute=end.minute, second=0, microsecond=0
        )
        if due.date() == moment.date() and due > end_cap:
            due = max(moment + timedelta(minutes=45), end_cap - timedelta(minutes=15))
        if due <= moment:
            due = moment + timedelta(minutes=45)
        return int(due.timestamp())

    due = (moment + timedelta(days=1)).replace(
        hour=start.hour, minute=start.minute, second=0, microsecond=0
    )
    while due.weekday() >= 5:
        due += timedelta(days=1)
    if due <= moment:
        due = moment + timedelta(hours=20)
    return int(due.timestamp())


def _task_already_exists(tasks: list[dict[str, Any]], *, day: str, attempt: int) -> bool:
    needle = f"[BBS-MISSED-CALL:{day}:{attempt}]"
    for task in tasks:
        if needle in str(task.get("text") or ""):
            return True
    return False


async def handle_missed_call(
    lead_id: int,
    *,
    source: str = "status",
    previous_status: str | None = None,
) -> dict[str, Any]:
    """Record a missed-call attempt and create the matching follow-up task."""
    notes = await kommo_service.get_recent_common_notes(lead_id, limit=50)
    day = _today_key()
    attempt = count_missed_attempts_today(notes, day=day) + 1
    marker = f"[BBS-MISSED-CALL:{day}:{attempt}]"
    when = datetime.now(_tz()).strftime("%d.%m.%Y %H:%M")
    note_lines = [
        f"Недозвон · попытка {attempt} за {day}",
        f"Источник: {source}",
        f"Время: {when}",
    ]
    if previous_status:
        note_lines.append(f"Предыдущий статус: {previous_status}")
    if attempt == 1:
        note_lines.append("Следующий шаг: позвонить ещё раз сегодня.")
    else:
        note_lines.append("Следующий шаг: позвонить завтра.")
    note_lines.extend(["", marker])
    await kommo_service.add_common_note(lead_id, "\n".join(note_lines)[:4000])

    tasks = await kommo_service.get_open_lead_tasks(lead_id, limit=50)
    if _task_already_exists(tasks, day=day, attempt=attempt):
        return {
            "attempt": attempt,
            "task_created": False,
            "reason": "task_already_exists",
            "due_rule": "today" if attempt == 1 else "tomorrow",
        }

    due_at = _due_timestamp(attempt=attempt)
    if attempt == 1:
        task_text = f"Позвонить еще раз сегодня · попытка {attempt} {marker}"
    else:
        task_text = f"Позвонить завтра · после {attempt}-го недозвона сегодня {marker}"

    created = await kommo_service.create_lead_task(
        lead_id=lead_id,
        text=task_text,
        complete_till=due_at,
    )
    logger.info(
        "missed_call_followup lead_id=%s attempt=%s due_rule=%s task_id=%s",
        lead_id,
        attempt,
        "today" if attempt == 1 else "tomorrow",
        created.get("task_id"),
    )
    return {
        "attempt": attempt,
        "task_created": True,
        "task_id": created.get("task_id"),
        "due_at": due_at,
        "due_rule": "today" if attempt == 1 else "tomorrow",
        "task_text": task_text,
    }
