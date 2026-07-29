"""Turn a spoken project update into independently confirmable operations."""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from app.agent import actions
from app.agent.lead_refs import extract_internal_lead_number


@dataclass(frozen=True)
class ProjectUpdateProposal:
    summary: str
    note_text: str
    task_text: str | None
    due_at: str | None
    next_step: str
    should_prepare_followup: bool


def _strip_project_reference(text: str) -> str:
    clean = " ".join(str(text or "").strip().split())
    clean = re.sub(
        r"^(?:по\s+)?проект[уае]?\s*[№#]?\s*\d{1,12}\s*[,.:—-]*\s*",
        "",
        clean,
        flags=re.I,
    )
    return clean.strip()


def _extract_due(text: str) -> str | None:
    lowered = text.casefold().replace("ё", "е")
    explicit = re.search(
        r"\b(сегодня|завтра|послезавтра|понедельник|вторник|среду|четверг|пятницу|субботу|воскресенье)"
        r"(?:\s+в)?\s+(\d{1,2}(?::\d{2})?)\b",
        lowered,
    )
    if explicit:
        return f"{explicit.group(1)} в {explicit.group(2)}"
    patterns = (
        r"\bдо\s+(понедельник[а-я]*|вторник[а-я]*|сред[а-я]*|четверг[а-я]*|пятниц[а-я]*|суббот[а-я]*|воскресень[а-я]*)\b",
        r"\b(сегодня|завтра|послезавтра)\b",
        r"\bдо\s+(\d{1,2}\.\d{1,2}(?:\.\d{2,4})?)\b",
    )
    for pattern in patterns:
        match = re.search(pattern, lowered)
        if not match:
            continue
        value = match.group(1)
        weekday_forms = {
            "понедель": "понедельник",
            "вторник": "вторник",
            "сред": "среду",
            "четверг": "четверг",
            "пятниц": "пятницу",
            "суббот": "субботу",
            "воскрес": "воскресенье",
        }
        for prefix, normalized in weekday_forms.items():
            if value.startswith(prefix):
                return f"{normalized} в 17:00"
        if value in {"сегодня", "завтра", "послезавтра"}:
            return f"{value} в 17:00"
        if re.fullmatch(r"\d{1,2}\.\d{1,2}(?:\.\d{2,4})?", value):
            if value.count(".") == 1:
                return f"{value} 17:00"
            return f"{value} 17:00"
    absolute = re.search(
        r"\b(\d{1,2}\.\d{1,2}(?:\.\d{2,4})?\s+\d{1,2}:\d{2})\b",
        lowered,
    )
    return absolute.group(1) if absolute else None


def analyse_update(text: str) -> ProjectUpdateProposal:
    clean = _strip_project_reference(text)
    if not clean:
        raise ValueError("Обновление проекта пустое.")
    due_at = _extract_due(clean)
    sentences = [
        part.strip(" .")
        for part in re.split(r"(?<=[.!?])\s+|;\s*", clean)
        if part.strip(" .")
    ]
    task_sentence = next(
        (
            sentence
            for sentence in reversed(sentences)
            if re.search(
                r"\b(подготов|сдела|отправ|провер|связа|позвон|написа|рассчита|уточни)",
                sentence,
                flags=re.I,
            )
            and (
                due_at is not None
                or re.search(r"\b(нужно|надо|следующ|задач)", sentence, flags=re.I)
            )
        ),
        None,
    )
    task_text = None
    if task_sentence:
        task_text = re.sub(
            r"\bдо\s+(?:понедельник[а-я]*|вторник[а-я]*|сред[а-я]*|четверг[а-я]*|пятниц[а-я]*|"
            r"суббот[а-я]*|воскресень[а-я]*|сегодня|завтра|послезавтра|\d{1,2}\.\d{1,2}(?:\.\d{2,4})?)\b",
            "",
            task_sentence,
            flags=re.I,
        ).strip(" ,.;—-")
    summary = clean[:4000]
    next_step = task_text or (
        "Зафиксировать конкретное следующее действие по результатам разговора"
    )
    should_followup = bool(
        re.search(
            r"\b(поговорил|разговор|созвон|клиент|обсудил|договорились|переговор)",
            clean,
            flags=re.I,
        )
    )
    return ProjectUpdateProposal(
        summary=summary,
        note_text=summary,
        task_text=task_text,
        due_at=due_at,
        next_step=next_step,
        should_prepare_followup=should_followup,
    )


def _lead_payload(lead: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": int(lead["id"]),
        "name": lead.get("name"),
        "url": lead.get("url"),
        "updated_at": lead.get("updated_at"),
        "responsible_user_id": lead.get("responsible_user_id"),
        "contacts": (lead.get("contacts") or [])[:2],
    }


