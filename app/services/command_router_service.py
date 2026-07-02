"""Voice and text command routing without Telegram buttons."""

from __future__ import annotations

import html
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from openai import AsyncOpenAI
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.services import (
    approval_service,
    crm_service,
    kommo_service,
    notion_service,
    telegram_service,
)

logger = logging.getLogger(__name__)
settings = get_settings()
_client = AsyncOpenAI(api_key=settings.openai_api_key)

COMMAND_HINTS = (
    "напомни",
    "remind",
    "добавь",
    "add ",
    "удали",
    "delete",
    "найди",
    "find",
    "покажи",
    "show",
    "обнови",
    "update",
    "задач",
    "task",
    "цел",
    "goal",
    "notion",
    "коммо",
    "kommo",
    "дайджест",
    "digest",
    "создай лид",
    "create lead",
    "примечан",
    "note",
    "календар",
    "calendar",
    "напоминан",
)

SYSTEM_PROMPT = """You route manager voice/text commands for Buy & Bring CRM bot.

Return ONLY valid JSON.

If the message is clearly a client sales conversation to analyze, use intent "analyze_conversation".
If it is a short instruction to the bot, choose the best command intent.

Supported intents:
- analyze_conversation
- search_client
- search_lead
- add_notion_note
- create_task
- create_reminder
- create_calendar
- update_lead
- delete_lead
- delete_task
- create_kommo_lead_from_last
- morning_digest
- unknown

JSON schema:
{
  "intent": "string",
  "confidence": 0.0,
  "lead_reference": {"by": "id|name|active", "value": "string|null"},
  "client_reference": {"by": "name|active", "value": "string|null"},
  "note_text": "string|null",
  "task_title": "string|null",
  "calendar": {"title": "string|null", "start_time": "ISO-8601|null", "duration_minutes": 15},
  "lead_updates": {"title": "string|null", "product": "string|null", "budget": "string|null"},
  "clarification_question": "string|null",
  "reply_language": "ru"
}

Rules:
- confidence must be high only when intent is obvious.
- Never invent IDs.
- "this deal/lead" means active context.
- destructive intents should set clarification_question if target is ambiguous.
- reply in Russian in clarification_question.
"""


@dataclass
class CommandPlan:
    intent: str
    confidence: float
    raw: dict[str, Any]


def _looks_like_command(text: str, *, strict: bool = False) -> bool:
    clean = text.strip().casefold()
    if not clean:
        return False
    if len(clean) > 1200:
        return False
    if any(hint in clean for hint in COMMAND_HINTS):
        return True
    if strict:
        return False
    return len(clean) < 220


async def classify_message(
    text: str,
    *,
    context: dict[str, Any],
    command_only: bool = False,
) -> CommandPlan:
    transcript = text.strip()
    if not transcript:
        return CommandPlan("unknown", 0.0, {})

    if command_only:
        if not _looks_like_command(transcript, strict=True):
            return CommandPlan("unknown", 0.0, {})
    elif not settings.voice_command_mode:
        return CommandPlan("analyze_conversation", 1.0, {})
    elif not _looks_like_command(transcript):
        return CommandPlan("analyze_conversation", 0.9, {})

    response = await _client.chat.completions.create(
        model=settings.openai_model,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    f"CONTEXT:\n{json.dumps(context, ensure_ascii=False)}\n\n"
                    f"COMMAND:\n{transcript}"
                ),
            },
        ],
        response_format={"type": "json_object"},
        temperature=0.1,
    )
    raw = json.loads(response.choices[0].message.content or "{}")
    intent = str(raw.get("intent") or "unknown")
    confidence = float(raw.get("confidence") or 0.0)
    if command_only and intent == "analyze_conversation":
        intent = "unknown"
        confidence = 0.0
    return CommandPlan(intent=intent, confidence=confidence, raw=raw)


COMMAND_NOT_RECOGNIZED = (
    "🤔 <b>Не распознал команду</b>\n\n"
    "Примеры:\n"
    "• <i>Напомни завтра в 10 позвонить клиенту</i>\n"
    "• <i>Добавь в календарь созвон в пятницу в 15:00</i>\n"
    "• <i>Найди сделку 90 надувная</i>\n\n"
    "Чтобы записать <b>разговор с клиентом</b> и создать лид — "
    "нажмите <b>🎙 Новый разговор</b> в меню и затем отправьте аудио."
)


def _manager_tz() -> ZoneInfo:
    try:
        return ZoneInfo(settings.manager_timezone)
    except Exception:
        return ZoneInfo("UTC")


