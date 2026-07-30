"""Local-first daily reflection and weekly kaizen analysis.

Raw manager text is committed before any OpenAI or Notion call. Notion receives
only explicitly confirmed weekly improvement cards through PendingAgentAction.
"""
from __future__ import annotations

import html
import json
import logging
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from sqlalchemy import and_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.agent import memory, notion_gateway
from app.config import get_settings
from app.models.agent_session import AgentSession
from app.models.kaizen_journal_entry import KaizenJournalEntry
from app.services import ai_analysis_service, next_action_service

logger = logging.getLogger(__name__)
settings = get_settings()

DAILY_ENTRY_TYPE = "daily"
WEEKLY_ENTRY_TYPE = "weekly"
_PENDING_KEY = "pending_daily_reflection"
_MAX_RAW_CHARS = 50_000
_MAX_ITEMS = 8

DAILY_SYSTEM_PROMPT = """You structure a private work reflection for the owner of Buy & Bring Solutions.
Return ONLY one valid JSON object. Write concise natural Russian.
Never invent facts, diagnoses, motives, savings, clients, projects or repeated patterns.
Preserve concrete client, project and process names from the manager's story.
Separate events from cautious hypotheses. Do not turn every complaint into a task.
A possible improvement is only a candidate and must be grounded in quoted or paraphrased evidence.
Do not claim recurrence from one day.

Return exactly this shape:
{
  "good": ["what worked or brought value"],
  "difficulties": ["what failed, blocked or irritated"],
  "time_losses": ["where time was lost"],
  "lessons": ["manager's conclusions"],
  "ideas": ["new ideas"],
  "tomorrow_focus": ["what matters tomorrow"],
  "improvement_signals": [
    {
      "area": "sales|suppliers|documents|planning|communication|automation|other",
      "problem": "short grounded problem",
      "evidence": "fact from the story",
      "possible_improvement": "preliminary concrete idea",
      "confidence": 0.0
    }
  ],
  "needs_followup": false,
  "followup_question": null
}
"""

WEEKLY_SYSTEM_PROMPT = """You analyse a private weekly work journal for the owner of Buy & Bring Solutions.
Return ONLY one valid JSON object in concise natural Russian.
Use only supplied daily entries and optional read-only operational counts.
Never invent facts, savings, money, time estimates or systematic problems.
A recurring problem requires evidence from at least two different days.
Root cause is always a cautious hypothesis. If fewer than three completed days are present,
set insufficient_data=true and avoid claims that a problem is systemic.
Prefer one or two concrete process improvements; return no more than three.
Avoid abstract actions such as 'plan better'. Each action must change one observable process.

Return exactly this shape:
{
  "summary": "brief overall conclusion",
  "main_wins": ["main results"],
  "recurring_problems": [
    {
      "problem": "recurring problem",
      "evidence": ["dated concrete mention"],
      "days_count": 2,
      "root_cause_hypothesis": "cautious hypothesis",
      "confidence": 0.0
    }
  ],
  "useful_patterns": ["what should continue"],
  "stop_or_reduce": ["what should stop or be reduced"],
  "next_week_focus": ["maximum three focuses"],
  "improvement_candidates": [
    {
      "title": "short improvement title",
      "problem": "what happens",
      "evidence": "why this is repeated",
      "proposed_action": "one concrete action",
      "expected_effect": "expected qualitative effect without invented numbers",
      "verification": "how to check next week",
      "impact": "high|medium|low",
      "effort": "low|medium|high",
      "priority": 1,
      "due_date": null
    }
  ],
  "insufficient_data": false
}
"""


def manager_timezone() -> ZoneInfo:
    name = settings.agent_digest_timezone or settings.manager_timezone or "Europe/Warsaw"
    try:
        return ZoneInfo(name)
    except Exception:
        logger.warning("Invalid kaizen timezone %s; using Europe/Warsaw", name)
        return ZoneInfo("Europe/Warsaw")


def local_now(now: datetime | None = None) -> datetime:
    value = now or datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(manager_timezone())


def local_date(now: datetime | None = None) -> date:
    return local_now(now).date()


