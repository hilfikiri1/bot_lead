"""Next-action engine, stale detection and operational inbox for Agent v5."""

from __future__ import annotations

import asyncio
import html
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.config import get_settings
from app.models.agent_v5 import NextActionState
from app.services import ai_analysis_service, kommo_service

settings = get_settings()
logger = logging.getLogger(__name__)


@dataclass
class NextActionView:
    kommo_lead_id: int
    internal_number: str | None
    name: str
    status: str
    waiting_on: str | None
    action_text: str | None
    due_at: datetime | None
    stale_reason: str | None
    recommended_action: str | None
    action_reason: str | None = None
    suggested_message: str | None = None
    age_days: int = 0
    category: str = "without_next"


@dataclass
class InboxResult:
    overdue: list[NextActionView] = field(default_factory=list)
    waiting_us: list[NextActionView] = field(default_factory=list)
    waiting_client: list[NextActionView] = field(default_factory=list)
    without_next: list[NextActionView] = field(default_factory=list)
    stale: list[NextActionView] = field(default_factory=list)
    document_review: list[NextActionView] = field(default_factory=list)
    integration_errors: list[NextActionView] = field(default_factory=list)
    ready: list[NextActionView] = field(default_factory=list)


def stale_threshold_days(*, pipeline: str | None = None, grade: str | None = None) -> int:
    configured = int(getattr(settings, "agent_stale_days_default", 7) or 7)
    if grade == "A":
        return max(2, configured // 2)
    if grade == "C":
        return configured * 2
    _ = pipeline
    return configured


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def evaluate_lead_next_action(
    lead: dict[str, Any],
    *,
    now: datetime | None = None,
    grade: str | None = None,
    waiting_on: str | None = None,
    action_text: str | None = None,
    stored_due_at: datetime | None = None,
    stored_stale_reason: str | None = None,
) -> NextActionView:
    now = now or datetime.now(timezone.utc)
    now_ts = int(now.timestamp())
    closest = lead.get("closest_task_at")
    updated_at = int(lead.get("updated_at") or lead.get("created_at") or 0)
    age_days = max(0, (now_ts - updated_at) // 86_400) if updated_at else 999
    internal = None
    from app.agent.lead_refs import extract_internal_lead_number

    try:
        internal = extract_internal_lead_number(lead)
    except Exception:
        internal = None

    due_at = _aware(stored_due_at)
    status = "ok"
    stale_reason = stored_stale_reason
    category = "ready"
    recommended = action_text

    if waiting_on == "us":
        status = "waiting_us"
        category = "waiting_us"
        recommended = recommended or "Ответить клиенту"
    elif waiting_on == "client":
        status = "waiting_client"
        category = "waiting_client"
        recommended = recommended or "Дождаться ответа клиента"

    effective_due_ts = None
    if due_at is not None:
        effective_due_ts = int(due_at.timestamp())
    elif isinstance(closest, (int, float)):
        effective_due_ts = int(closest)
        due_at = datetime.fromtimestamp(effective_due_ts, tz=timezone.utc)

    if effective_due_ts is not None and effective_due_ts < now_ts:
        status = "overdue"
        category = "overdue"
        stale_reason = stale_reason or (
            "клиент не ответил к сроку follow-up"
            if waiting_on == "client"
            else "задача просрочена"
        )
        recommended = recommended or (
            "Подготовить follow-up"
            if waiting_on == "client"
            else "Закрыть или перенести просроченную задачу"
        )
    elif closest is None and not action_text and waiting_on not in {"us", "client"}:
        status = "missing"
        category = "without_next"
        stale_reason = "нет следующей задачи"
        recommended = "Создать следующее действие"
    elif effective_due_ts is not None and not action_text:
        recommended = "Выполнить задачу в срок"

    threshold = stale_threshold_days(pipeline=str(lead.get("pipeline_name") or ""), grade=grade)
    if age_days >= threshold and status not in {"overdue"}:
        if status == "ok":
            status = "stale"
            category = "stale"
        stale_reason = stale_reason or f"нет контакта {age_days} дн."
        recommended = recommended or "Связаться с клиентом"

    return NextActionView(
        kommo_lead_id=int(lead.get("id") or 0),
        internal_number=internal,
        name=str(lead.get("name") or lead.get("id") or "—"),
        status=status,
        waiting_on=waiting_on,
        action_text=action_text,
        due_at=due_at,
        stale_reason=stale_reason,
        recommended_action=recommended,
        age_days=age_days,
        category=category,
    )


async def upsert_next_action_state(
    db: AsyncSession,
    view: NextActionView,
    *,
    responsible_user_id: int | None = None,
) -> NextActionState:
    result = await db.execute(
        select(NextActionState).where(NextActionState.kommo_lead_id == view.kommo_lead_id)
    )
    row = result.scalar_one_or_none()
    if row is None:
        row = NextActionState(kommo_lead_id=view.kommo_lead_id)
        db.add(row)
    row.status = view.status
    row.waiting_on = view.waiting_on
    row.action_text = view.action_text or view.recommended_action
    row.due_at = view.due_at
    row.responsible_user_id = responsible_user_id
    row.stale_reason = view.stale_reason
    metadata = dict(row.metadata_json or {})
    metadata.update({"category": view.category, "age_days": view.age_days})
    row.metadata_json = metadata
    try:
        flag_modified(row, "metadata_json")
    except Exception:
        pass
    row.updated_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(row)
    return row


def _stored_state_is_meaningful(row: NextActionState | None) -> bool:
    if row is None:
        return False
    followup = dict((row.metadata_json or {}).get("followup") or {})
    if followup.get("status") in {"scheduled", "reminded"}:
        return True
    return row.waiting_on in {"us", "client"}


async def build_inbox(
    db: AsyncSession,
    *,
    limit_per_section: int = 8,
    responsible_user_id: int | None = None,
) -> InboxResult:
    open_result = await kommo_service.get_all_open_leads()
    leads = open_result.get("leads") or []
    lead_ids = [int(lead.get("id") or 0) for lead in leads if int(lead.get("id") or 0)]
    stored_by_lead: dict[int, NextActionState] = {}
    if lead_ids:
        stored_rows = list(
            (
                await db.execute(
                    select(NextActionState).where(NextActionState.kommo_lead_id.in_(lead_ids))
                )
            ).scalars().all()
        )
        stored_by_lead = {int(row.kommo_lead_id): row for row in stored_rows}

    inbox = InboxResult()
    for lead in leads:
        if responsible_user_id and int(lead.get("responsible_user_id") or 0) != int(responsible_user_id):
            continue
        lead_id = int(lead.get("id") or 0)
        stored = stored_by_lead.get(lead_id)
        use_stored = _stored_state_is_meaningful(stored)
        view = evaluate_lead_next_action(
            lead,
            waiting_on=stored.waiting_on if use_stored and stored else None,
            action_text=stored.action_text if use_stored and stored else None,
            stored_due_at=stored.due_at if use_stored and stored else None,
            stored_stale_reason=stored.stale_reason if use_stored and stored else None,
        )
        try:
            await upsert_next_action_state(
                db,
                view,
                responsible_user_id=lead.get("responsible_user_id"),
            )
        except Exception:
            await db.rollback()
        bucket = {
            "overdue": inbox.overdue,
            "waiting_us": inbox.waiting_us,
            "waiting_client": inbox.waiting_client,
            "without_next": inbox.without_next,
            "stale": inbox.stale,
            "ready": inbox.ready,
        }.get(view.category, inbox.without_next)
        if len(bucket) < limit_per_section:
            bucket.append(view)
    await enrich_recommended_actions(inbox)
    return inbox


_NEXT_ACTION_PROMPT = """Ты — коммерческий ассистент Buy & Bring Solutions.
По каждой сделке предложи ОДНО конкретное действие менеджера на сегодня.
Используй только переданные название, этап, открытую задачу и заметки Kommo.
Не выдумывай цену, обещания, договорённости, сроки или характеристики.
Если данных недостаточно, предложи вопрос, который восстановит контекст.
Если клиент ждёт нас — действие должно закрывать наше обещание.
Если мы ждём клиента — подготовь короткий follow-up.
Текст клиенту пиши на языке последних сообщений, иначе по-русски.
Верни только JSON:
{"items":[{"lead_id":1,"action":"что именно сделать","reason":"зачем сейчас",
"message":"готовый текст клиенту или null","confidence":0.0}]}"""


def _fallback_recommendation(
    view: NextActionView,
    *,
    tasks: list[dict[str, Any]],
    notes: list[dict[str, Any]],
) -> None:
    task_text = next(
        (str(item.get("text") or "").strip() for item in tasks if item.get("text")),
        "",
    )
    if task_text:
        view.recommended_action = task_text
        view.action_reason = (
            "Это ближайшая незавершённая задача Kommo; её нужно выполнить "
            "или назначить новый реальный срок."
        )
        return
    if view.waiting_on == "us":
        view.recommended_action = "Проверить последнее обещание клиенту и дать результат"
        view.action_reason = "Клиент ждёт действие с нашей стороны."
    elif view.waiting_on == "client":
        view.recommended_action = "Отправить клиенту короткий follow-up по последнему вопросу"
        view.action_reason = "Нужно получить ответ или согласовать новый срок контакта."
    elif notes:
        view.recommended_action = "Прочитать последнюю заметку и зафиксировать конкретный следующий шаг"
        view.action_reason = "В Kommo нет открытой задачи, но история общения сохранена."
    else:
        view.recommended_action = "Восстановить контекст сделки и решить: продолжать или закрыть"
        view.action_reason = "В Kommo недостаточно данных для обоснованного сообщения клиенту."


async def enrich_recommended_actions(inbox: InboxResult) -> None:
    """Turn technical inbox flags into evidence-backed manager actions.

    One bounded AI call analyses all visible plan items. Kommo/OpenAI failures
    preserve a useful deterministic plan instead of breaking /today.
    """
    candidates = (
        inbox.overdue + inbox.waiting_us + inbox.without_next + inbox.stale
    )[:10]
    if not candidates:
        return

    semaphore = asyncio.Semaphore(4)

    async def load(view: NextActionView) -> tuple[NextActionView, dict[str, Any], list[dict[str, Any]]]:
        async with semaphore:
            details_result, tasks_result = await asyncio.gather(
                kommo_service.get_lead_details(view.kommo_lead_id),
                kommo_service.get_open_lead_tasks(view.kommo_lead_id, limit=5),
                return_exceptions=True,
            )
        details = details_result if isinstance(details_result, dict) else {}
        tasks = tasks_result if isinstance(tasks_result, list) else []
        _fallback_recommendation(
            view,
            tasks=tasks,
            notes=list(details.get("notes") or []),
        )
        return view, details, tasks

    loaded = await asyncio.gather(*(load(view) for view in candidates))
    payload = []
    for view, details, tasks in loaded:
        payload.append(
            {
                "lead_id": view.kommo_lead_id,
                "name": view.name,
                "status": details.get("status_name"),
                "category": view.category,
                "waiting_on": view.waiting_on,
                "age_days": view.age_days,
                "open_tasks": [
                    {
                        "text": str(task.get("text") or "")[:500],
                        "complete_till": task.get("complete_till"),
                    }
                    for task in tasks[:3]
                ],
                "recent_notes": [
                    {
                        "text": str(note.get("text") or "")[:1200],
                        "created_at": note.get("created_at"),
                    }
                    for note in list(details.get("notes") or [])[:5]
                ],
            }
        )
    try:
        response = await ai_analysis_service._client.chat.completions.create(  # noqa: SLF001
            model=settings.agent_writer_model or settings.openai_model,
            messages=[
                {"role": "system", "content": _NEXT_ACTION_PROMPT},
                {
                    "role": "user",
                    "content": json.dumps(payload, ensure_ascii=False, default=str),
                },
            ],
            response_format={"type": "json_object"},
            temperature=0.1,
        )
        parsed = json.loads(response.choices[0].message.content or "{}")
        by_id = {
            int(item.get("lead_id")): item
            for item in parsed.get("items") or []
            if isinstance(item, dict) and str(item.get("lead_id") or "").isdigit()
        }
        for view in candidates:
            item = by_id.get(view.kommo_lead_id) or {}
            action = str(item.get("action") or "").strip()
            reason = str(item.get("reason") or "").strip()
            message = str(item.get("message") or "").strip()
            if action:
                view.recommended_action = action[:500]
            if reason:
                view.action_reason = reason[:500]
            if message and message.casefold() not in {"null", "none"}:
                view.suggested_message = message[:1200]
    except Exception as exc:
        logger.warning("Smart next-action analysis unavailable: %s", exc.__class__.__name__)


def _item_lines(items: list[NextActionView], *, emoji: str, title: str) -> list[str]:
    if not items:
        return []
    lines = [f"{emoji} <b>{title}: {len(items)}</b>", ""]
    for index, item in enumerate(items[:8], 1):
        label = f"№{item.internal_number}" if item.internal_number else str(item.kommo_lead_id)
        lines.append(f"{index}. {html.escape(label)} — {html.escape(item.name[:60])}")
        reason = item.stale_reason or item.recommended_action or item.status
        lines.append(html.escape(str(reason)[:120]))
        lines.append("")
    return lines


def format_inbox(inbox: InboxResult) -> str:
    lines = ["<b>📥 Операционный inbox</b>", ""]
    lines.extend(_item_lines(inbox.overdue, emoji="🔴", title="Просрочено"))
    lines.extend(_item_lines(inbox.waiting_us, emoji="🟠", title="Клиент ждёт нас"))
    lines.extend(_item_lines(inbox.waiting_client, emoji="🟡", title="Мы ждём клиента"))
    lines.extend(_item_lines(inbox.without_next, emoji="🔵", title="Нет следующего действия"))
    lines.extend(_item_lines(inbox.stale, emoji="⚪", title="Давно без контакта"))
    if len(lines) == 2:
        lines.append("Сейчас нет проблемных проектов.")
    return "\n".join(lines)


def format_plan(inbox: InboxResult) -> str:
    lines = [
        "<b>📅 ПЛАН НА СЕГОДНЯ</b>",
        "",
        f"🔴 Просрочено: <b>{len(inbox.overdue)}</b>",
        f"🔵 Без следующего шага: <b>{len(inbox.without_next)}</b>",
        f"🟠 Клиент ждёт нас: <b>{len(inbox.waiting_us)}</b>",
        f"🟡 Ждём клиента: <b>{len(inbox.waiting_client)}</b>",
        f"⚪ Stale: <b>{len(inbox.stale)}</b>",
        "",
    ]
    combined = (inbox.overdue + inbox.waiting_us + inbox.without_next + inbox.stale)[:10]
    if not combined:
        lines.append("На сегодня критичных пунктов нет.")
        return "\n".join(lines)
    for index, item in enumerate(combined, 1):
        label = f"№{item.internal_number}" if item.internal_number else str(item.kommo_lead_id)
        lines.append(f"<b>{index}. {html.escape(label)} — {html.escape(item.name[:50])}</b>")
        lines.append(
            "Что сделать: "
            + html.escape(str(item.recommended_action or item.stale_reason or "—")[:500])
        )
        if item.action_reason:
            lines.append("Почему: " + html.escape(item.action_reason[:500]))
        if item.suggested_message:
            lines.append("Что написать: «" + html.escape(item.suggested_message[:1200]) + "»")
        lines.append("")
    return "\n".join(lines)


def inbox_markup(inbox: InboxResult) -> dict[str, Any] | None:
    rows: list[list[dict[str, str]]] = []
    for item in (inbox.overdue + inbox.waiting_us + inbox.without_next)[:6]:
        label = f"№{item.internal_number}" if item.internal_number else str(item.kommo_lead_id)
        rows.append(
            [
                {
                    "text": f"Открыть {label}"[:64],
                    "callback_data": f"agent:lead:{item.kommo_lead_id}",
                }
            ]
        )
    return {"inline_keyboard": rows} if rows else None


NEXT_ACTION_OPTIONS = [
    ("write", "Написать клиенту"),
    ("call", "Позвонить"),
    ("factory", "Запросить фабрику"),
    ("offer", "Подготовить предложение"),
    ("wait", "Ждём ответ"),
    ("close", "Закрыть как нецелевой"),
    ("manual", "Указать вручную"),
]


def next_action_prompt_markup(*, kommo_lead_id: int) -> dict[str, Any]:
    rows = []
    for key, label in NEXT_ACTION_OPTIONS:
        rows.append(
            [{"text": label, "callback_data": f"agent:next:{kommo_lead_id}:{key}"}]
        )
    return {"inline_keyboard": rows}
