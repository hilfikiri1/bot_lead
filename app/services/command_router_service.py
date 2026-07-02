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
    calendar_service,
    crm_service,
    kommo_service,
    notion_service,
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


def _looks_like_command(text: str) -> bool:
    clean = text.strip().casefold()
    if not clean:
        return False
    if len(clean) > 1200:
        return False
    if any(hint in clean for hint in COMMAND_HINTS):
        return True
    return len(clean) < 220


async def classify_message(text: str, *, context: dict[str, Any]) -> CommandPlan:
    transcript = text.strip()
    if not transcript:
        return CommandPlan("unknown", 0.0, {})

    if not settings.voice_command_mode:
        return CommandPlan("analyze_conversation", 1.0, {})

    if not _looks_like_command(transcript):
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
    return CommandPlan(intent=intent, confidence=confidence, raw=raw)


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
    lowered = raw.casefold()
    now = datetime.now(tz=_manager_tz())
    if "завтра" in lowered:
        hour = 10
        match = re.search(r"(\d{1,2})[:\.]?(\d{2})?", lowered)
        if match:
            hour = int(match.group(1))
            minute = int(match.group(2) or 0)
        else:
            minute = 0
        target = (now + timedelta(days=1)).replace(
            hour=hour, minute=minute, second=0, microsecond=0
        )
        return target.isoformat()
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

    if intent in {"create_task", "create_reminder"}:
        title = str(raw.get("task_title") or raw.get("note_text") or "Напоминание").strip()
        calendar = raw.get("calendar") or {}
        start_iso = calendar.get("start_time") or _parse_relative_time(title)
        page_id = await notion_service.create_task_page(
            title=title[:200],
            task_type="Goal" if "цел" in title.casefold() else "Task",
            due_at=start_iso,
            lead_page_id=context.get("notion_lead_page_id"),
            client_page_id=context.get("notion_client_page_id"),
            source="Voice",
        )
        calendar_msg = ""
        if start_iso:
            result = calendar_service.create_event_with_fallback(
                title,
                "Создано голосовой командой из Telegram",
                start_iso,
                int(calendar.get("duration_minutes") or 30),
            )
            if result["success"]:
                calendar_msg = f"\nКалендарь: событие создано (<code>{result['event_id']}</code>)."
            elif result.get("ics_content"):
                calendar_msg = "\nКалендарь: CalDAV недоступен, используйте .ics fallback через мастер сделки."
        if page_id:
            return f"✅ Задача добавлена в Notion.{calendar_msg}"
        return "⚠️ Notion tasks DB не настроена, но команда распознана."

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