def week_period(day: date | None = None) -> tuple[date, date]:
    target = day or local_date()
    start = target - timedelta(days=target.weekday())
    return start, start + timedelta(days=6)


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _clean_text(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()


def is_meaningful_reflection(value: str) -> bool:
    text = _clean_text(value)
    return len(text) >= 5 and any(char.isalnum() for char in text)


def _short_list(value: Any, *, limit: int = _MAX_ITEMS) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value:
        text = _clean_text(item)
        if text and text not in result:
            result.append(text[:500])
        if len(result) >= limit:
            break
    return result


def _confidence(value: Any) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.0


def normalise_daily_analysis(value: Any) -> dict[str, Any]:
    data = value if isinstance(value, dict) else {}
    signals: list[dict[str, Any]] = []
    allowed_areas = {
        "sales", "suppliers", "documents", "planning", "communication", "automation", "other"
    }
    for raw in data.get("improvement_signals") or []:
        if not isinstance(raw, dict):
            continue
        problem = _clean_text(raw.get("problem"))
        evidence = _clean_text(raw.get("evidence"))
        possible = _clean_text(raw.get("possible_improvement"))
        if not problem or not evidence:
            continue
        area = str(raw.get("area") or "other").casefold()
        signals.append(
            {
                "area": area if area in allowed_areas else "other",
                "problem": problem[:500],
                "evidence": evidence[:700],
                "possible_improvement": possible[:700],
                "confidence": _confidence(raw.get("confidence")),
            }
        )
        if len(signals) >= 6:
            break
    question = _clean_text(data.get("followup_question")) or None
    return {
        "good": _short_list(data.get("good")),
        "difficulties": _short_list(data.get("difficulties")),
        "time_losses": _short_list(data.get("time_losses")),
        "lessons": _short_list(data.get("lessons")),
        "ideas": _short_list(data.get("ideas")),
        "tomorrow_focus": _short_list(data.get("tomorrow_focus"), limit=5),
        "improvement_signals": signals,
        "needs_followup": bool(data.get("needs_followup")) and bool(question),
        "followup_question": question,
    }


def normalise_weekly_analysis(value: Any, *, completed_days: int) -> dict[str, Any]:
    data = value if isinstance(value, dict) else {}
    recurring: list[dict[str, Any]] = []
    for raw in data.get("recurring_problems") or []:
        if not isinstance(raw, dict):
            continue
        evidence = _short_list(raw.get("evidence"), limit=8)
        try:
            days_count = max(0, int(raw.get("days_count") or len(evidence)))
        except (TypeError, ValueError):
            days_count = len(evidence)
        # Do not let an AI call a one-day observation recurring.
        if days_count < 2 or len(evidence) < 2:
            continue
        recurring.append(
            {
                "problem": _clean_text(raw.get("problem"))[:500],
                "evidence": evidence,
                "days_count": days_count,
                "root_cause_hypothesis": _clean_text(raw.get("root_cause_hypothesis"))[:700],
                "confidence": _confidence(raw.get("confidence")),
            }
        )
        if len(recurring) >= 5:
            break

    candidates: list[dict[str, Any]] = []
    for index, raw in enumerate(data.get("improvement_candidates") or [], 1):
        if not isinstance(raw, dict):
            continue
        title = _clean_text(raw.get("title"))
        action = _clean_text(raw.get("proposed_action"))
        evidence = _clean_text(raw.get("evidence"))
        if not title or not action or not evidence:
            continue
        impact = str(raw.get("impact") or "medium").casefold()
        effort = str(raw.get("effort") or "medium").casefold()
        try:
            priority = int(raw.get("priority") or index)
        except (TypeError, ValueError):
            priority = index
        candidates.append(
            {
                "title": title[:200],
                "problem": _clean_text(raw.get("problem"))[:700],
                "evidence": evidence[:1200],
                "proposed_action": action[:900],
                "expected_effect": _clean_text(raw.get("expected_effect"))[:700],
                "verification": _clean_text(raw.get("verification"))[:700],
                "impact": impact if impact in {"high", "medium", "low"} else "medium",
                "effort": effort if effort in {"high", "medium", "low"} else "medium",
                "priority": max(1, min(priority, 99)),
                "due_date": raw.get("due_date") or None,
            }
        )
        if len(candidates) >= 3:
            break
    candidates.sort(key=lambda item: item["priority"])
    return {
        "summary": _clean_text(data.get("summary"))[:1200],
        "main_wins": _short_list(data.get("main_wins")),
        "recurring_problems": recurring,
        "useful_patterns": _short_list(data.get("useful_patterns")),
        "stop_or_reduce": _short_list(data.get("stop_or_reduce")),
        "next_week_focus": _short_list(data.get("next_week_focus"), limit=3),
        "improvement_candidates": candidates[:3],
        "insufficient_data": completed_days < 3 or bool(data.get("insufficient_data")),
        "completed_days": completed_days,
    }


async def _structured_json(system_prompt: str, user_prompt: str) -> dict[str, Any]:
    """Reuse the existing application OpenAI client; never create another client."""
    response = await ai_analysis_service._client.chat.completions.create(  # noqa: SLF001
        model=settings.agent_writer_model or settings.openai_model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        response_format={"type": "json_object"},
        temperature=0.1,
    )
    raw = response.choices[0].message.content or "{}"
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ValueError("AI kaizen response must be a JSON object")
    return parsed


async def get_entry(
    db: AsyncSession,
    *,
    telegram_user_id: int,
    entry_type: str,
    period_start: date,
    period_end: date,
    lock: bool = False,
) -> KaizenJournalEntry | None:
    query = select(KaizenJournalEntry).where(
        KaizenJournalEntry.telegram_user_id == int(telegram_user_id),
        KaizenJournalEntry.entry_type == entry_type,
        KaizenJournalEntry.period_start == period_start,
        KaizenJournalEntry.period_end == period_end,
    )
    if lock:
        query = query.with_for_update()
    return (await db.execute(query)).scalar_one_or_none()


async def get_entry_by_id(
    db: AsyncSession,
    *,
    entry_id: int,
    telegram_user_id: int,
    lock: bool = False,
) -> KaizenJournalEntry | None:
    query = select(KaizenJournalEntry).where(
        KaizenJournalEntry.id == int(entry_id),
        KaizenJournalEntry.telegram_user_id == int(telegram_user_id),
    )
    if lock:
        query = query.with_for_update()
    return (await db.execute(query)).scalar_one_or_none()


async def get_or_create_entry(
    db: AsyncSession,
    *,
    telegram_user_id: int,
    entry_type: str,
    period_start: date,
    period_end: date,
    source: str,
    status: str = "open",
) -> KaizenJournalEntry:
    existing = await get_entry(
        db,
        telegram_user_id=telegram_user_id,
        entry_type=entry_type,
        period_start=period_start,
        period_end=period_end,
    )
    if existing is not None:
        return existing
    row = KaizenJournalEntry(
        telegram_user_id=int(telegram_user_id),
        entry_type=entry_type,
        period_start=period_start,
        period_end=period_end,
        source=source[:24],
        status=status[:24],
        analysis={},
        notion_page_ids=[],
    )
    db.add(row)
    try:
        await db.commit()
        await db.refresh(row)
        return row
    except IntegrityError:
        await db.rollback()
        concurrent = await get_entry(
            db,
            telegram_user_id=telegram_user_id,
            entry_type=entry_type,
            period_start=period_start,
            period_end=period_end,
        )
        if concurrent is not None:
            return concurrent
        raise


def _pending_expiry(now: datetime | None = None) -> datetime:
    local = local_now(now)
    six_hours = local + timedelta(hours=6)
    next_day = datetime.combine(local.date() + timedelta(days=1), time.min, tzinfo=local.tzinfo)
    return min(six_hours, next_day).astimezone(timezone.utc)


async def set_pending_reflection(
    db: AsyncSession,
    *,
    session: AgentSession,
    day: date,
    source: str,
    now: datetime | None = None,
) -> None:
    started = _utc(now or datetime.now(timezone.utc))
    context = dict(session.context or {})
    context[_PENDING_KEY] = {
        "local_date": day.isoformat(),
        "started_at": started.isoformat(),
        "expires_at": _pending_expiry(now).isoformat(),
        "source": source,
    }
    session.context = context
    await db.commit()


async def clear_pending_reflection(db: AsyncSession, *, session: AgentSession) -> None:
    context = dict(session.context or {})
    if _PENDING_KEY in context:
        context.pop(_PENDING_KEY, None)
        session.context = context
        await db.commit()


async def active_pending_reflection(
    db: AsyncSession,
    *,
    session: AgentSession,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    pending = dict((session.context or {}).get(_PENDING_KEY) or {})
    if not pending:
        return None
    try:
        pending_day = date.fromisoformat(str(pending["local_date"]))
        expires = datetime.fromisoformat(str(pending["expires_at"]).replace("Z", "+00:00"))
    except (KeyError, TypeError, ValueError):
        await clear_pending_reflection(db, session=session)
        return None
    current = _utc(now or datetime.now(timezone.utc))
    if pending_day != local_date(current) or _utc(expires) <= current:
        await clear_pending_reflection(db, session=session)
        return None
    return pending


async def start_daily_reflection(
    db: AsyncSession,
    *,
    telegram_user_id: int,
    session: AgentSession,
    source: str = "command",
    now: datetime | None = None,
) -> KaizenJournalEntry:
    day = local_date(now)
    entry = await get_or_create_entry(
        db,
        telegram_user_id=telegram_user_id,
        entry_type=DAILY_ENTRY_TYPE,
        period_start=day,
        period_end=day,
        source=source,
    )
    if entry.status != "skipped":
        await set_pending_reflection(db, session=session, day=day, source=source, now=now)
    return entry


async def save_daily_reflection(
    db: AsyncSession,
    *,
    telegram_user_id: int,
    session: AgentSession,
    text: str,
    source: str,
    append: bool = False,
    now: datetime | None = None,
) -> tuple[KaizenJournalEntry, bool]:
    clean = str(text or "").strip()
    if not is_meaningful_reflection(clean):
        raise ValueError("Рассказ слишком короткий, чтобы сохранить итоги дня.")
    day = local_date(now)
    entry = await get_or_create_entry(
        db,
        telegram_user_id=telegram_user_id,
        entry_type=DAILY_ENTRY_TYPE,
        period_start=day,
        period_end=day,
        source=source,
    )
    old = str(entry.raw_text or "").strip()
    if old and (append or entry.status == "completed"):
        combined = f"{old}\n\nДополнение:\n{clean}"
    else:
        combined = clean
    entry.raw_text = combined[:_MAX_RAW_CHARS]
    entry.source = source[:24]
    entry.status = "completed"
    entry.remind_at = None
    await clear_pending_reflection(db, session=session)
    # Local-first commit before OpenAI.
    await db.commit()
    await db.refresh(entry)

    analysis_ok = False
    try:
        parsed = await _structured_json(
            DAILY_SYSTEM_PROMPT,
            f"LOCAL DATE: {day.isoformat()}\n\nMANAGER STORY:\n{entry.raw_text}",
        )
        entry.analysis = normalise_daily_analysis(parsed)
        flag_modified(entry, "analysis")
        analysis_ok = True
    except Exception as exc:
        logger.warning("Daily kaizen analysis unavailable: %s", exc.__class__.__name__)
        fallback = dict(entry.analysis or {})
        fallback["analysis_unavailable"] = True
        fallback["analysis_error_type"] = exc.__class__.__name__
        entry.analysis = fallback
        flag_modified(entry, "analysis")
    await db.commit()
    await db.refresh(entry)
    return entry, analysis_ok


async def skip_daily_reflection(
    db: AsyncSession,
    *,
    telegram_user_id: int,
    session: AgentSession,
    day: date | None = None,
) -> KaizenJournalEntry:
    target = day or local_date()
    entry = await get_or_create_entry(
        db,
        telegram_user_id=telegram_user_id,
        entry_type=DAILY_ENTRY_TYPE,
        period_start=target,
        period_end=target,
        source="system",
    )
    if entry.status != "completed":
        entry.status = "skipped"
        entry.remind_at = None
        await db.commit()
    await clear_pending_reflection(db, session=session)
    return entry


async def remind_daily_reflection_later(
    db: AsyncSession,
    *,
    telegram_user_id: int,
    session: AgentSession,
    day: date | None = None,
    now: datetime | None = None,
) -> KaizenJournalEntry:
    target = day or local_date(now)
    entry = await get_or_create_entry(
        db,
        telegram_user_id=telegram_user_id,
        entry_type=DAILY_ENTRY_TYPE,
        period_start=target,
        period_end=target,
        source="scheduled",
    )
    if entry.status != "completed":
        hours = max(1, min(int(settings.agent_evening_reflection_reminder_hours or 1), 6))
        requested = _utc(now or datetime.now(timezone.utc)) + timedelta(hours=hours)
        local_limit = datetime.combine(
            target + timedelta(days=1), time.min, tzinfo=manager_timezone()
        ).astimezone(timezone.utc)
        entry.status = "open"
        entry.remind_at = min(requested, local_limit - timedelta(minutes=1))
        await db.commit()
    await clear_pending_reflection(db, session=session)
    return entry


def reflection_invitation_text() -> str:
    return (
        "🌙 <b>Подведём итоги дня?</b>\n\n"
        "Расскажи одним сообщением или голосом, как прошёл сегодняшний день.\n\n"
        "Мне особенно важно понять:\n\n"
        "• что сегодня было хорошего и что получилось;\n"
        "• что не получилось, мешало или раздражало;\n"
        "• где потерялось время;\n"
        "• какие выводы или идеи появились;\n"
        "• что важно не забыть завтра.\n\n"
        "Не обязательно отвечать по пунктам — просто расскажи своими словами."
    )


def reflection_invitation_markup(day: date | None = None) -> dict[str, Any]:
    label = (day or local_date()).isoformat()
    return {
        "inline_keyboard": [
            [{"text": "🎙 Рассказать", "callback_data": f"agent:kaizen:start:{label}"}],
            [{"text": "⏰ Напомнить через час", "callback_data": f"agent:kaizen:later:{label}"}],
            [{"text": "⏭ Пропустить сегодня", "callback_data": f"agent:kaizen:skip:{label}"}],
        ]
    }


def format_daily_entry(entry: KaizenJournalEntry, *, analysis_ok: bool = True) -> str:
    data = normalise_daily_analysis(entry.analysis or {})
    lines = ["✅ <b>Итоги дня сохранены</b>"]
    sections = (
        ("Хорошее", data["good"]),
        ("Что мешало", data["difficulties"] or data["time_losses"]),
    )
    for title, items in sections:
        if items:
            lines.extend(["", f"<b>{title}:</b>"])
            lines.extend(f"• {html.escape(item)}" for item in items[:3])
    if data["lessons"]:
        lines.extend(["", "<b>Главный вывод:</b>", html.escape(data["lessons"][0])])
    if data["tomorrow_focus"]:
        lines.extend(["", "<b>Фокус на завтра:</b>"])
        lines.extend(f"• {html.escape(item)}" for item in data["tomorrow_focus"][:3])
    if not analysis_ok:
        lines.extend(
            [
                "",
                "⚠️ Рассказ сохранён, но сейчас не удалось структурировать выводы. "
                "Я использую эту запись при следующем анализе недели.",
            ]
        )
    else:
        lines.extend(["", "Я учту эту запись в итогах недели."])
    return "\n".join(lines)[:1500]


async def daily_entries_for_week(
    db: AsyncSession,
    *,
    telegram_user_id: int,
    start: date,
    end: date,
) -> list[KaizenJournalEntry]:
    result = await db.execute(
        select(KaizenJournalEntry)
        .where(
            KaizenJournalEntry.telegram_user_id == int(telegram_user_id),
            KaizenJournalEntry.entry_type == DAILY_ENTRY_TYPE,
            KaizenJournalEntry.period_start >= start,
            KaizenJournalEntry.period_end <= end,
            KaizenJournalEntry.status == "completed",
            KaizenJournalEntry.raw_text.is_not(None),
        )
        .order_by(KaizenJournalEntry.period_start.asc())
    )
    return list(result.scalars().all())


def _fallback_weekly(entries: Iterable[KaizenJournalEntry]) -> dict[str, Any]:
    rows = list(entries)
    wins: list[str] = []
    focus: list[str] = []
    for entry in rows:
        daily = normalise_daily_analysis(entry.analysis or {})
        wins.extend(daily["good"][:1])
        focus.extend(daily["tomorrow_focus"][:1])
    return normalise_weekly_analysis(
        {
            "summary": (
                "Записи недели сохранены. Для надёжного поиска повторяющихся проблем "
                "нужен доступ к AI-анализу."
            ),
            "main_wins": wins,
            "next_week_focus": focus,
            "insufficient_data": len(rows) < 3,
        },
        completed_days=len(rows),
    )


async def _operational_counts(db: AsyncSession) -> tuple[dict[str, int], str | None]:
    try:
        inbox = await next_action_service.build_inbox(db)
        return (
            {
                "overdue": len(inbox.overdue),
                "without_next": len(inbox.without_next),
                "waiting_us": len(inbox.waiting_us),
                "waiting_client": len(inbox.waiting_client),
                "stale": len(inbox.stale),
            },
            None,
        )
    except Exception as exc:
        await db.rollback()
        logger.info("Weekly kaizen without CRM snapshot: %s", exc.__class__.__name__)
        return {}, exc.__class__.__name__


async def build_weekly_review(
    db: AsyncSession,
    *,
    telegram_user_id: int,
    week_start: date | None = None,
    force_rebuild: bool = False,
    now: datetime | None = None,
) -> tuple[KaizenJournalEntry, bool]:
    start, end = week_period(week_start or local_date(now))
    entries = await daily_entries_for_week(
        db, telegram_user_id=telegram_user_id, start=start, end=end
    )
    # On a new week, /week should still find the most recently completed week.
    if week_start is None and not entries:
        previous_start = start - timedelta(days=7)
        previous_end = end - timedelta(days=7)
        previous = await daily_entries_for_week(
            db,
            telegram_user_id=telegram_user_id,
            start=previous_start,
            end=previous_end,
        )
        if previous:
            start, end, entries = previous_start, previous_end, previous

    weekly = await get_or_create_entry(
        db,
        telegram_user_id=telegram_user_id,
        entry_type=WEEKLY_ENTRY_TYPE,
        period_start=start,
        period_end=end,
        source="system",
    )
    if weekly.status == "completed" and weekly.analysis and not force_rebuild:
        return weekly, not bool((weekly.analysis or {}).get("analysis_unavailable"))

    journal_payload = [
        {
            "date": entry.period_start.isoformat(),
            "raw_text": entry.raw_text,
            "analysis": normalise_daily_analysis(entry.analysis or {}),
        }
        for entry in entries
    ]
    counts, crm_error = await _operational_counts(db)
    weekly.raw_text = json.dumps(journal_payload, ensure_ascii=False)[:_MAX_RAW_CHARS]
    weekly.status = "completed"
    weekly.source = "system"
    await db.commit()
    await db.refresh(weekly)

    analysis_ok = False
    try:
        parsed = await _structured_json(
            WEEKLY_SYSTEM_PROMPT,
            json.dumps(
                {
                    "period": {"start": start.isoformat(), "end": end.isoformat()},
                    "completed_days": len(entries),
                    "daily_entries": journal_payload,
                    "operational_counts": counts,
                    "crm_snapshot_unavailable": crm_error,
                },
                ensure_ascii=False,
            ),
        )
        analysis = normalise_weekly_analysis(parsed, completed_days=len(entries))
        analysis_ok = True
    except Exception as exc:
        logger.warning("Weekly kaizen analysis unavailable: %s", exc.__class__.__name__)
        analysis = _fallback_weekly(entries)
        analysis["analysis_unavailable"] = True
        analysis["analysis_error_type"] = exc.__class__.__name__
    analysis["crm_snapshot_unavailable"] = crm_error
    analysis["operational_counts"] = counts
    weekly.analysis = analysis
    flag_modified(weekly, "analysis")
    await db.commit()
    await db.refresh(weekly)
    return weekly, analysis_ok


def notion_improvements_available() -> bool:
    return bool(
        settings.notion_api_token.strip()
        and settings.notion_tasks_data_source_id.strip()
    )


def format_weekly_review(entry: KaizenJournalEntry, *, analysis_ok: bool = True) -> str:
    data = normalise_weekly_analysis(
        entry.analysis or {},
        completed_days=int((entry.analysis or {}).get("completed_days") or 0),
    )
    lines = [
        "📊 <b>Итоги недели</b>",
        f"{entry.period_start.strftime('%d.%m')}–{entry.period_end.strftime('%d.%m.%Y')}",
    ]
    if data["main_wins"]:
        lines.extend(["", "<b>Что получилось</b>"])
        lines.extend(f"• {html.escape(item)}" for item in data["main_wins"][:4])
    if data["recurring_problems"]:
        lines.extend(["", "<b>Что повторялось</b>"])
        for item in data["recurring_problems"][:4]:
            lines.append(
                f"• {html.escape(item['problem'])} ({int(item['days_count'])} дн.)"
            )
    if data["summary"]:
        lines.extend(["", "<b>Главный вывод</b>", html.escape(data["summary"])])
    if data["next_week_focus"]:
        lines.extend(["", "<b>Фокус следующей недели</b>"])
        lines.extend(
            f"{index}. {html.escape(item)}"
            for index, item in enumerate(data["next_week_focus"][:3], 1)
        )
    candidates = data["improvement_candidates"][:3]
    if candidates:
        lines.extend(["", "<b>Предлагаемые улучшения для Notion</b>"])
        lines.extend(
            f"{index}. {html.escape(item['title'])}"
            for index, item in enumerate(candidates, 1)
        )
        lines.extend(["", "Создать эти карточки в Notion?"])
    if data["insufficient_data"]:
        lines.extend(
            [
                "",
                "ℹ️ Заполнено меньше трёх дней. Наблюдения полезны, но пока не считаются системной закономерностью.",
            ]
        )
    if (entry.analysis or {}).get("crm_snapshot_unavailable"):
        lines.extend(["", "⚠️ Операционные данные CRM не были добавлены; отчёт построен по дневнику."])
    if not analysis_ok:
        lines.extend(["", "⚠️ AI-анализ временно недоступен; локальные записи сохранены."])
    return "\n".join(lines)[:3900]


def weekly_review_markup(entry: KaizenJournalEntry) -> dict[str, Any] | None:
    candidates = normalise_weekly_analysis(
        entry.analysis or {},
        completed_days=int((entry.analysis or {}).get("completed_days") or 0),
    )["improvement_candidates"]
    rows: list[list[dict[str, str]]] = []
    if candidates and notion_improvements_available() and not entry.notion_page_ids:
        rows.append(
            [
                {
                    "text": "✅ Создать в Notion",
                    "callback_data": f"agent:kaizen:weekcreate:{entry.id}",
                },
                {
                    "text": "✏️ Не создавать",
                    "callback_data": f"agent:kaizen:weekcancel:{entry.id}",
                },
            ]
        )
    rows.append(
        [
            {
                "text": "🔄 Пересобрать выводы",
                "callback_data": f"agent:kaizen:weekrebuild:{entry.id}",
            }
        ]
    )
    return {"inline_keyboard": rows}


def notion_action_preview(entry: KaizenJournalEntry) -> tuple[str, list[dict[str, Any]]]:
    analysis = normalise_weekly_analysis(
        entry.analysis or {},
        completed_days=int((entry.analysis or {}).get("completed_days") or 0),
    )
    items = analysis["improvement_candidates"][:3]
    lines = [
        "<b>Создать карточки улучшений в Notion?</b>",
        "",
        f"Период: {entry.period_start.isoformat()} — {entry.period_end.isoformat()}",
    ]
    for index, item in enumerate(items, 1):
        lines.extend(
            [
                "",
                f"<b>{index}. {html.escape(item['title'])}</b>",
                html.escape(item["proposed_action"]),
            ]
        )
    lines.extend(
        [
            "",
            "Будет использована существующая база Tasks. Kommo, WhatsApp и Gmail не изменяются.",
        ]
    )
    return "\n".join(lines)[:3900], items


def _property_payload(prop: dict[str, Any], value: str) -> dict[str, Any] | None:
    prop_type = str(prop.get("type") or "")
    if prop_type == "select":
        return {"select": {"name": value}}
    if prop_type == "status":
        return {"status": {"name": value}}
    if prop_type == "rich_text":
        return notion_gateway._rich_text(value)  # noqa: SLF001
    return None


def _paragraph(text: str) -> dict[str, Any]:
    return {
        "object": "block",
        "type": "paragraph",
        "paragraph": {
            "rich_text": [
                {"type": "text", "text": {"content": str(text or "—")[:1900]}}
            ]
        },
    }


def _heading(text: str) -> dict[str, Any]:
    return {
        "object": "block",
        "type": "heading_2",
        "heading_2": {
            "rich_text": [
                {"type": "text", "text": {"content": str(text)[:200]}}
            ]
        },
    }


async def create_notion_improvement_page(
    *,
    weekly_entry: KaizenJournalEntry,
    item: dict[str, Any],
    index: int,
) -> dict[str, Any]:
    source_id = settings.notion_tasks_data_source_id
    source = await notion_gateway.retrieve_data_source(source_id)
    properties_meta = source.get("properties") or {}
    title_name = next(
        (
            name
            for name in ("Задача", "Name", "Название")
            if str((properties_meta.get(name) or {}).get("type")) == "title"
        ),
        None,
    )
    if not title_name:
        title_name = next(
            (name for name, prop in properties_meta.items() if prop.get("type") == "title"),
            None,
        )
    if not title_name:
        raise notion_gateway.OperationalNotionError("В базе Tasks нет title-свойства.")

    external_id = f"kaizen:{weekly_entry.id}:{index}"
    ext_prop_name = next(
        (name for name in ("External ID", "External ID ") if name in properties_meta),
        None,
    )
    if ext_prop_name and properties_meta[ext_prop_name].get("type") == "rich_text":
        existing = await notion_gateway.query_by_text(source_id, ext_prop_name, external_id)
        if existing:
            page = existing[0]
            return {
                "id": page.get("id"),
                "url": page.get("url") or notion_gateway.notion_page_url(str(page.get("id") or "")),
                "created": False,
                "external_id": external_id,
            }

    properties: dict[str, Any] = {
        title_name: notion_gateway._title(str(item.get("title") or "Kaizen improvement"))  # noqa: SLF001
    }
    for names, value in (
        (("Тип", "Type"), "Improvement"),
        (("Статус", "Status"), "Todo"),
        (("Источник", "Source"), "Kaizen"),
    ):
        name = next((candidate for candidate in names if candidate in properties_meta), None)
        if name:
            payload = _property_payload(properties_meta[name], value)
            if payload:
                properties[name] = payload
    if ext_prop_name:
        payload = _property_payload(properties_meta[ext_prop_name], external_id)
        if payload:
            properties[ext_prop_name] = payload
    due = item.get("due_date")
    due_name = next((name for name in ("Срок", "Due") if name in properties_meta), None)
    if due and due_name and properties_meta[due_name].get("type") == "date":
        properties[due_name] = {"date": {"start": str(due)}}

    body = [
        _heading("Проблема"),
        _paragraph(str(item.get("problem") or "—")),
        _heading("Наблюдения недели"),
        _paragraph(str(item.get("evidence") or "—")),
        _heading("Предлагаемое изменение"),
        _paragraph(str(item.get("proposed_action") or "—")),
        _heading("Ожидаемый эффект"),
        _paragraph(str(item.get("expected_effect") or "—")),
        _heading("Как проверить результат"),
        _paragraph(str(item.get("verification") or "—")),
        _heading("Период анализа"),
        _paragraph(f"{weekly_entry.period_start.isoformat()} — {weekly_entry.period_end.isoformat()}"),
    ]
    data = await notion_gateway._request(  # noqa: SLF001
        "POST",
        "/pages",
        json={
            "parent": {
                "type": "data_source_id",
                "data_source_id": notion_gateway._data_source_id(source_id),  # noqa: SLF001
            },
            "properties": properties,
            "children": body,
        },
    )
    return {
        "id": data["id"],
        "url": data.get("url") or notion_gateway.notion_page_url(data["id"]),
        "created": True,
        "external_id": external_id,
    }


async def create_notion_kaizen_item(
    *,
    title: str,
    details: str,
    item_kind: str,
    external_id: str,
) -> dict[str, Any]:
    """Create one explicitly confirmed voice/text capture in the Kaizen board."""
    source_id = settings.notion_tasks_data_source_id
    source = await notion_gateway.retrieve_data_source(source_id)
    properties_meta = source.get("properties") or {}
    title_name = next(
        (
            name
            for name in ("Задача", "Name", "Название")
            if str((properties_meta.get(name) or {}).get("type")) == "title"
        ),
        None,
    ) or next(
        (name for name, prop in properties_meta.items() if prop.get("type") == "title"),
        None,
    )
    if not title_name:
        raise notion_gateway.OperationalNotionError("В базе Tasks нет title-свойства.")

    ext_prop_name = next(
        (name for name in ("External ID", "External ID ") if name in properties_meta),
        None,
    )
    if ext_prop_name and properties_meta[ext_prop_name].get("type") == "rich_text":
        existing = await notion_gateway.query_by_text(source_id, ext_prop_name, external_id)
        if existing:
            page = existing[0]
            return {
                "id": page.get("id"),
                "url": page.get("url")
                or notion_gateway.notion_page_url(str(page.get("id") or "")),
                "created": False,
                "external_id": external_id,
            }

    properties: dict[str, Any] = {
        title_name: notion_gateway._title(title[:200])  # noqa: SLF001
    }
    for names, value in (
        (("Тип", "Type"), "Improvement"),
        (("Статус", "Status"), "Todo"),
        (("Источник", "Source"), "Kaizen"),
    ):
        name = next((candidate for candidate in names if candidate in properties_meta), None)
        if name:
            payload = _property_payload(properties_meta[name], value)
            if payload:
                properties[name] = payload
    if ext_prop_name:
        payload = _property_payload(properties_meta[ext_prop_name], external_id)
        if payload:
            properties[ext_prop_name] = payload

    data = await notion_gateway._request(  # noqa: SLF001
        "POST",
        "/pages",
        json={
            "parent": {
                "type": "data_source_id",
                "data_source_id": notion_gateway._data_source_id(source_id),  # noqa: SLF001
            },
            "properties": properties,
            "children": [
                _heading("Категория"),
                _paragraph(item_kind),
                _heading("Запись"),
                _paragraph(details),
                _heading("Источник"),
                _paragraph("Голосовая или текстовая команда менеджера → Kaizen"),
            ],
        },
    )
    return {
        "id": data["id"],
        "url": data.get("url") or notion_gateway.notion_page_url(data["id"]),
        "created": True,
        "external_id": external_id,
    }


async def claim_evening_invitation(
    db: AsyncSession,
    *,
    telegram_user_id: int,
    now: datetime | None = None,
) -> KaizenJournalEntry | None:
    day = local_date(now)
    entry = await get_or_create_entry(
        db,
        telegram_user_id=telegram_user_id,
        entry_type=DAILY_ENTRY_TYPE,
        period_start=day,
        period_end=day,
        source="scheduled",
    )
    entry = await get_entry(
        db,
        telegram_user_id=telegram_user_id,
        entry_type=DAILY_ENTRY_TYPE,
        period_start=day,
        period_end=day,
        lock=True,
    )
    if entry is None or entry.status in {"completed", "skipped"}:
        return None
    meta = dict(entry.analysis or {})
    scheduler = dict(meta.get("scheduler") or {})
    if scheduler.get("invitation_sent_at"):
        return None
    claimed = scheduler.get("invitation_claimed_at")
    if claimed:
        try:
            claimed_at = datetime.fromisoformat(str(claimed).replace("Z", "+00:00"))
            if _utc(claimed_at) > datetime.now(timezone.utc) - timedelta(minutes=10):
                return None
        except ValueError:
            pass
    scheduler["invitation_claimed_at"] = datetime.now(timezone.utc).isoformat()
    meta["scheduler"] = scheduler
    entry.analysis = meta
    flag_modified(entry, "analysis")
    await db.commit()
    return entry


async def mark_evening_invitation_sent(
    db: AsyncSession, *, entry: KaizenJournalEntry
) -> None:
    meta = dict(entry.analysis or {})
    scheduler = dict(meta.get("scheduler") or {})
    scheduler["invitation_sent_at"] = datetime.now(timezone.utc).isoformat()
    scheduler.pop("invitation_claimed_at", None)
    meta["scheduler"] = scheduler
    entry.analysis = meta
    flag_modified(entry, "analysis")
    await db.commit()


async def release_evening_invitation_claim(
    db: AsyncSession, *, entry: KaizenJournalEntry
) -> None:
    meta = dict(entry.analysis or {})
    scheduler = dict(meta.get("scheduler") or {})
    scheduler.pop("invitation_claimed_at", None)
    meta["scheduler"] = scheduler
    entry.analysis = meta
    flag_modified(entry, "analysis")
    await db.commit()


async def claim_due_reminders(
    db: AsyncSession, *, now: datetime | None = None
) -> list[KaizenJournalEntry]:
    current = _utc(now or datetime.now(timezone.utc))
    rows = list(
        (
            await db.execute(
                select(KaizenJournalEntry)
                .where(
                    KaizenJournalEntry.entry_type == DAILY_ENTRY_TYPE,
                    KaizenJournalEntry.status == "open",
                    KaizenJournalEntry.remind_at.is_not(None),
                    KaizenJournalEntry.remind_at <= current,
                )
                .with_for_update(skip_locked=True)
                .limit(50)
            )
        ).scalars().all()
    )
    for entry in rows:
        entry.remind_at = None
        meta = dict(entry.analysis or {})
        scheduler = dict(meta.get("scheduler") or {})
        scheduler["reminder_sent_at"] = current.isoformat()
        meta["scheduler"] = scheduler
        entry.analysis = meta
        flag_modified(entry, "analysis")
    if rows:
        await db.commit()
    return rows


async def claim_weekly_review(
    db: AsyncSession,
    *,
    telegram_user_id: int,
    now: datetime | None = None,
) -> tuple[date, date] | None:
    start, end = week_period(local_date(now))
    existing = await get_entry(
        db,
        telegram_user_id=telegram_user_id,
        entry_type=WEEKLY_ENTRY_TYPE,
        period_start=start,
        period_end=end,
        lock=True,
    )
    if existing is not None:
        return None
    entries = await daily_entries_for_week(
        db, telegram_user_id=telegram_user_id, start=start, end=end
    )
    minimum = max(1, int(settings.agent_weekly_review_min_daily_entries or 2))
    if len(entries) < minimum:
        return None
    row = await get_or_create_entry(
        db,
        telegram_user_id=telegram_user_id,
        entry_type=WEEKLY_ENTRY_TYPE,
        period_start=start,
        period_end=end,
        source="scheduled",
    )
    meta = dict(row.analysis or {})
    meta["scheduler"] = {"weekly_claimed_at": datetime.now(timezone.utc).isoformat()}
    row.analysis = meta
    flag_modified(row, "analysis")
    await db.commit()
    return start, end