def _parse_relative_time(value: str | None) -> str | None:
    if not value:
        return None
    raw = value.strip()
    try:
        return datetime.fromisoformat(raw).astimezone(_manager_tz()).isoformat()
    except ValueError:
        pass

    lowered = raw.casefold().replace("ё", "е")
    now = datetime.now(tz=_manager_tz())
    day_offset = 0
    if "послезавтра" in lowered:
        day_offset = 2
    elif "завтра" in lowered:
        day_offset = 1
    elif "сегодня" in lowered:
        day_offset = 0
    elif "через" in lowered and "час" in lowered:
        match = re.search(r"через\s+(\d{1,2})\s*час", lowered)
        hours = int(match.group(1)) if match else 1
        return (now + timedelta(hours=hours)).replace(second=0, microsecond=0).isoformat()

    hour = 10
    minute = 0
    match = re.search(
        r"(?:в\s+)?(\d{1,2})[:\.](\d{2})|(?:в\s+)(\d{1,2})(?:\s*час|\s*ч\.?)?",
        lowered,
    )
    if match:
        if match.group(1) and match.group(2):
            hour = int(match.group(1))
            minute = int(match.group(2))
        elif match.group(3):
            hour = int(match.group(3))
            minute = 0

    if day_offset or any(
        token in lowered
        for token in ("завтра", "сегодня", "послезавтра", "в ", ":", ".")
    ):
        target = (now + timedelta(days=day_offset)).replace(
            hour=hour, minute=minute, second=0, microsecond=0
        )
        if day_offset == 0 and target <= now and ("в " in lowered or ":" in lowered):
            target += timedelta(days=1)
        return target.isoformat()
    return None


def _resolve_calendar_start(raw: dict[str, Any], *, fallback_text: str) -> str | None:
    calendar = raw.get("calendar") or {}
    start_iso = calendar.get("start_time")
    if start_iso:
        parsed = _parse_relative_time(str(start_iso))
        if parsed:
            return parsed
    for candidate in (
        calendar.get("title"),
        raw.get("task_title"),
        raw.get("note_text"),
        fallback_text,
    ):
        parsed = _parse_relative_time(str(candidate or ""))
        if parsed:
            return parsed
    return None


def _format_start_display(start_iso: str) -> str:
    try:
        dt = datetime.fromisoformat(start_iso).astimezone(_manager_tz())
        return dt.strftime("%d.%m.%Y %H:%M")
    except ValueError:
        return start_iso


async def _create_calendar_event(
    chat_id: int,
    *,
    title: str,
    start_iso: str | None,
    duration_minutes: int = 30,
    description: str = "Создано голосовой командой из Telegram",
) -> str | None:
    """Create a calendar event. Returns extra reply text or None if fully handled."""
    if not start_iso:
        return (
            "🕒 <b>Когда добавить в календарь?</b>\n\n"
            "Напишите или скажите дату и время. Например: "
            "<code>завтра в 10:00</code> или <code>2 июля в 15:30</code>."
        )
    await telegram_service.send_calendar_result(
        chat_id,
        title=title[:200],
        start_iso=start_iso,
        duration_minutes=duration_minutes,
        description=description,
        start_display=_format_start_display(start_iso),
    )
    return None