async def stage_bundle(
    db: AsyncSession,
    *,
    chat_id: int,
    telegram_user_id: int,
    lead: dict[str, Any],
    proposal: ProjectUpdateProposal,
    followup_draft: dict[str, Any] | None,
    language_source: str | None,
    client_id: int | None,
) -> tuple[list[Any], str]:
    group_id = uuid4().hex
    label = (
        f"№{extract_internal_lead_number(lead)} — {lead.get('name')}"
        if extract_internal_lead_number(lead)
        else str(lead.get("name") or lead.get("id"))
    )
    definitions: list[tuple[str, str, dict[str, Any]]] = [
        (
            "add_kommo_note",
            "Заметка в Kommo",
            {
                "lead_id": int(lead["id"]),
                "kommo_lead_id": int(lead["id"]),
                "note_text": proposal.note_text,
            },
        ),
        (
            "create_notion_communication",
            "Переговоры в Notion",
            {
                "lead": _lead_payload(lead),
                "kommo_lead_id": int(lead["id"]),
                "summary": proposal.summary,
                "full_text": proposal.note_text,
                "channel": "Голос",
            },
        ),
    ]
    if proposal.task_text and proposal.due_at:
        definitions.append(
            (
                "create_project_task",
                "Задача Kommo + Notion",
                {
                    "lead": _lead_payload(lead),
                    "kommo_lead_id": int(lead["id"]),
                    "task_text": proposal.task_text,
                    "due_at": proposal.due_at,
                },
            )
        )
    definitions.append(
        (
            "update_project_next_step",
            "Следующий шаг",
            {
                "lead": _lead_payload(lead),
                "kommo_lead_id": int(lead["id"]),
                "next_step": proposal.next_step,
                "due_at": proposal.due_at,
            },
        )
    )
    if followup_draft:
        definitions.append(
            (
                "prepare_client_followup",
                "Польский/украинский follow-up",
                {
                    "lead": _lead_payload(lead),
                    "kommo_lead_id": int(lead["id"]),
                    "draft": followup_draft,
                    "language_source": language_source,
                    "client_id": client_id,
                },
            )
        )

    staged: list[Any] = []
    for action_type, title, payload in definitions:
        action = await actions.stage_action(
            db,
            telegram_user_id=telegram_user_id,
            chat_id=chat_id,
            action_type=action_type,
            payload=payload,
            preview_text=f"{title}\n{label}",
            batch_group_id=group_id,
        )
        staged.append(action)
    return staged, group_id


def format_bundle(
    *,
    lead: dict[str, Any],
    proposal: ProjectUpdateProposal,
    actions_list: list[Any],
    followup_draft: dict[str, Any] | None,
) -> str:
    lines = [
        "<b>🎙 Обновление проекта готово к подтверждению</b>",
        "",
        f"Проект: <b>{html.escape(str(lead.get('name') or lead.get('id')))}</b>",
        "",
        "<b>Резюме</b>",
        html.escape(proposal.summary[:1000]),
        "",
        "<b>Предлагаемые изменения</b>",
    ]
    labels = {
        "add_kommo_note": "Заметка переговоров в Kommo",
        "create_notion_communication": "Коммуникация в Notion",
        "create_project_task": (
            f"Задача: {proposal.task_text} · {proposal.due_at}"
        ),
        "update_project_next_step": f"Следующий шаг: {proposal.next_step}",
        "prepare_client_followup": (
            f"Follow-up {str((followup_draft or {}).get('language') or '').upper()}"
        ),
    }
    for action in actions_list:
        lines.append(
            f"• {html.escape(labels.get(action.action_type, action.action_type))}"
        )
    lines.extend(
        [
            "",
            "Можно подтвердить каждый пункт отдельно или выполнить весь пакет.",
        ]
    )
    return "\n".join(lines)[:4000]


def bundle_markup(actions_list: list[Any], group_id: str) -> dict[str, Any]:
    labels = {
        "add_kommo_note": "📝 Kommo",
        "create_notion_communication": "📓 Notion",
        "create_project_task": "✅ Задача",
        "update_project_next_step": "➡️ Следующий шаг",
        "prepare_client_followup": "✍️ Follow-up",
    }
    rows = [
        [
            {
                "text": labels.get(action.action_type, action.action_type)[:64],
                "callback_data": f"agent:ok:{action.id}",
            }
        ]
        for action in actions_list
    ]
    rows.append(
        [
            {
                "text": "✅ Подтвердить всё",
                "callback_data": f"agent:bundle:{group_id}:all",
            },
            {
                "text": "❌ Отменить всё",
                "callback_data": f"agent:bundle:{group_id}:no",
            },
        ]
    )
    return {"inline_keyboard": rows}
