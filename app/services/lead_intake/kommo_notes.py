"""Readable Kommo note/task rendering with duplicate-protection markers.

The note format follows the lead-intake contract exactly (client card, a
single translated product line, original raw value, budget, a "ЛИЧНЫЙ
АНАЛИЗ" section, risks, missing information, next action and the task) —
never raw JSON. A machine-readable marker is appended on its own final line
so a retry never creates a second note for the same lead.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from app.services.lead_intake.business_hours import now as intake_now
from app.services.lead_intake.matching import LeadSnapshot
from app.services.lead_intake.schema import LeadQualification

NOTE_MARKER_PREFIX = "AUTO_LEAD_ANALYSIS_V"
TASK_MARKER_PREFIX = "AUTO_LEAD_TASK_V"

_POTENTIAL_RU = {"high": "высокий", "medium": "средний", "low": "низкий", "unknown": "не определён"}
_READINESS_RU = {"high": "высокая", "medium": "средняя", "low": "низкая", "unknown": "не определена"}
_ACTION_RU = {
    "whatsapp": "WhatsApp",
    "phone_call": "телефонный звонок",
    "email": "email",
    "manual_review": "ручная проверка",
}


def note_marker(kommo_lead_id: int, version: int) -> str:
    return f"[{NOTE_MARKER_PREFIX}{version}:{kommo_lead_id}]"


def task_marker(kommo_lead_id: int, version: int, *, kind: str = "primary") -> str:
    return f"[{TASK_MARKER_PREFIX}{version}:{kommo_lead_id}:{kind}]"


def note_has_marker(note_text: str | None, marker: str) -> bool:
    return marker in str(note_text or "")


def task_has_marker(task_text: str | None, marker: str) -> bool:
    return marker in str(task_text or "")


def _clean(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()


def build_kommo_note(
    *,
    snapshot: LeadSnapshot,
    qualification: LeadQualification,
    kommo_lead_id: int,
    processing_version: int,
    phone_display: str | None,
    generated_at: datetime | None = None,
) -> str:
    moment = generated_at or intake_now()
    action_label = _ACTION_RU.get(qualification.recommended_action, qualification.recommended_action)

    lines: list[str] = [
        f"{moment.strftime('%d.%m.%Y')} — новая заявка / {action_label}",
        "",
        f"Клиент: {_clean(snapshot.name) or 'не указан'}",
        f"Телефон: {phone_display or _clean(snapshot.phone) or 'не указан'}",
        f"Email: {_clean(snapshot.email) or 'не указан'}",
        f"Регион: {_clean(snapshot.region) or 'не указан'}",
        "",
        "Товар:",
        qualification.product_name_ru,
        "",
        "Исходное значение:",
        _clean(snapshot.product) or "не указано",
    ]

    lines.extend(["", "О ЧЁМ ЗАЯВКА / ЛИЧНЫЙ АНАЛИЗ", "", qualification.lead_analysis_ru, ""])
    lines.append(f"Потенциал: {_POTENTIAL_RU.get(qualification.potential, qualification.potential)}")
    lines.append(f"Готовность: {_READINESS_RU.get(qualification.readiness, qualification.readiness)}")
    lines.append(f"Приоритет: {qualification.priority} — {qualification.priority_label_ru}")

    if qualification.call_script:
        script = qualification.call_script
        lines.extend(["", "ЦЕЛЬ ЗВОНКА", "", _clean(script.objective) or "Квалифицировать запрос"])
        if script.questions:
            lines.extend(["", "О ЧЁМ ГОВОРИТЬ", ""])
            lines.extend(f"– {question}" for question in script.questions[:10] if _clean(question))
        if script.opening_phrase:
            lines.extend(["", "ОТКРЫТИЕ (язык клиента)", "", script.opening_phrase])

    if qualification.main_risks_ru:
        lines.extend(["", "ОСНОВНЫЕ РИСКИ", ""])
        lines.extend(f"– {risk};" for risk in qualification.main_risks_ru)

    if qualification.missing_information_ru:
        lines.extend(["", "ЧТО НУЖНО ПОЛУЧИТЬ", ""])
        lines.extend(f"– {item};" for item in qualification.missing_information_ru)

    if qualification.next_steps_ru and not qualification.call_script:
        lines.extend(["", "О ЧЁМ ГОВОРИТЬ / СЛЕДУЮЩИЕ ШАГИ", ""])
        lines.extend(f"– {step}" for step in qualification.next_steps_ru[:8] if _clean(step))

    lines.extend(
        [
            "",
            "СЛЕДУЮЩЕЕ ДЕЙСТВИЕ",
            "",
            qualification.recommended_action_reason_ru,
        ]
    )

    lines.extend(["", "ЗАДАЧА", "", qualification.task.title_ru])

    lines.extend(["", note_marker(kommo_lead_id, processing_version)])
    return "\n".join(lines)[:14_000]


def build_call_result_note(*, outcome: str, details: str | None = None) -> str:
    label = {
        "connected": "Поговорили",
        "no_answer": "Не ответил",
        "rescheduled": "Перенесено",
        "wrong_number": "Номер неверный",
    }.get(outcome, outcome)
    lines = [f"Результат звонка: {label}"]
    if details:
        lines.append(_clean(details))
    return "\n".join(lines)[:4000]