async def execute_plan(
    db: AsyncSession,
    *,
    plan: CommandPlan,
    chat_id: int,
    telegram_user_id: int,
    context: dict[str, Any],
) -> str | None:
    if plan.intent == "analyze_conversation":
        return None
    if plan.confidence < 0.65 and plan.intent not in {"morning_digest"}:
        question = plan.raw.get("clarification_question")
        if question:
            return f"❓ {html.escape(str(question))}"
        return None

    intent = plan.intent
    raw = plan.raw

    if intent == "morning_digest":
        return await notion_service.get_morning_digest()

    if intent == "search_client":
        query = (raw.get("client_reference") or {}).get("value") or raw.get("note_text") or ""
        results = await notion_service.search_clients(str(query))
        if not results:
            return "Клиент в Notion не найден."
        lines = ["<b>Клиенты в Notion</b>", ""]
        for item in results:
            lines.append(f"• {html.escape(item['title'])}")
            if item.get("notes"):
                lines.append(f"  <i>{html.escape(item['notes'][:200])}</i>")
        return "\n".join(lines)

    if intent == "search_lead":
        query = (raw.get("lead_reference") or {}).get("value") or ""
        kommo = await kommo_service.search_open_leads(str(query), limit=5)
        notion = await notion_service.search_leads(str(query), limit=5)
        lines = ["<b>Найденные сделки</b>", ""]
        for lead in kommo.get("leads") or []:
            lines.append(
                f"• Kommo: {html.escape(str(lead.get('name') or '—'))} "
                f"(<code>{lead.get('id')}</code>)"
            )
        for lead in notion:
            lines.append(f"• Notion: {html.escape(lead['title'])}")
        return "\n".join(lines) if len(lines) > 2 else "Сделки не найдены."

    if intent == "add_notion_note":
        note = str(raw.get("note_text") or "").strip()
        if not note:
            return "Не указан текст примечания."
        page_id = context.get("notion_lead_page_id") or context.get("notion_client_page_id")
        if not page_id:
            return "Не выбран клиент или сделка. Сначала найдите клиента или откройте сделку."
        await notion_service.add_note_to_page(page_id, note, human_field=True)
        return "✅ Мысль добавлена в Notion (поле Manager thoughts)."

    if intent in {"create_task", "create_reminder", "create_calendar"}:
        calendar = raw.get("calendar") or {}
        title = str(
            calendar.get("title")
            or raw.get("task_title")
            or raw.get("note_text")
            or "Напоминание"
        ).strip()
        duration = int(calendar.get("duration_minutes") or 30)
        start_iso = _resolve_calendar_start(raw, fallback_text=title)
        calendar_requested = intent in {"create_calendar", "create_reminder"} or bool(
            start_iso
        )
        calendar_handled = False

        if calendar_requested:
            calendar_reply = await _create_calendar_event(
                chat_id,
                title=title,
                start_iso=start_iso,
                duration_minutes=duration,
            )
            if calendar_reply:
                if intent == "create_calendar":
                    return calendar_reply
                if not settings.notion_tasks_database_id.strip():
                    return calendar_reply
            else:
                calendar_handled = True
                if intent == "create_calendar":
                    return None

        page_id = None
        if settings.notion_tasks_database_id.strip() and intent != "create_calendar":
            page_id = await notion_service.create_task_page(
                title=title[:200],
                task_type="Goal" if "цел" in title.casefold() else "Task",
                due_at=start_iso,
                lead_page_id=context.get("notion_lead_page_id"),
                client_page_id=context.get("notion_client_page_id"),
                source="Voice",
            )

        if page_id and calendar_handled:
            return "✅ Задача добавлена в Notion."
        if page_id:
            return "✅ Задача добавлена в Notion."
        if calendar_handled:
            return None
        if calendar_requested:
            return (
                "🕒 <b>Когда добавить в календарь?</b>\n\n"
                "Укажите дату и время. Например: <code>завтра в 10:00</code>."
            )
        return (
            "⚠️ Notion tasks DB не настроена.\n\n"
            "Добавьте <code>NOTION_TASKS_DATABASE_ID</code> в Railway "
            "или попросите напоминание с датой для календаря."
        )

    if intent == "update_lead":
        lead_page_id = context.get("notion_lead_page_id")
        if not lead_page_id:
            ref = (raw.get("lead_reference") or {}).get("value")
            if ref:
                found = await notion_service.search_leads(str(ref), limit=1)
                lead_page_id = found[0]["id"] if found else None
        if not lead_page_id:
            return "Не найдена сделка в Notion для обновления."
        await notion_service.update_lead_fields(
            lead_page_id, raw.get("lead_updates") or {}
        )
        return "✅ Сделка обновлена в Notion."

    if intent == "delete_lead":
        lead_page_id = context.get("notion_lead_page_id")
        if not lead_page_id:
            return "Уточните сделку: «удали сделку 90 надувная»."
        await notion_service.delete_page_soft(lead_page_id)
        return "✅ Сделка архивирована в Notion (мягкое удаление)."

    if intent == "delete_task":
        return "ℹ️ Удаление задач по голосу пока через Notion UI. Могу добавить точечное удаление в следующем шаге."

    if intent == "create_kommo_lead_from_last":
        lead_id = context.get("local_lead_id")
        voice_note_id = context.get("voice_note_id")
        if not lead_id or not voice_note_id:
            return "Нет последнего проанализированного звонка."
        draft = await approval_service.build_kommo_creation_draft(
            db, lead_id=int(lead_id), voice_note_id=int(voice_note_id)
        )
        return await approval_service.execute_kommo_create_from_draft(
            db,
            lead_id=int(lead_id),
            voice_note_id=int(voice_note_id),
            draft=draft,
            telegram_user_id=telegram_user_id,
        )

    if intent == "unknown":
        return None

    return f"Команда распознана как <code>{html.escape(intent)}</code>, но пока не реализована."
