"""High-fidelity company/personal evening journal with optional Notion projection."""
from __future__ import annotations

import hashlib
import html
import logging
import os
from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy.orm.attributes import flag_modified

from app.agent import memory, notion_gateway, service as agent_service
from app.agent.contracts import AgentReply
from app.services import kaizen_journal_service, kaizen_source_guard_runtime

logger = logging.getLogger(__name__)
_INSTALLED = False
_PENDING_KEY = "pending_daily_reflection"
COMPANY = "company"
PERSONAL = "personal"
PERSONAL_ENTRY_TYPE = "daily_personal"
MAX_POINTS = 40

LABELS = {
    "result": "✅ Результаты / сделано",
    "problem": "⚠️ Явно названные проблемы / трудности",
    "observation": "🔎 Наблюдения / закономерности",
    "idea": "💡 Идеи",
    "decision": "🎯 Решения",
    "lesson": "🧭 Выводы",
    "tomorrow": "📌 Что важно дальше",
    "self_observation": "👤 Наблюдения о себе",
    "neutral": "📝 Другие важные факты",
}

SYSTEM_PROMPT = """You structure a private daily journal for the owner of Buy & Bring Solutions.
Return ONLY valid JSON in Russian with this exact shape:
{
 "clean_text":"complete corrected readable transcript",
 "important_points":[{"category":"result|problem|observation|idea|decision|lesson|tomorrow|self_observation|neutral","text":"one grounded point","evidence":"short grounded evidence"}],
 "main_conclusion":null
}
Rules:
- Fidelity is more important than compression. Preserve EVERY substantive fact, result, observation, idea, decision and intention. Up to 40 points; never arbitrarily choose only three.
- clean_text is the whole story with obvious speech-recognition/grammar errors corrected and repetitions cleaned only when meaning is clear. Do not add facts or remove meaning.
- Never infer causes, motives, blockers, emotions or importance the speaker did not state.
- Use category=problem only when the speaker explicitly says something failed, blocked, irritated, wasted time or did not work. "Разобрался с проблемами в банке" is an achieved action/result, not proof that the bank blocked the day.
- If classification is uncertain, use neutral. Separate an observation from an idea/action based on that observation.
- main_conclusion may be null; fill it only if explicitly stated or directly supported without speculation.
"""


def invitation_text() -> str:
    return (
        "🌙 <b>Подведём итоги дня?</b>\n\n"
        "Можно заполнить оба дневника по очереди.\n\n"
        "🏢 <b>О фирме</b> — клиенты, реклама, проекты, документы, процессы, идеи и решения.\n\n"
        "👤 <b>О себе</b> — личные мысли, наблюдения и то, что хочется улучшить. "
        "Личный дневник хранится отдельно от рабочего.\n\n"
        "Я сохраню полный смысл рассказа, сделаю читаемый исправленный текст и отдельно "
        "структурирую его без додумывания причин и фактов."
    )


def invitation_markup(day: date | None = None) -> dict[str, Any]:
    d = (day or kaizen_journal_service.local_date()).isoformat()
    return {"inline_keyboard": [
        [{"text": "🏢 Рассказать о фирме", "callback_data": f"agent:reflection:company:{d}"}],
        [{"text": "👤 Рассказать о себе", "callback_data": f"agent:reflection:personal:{d}"}],
        [{"text": "⏰ Напомнить через час", "callback_data": f"agent:kaizen:later:{d}"}],
        [{"text": "⏭ Пропустить сегодня", "callback_data": f"agent:kaizen:skip:{d}"}],
    ]}


def _prompt(scope: str) -> str:
    if scope == PERSONAL:
        return (
            "👤 <b>Личный дневник</b>\n\nРасскажи одним текстовым или голосовым сообщением всё, "
            "что хочешь сохранить о себе. Эта запись не попадёт в рабочий недельный анализ компании."
        )
    return (
        "🏢 <b>Дневник компании</b>\n\nРасскажи одним текстовым или голосовым сообщением всё важное "
        "за день. Я не буду сокращать рассказ до трёх пунктов и не буду придумывать причины, которых ты не называл."
    )


def _page_id(scope: str) -> str:
    key = "NOTION_PERSONAL_JOURNAL_PAGE_ID" if scope == PERSONAL else "NOTION_COMPANY_JOURNAL_PAGE_ID"
    return os.getenv(key, "").strip()


def _clean(v: Any) -> str:
    return " ".join(str(v or "").split()).strip()


def _normalise(parsed: Any, raw: str) -> dict[str, Any]:
    data = parsed if isinstance(parsed, dict) else {}
    points: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in data.get("important_points") or []:
        if not isinstance(item, dict):
            continue
        category = str(item.get("category") or "neutral").casefold()
        category = category if category in LABELS else "neutral"
        text, evidence = _clean(item.get("text")), _clean(item.get("evidence"))
        if not text:
            continue
        # Deterministic guard for the exact failure mode reported by the owner.
        sample = f"{text} {evidence}".casefold().replace("ё", "е")
        if category == "problem" and any(x in sample for x in (
            "разобрался с проблем", "решил проблем", "закрыл проблем", "устранил проблем"
        )) and not any(x in sample for x in (
            "мешал", "помешал", "блокировал", "не получилось", "не удалось", "раздражал", "потерял время"
        )):
            category = "result"
        key = (category, text.casefold())
        if key in seen:
            continue
        seen.add(key)
        points.append({"category": category, "text": text[:1000], "evidence": evidence[:700]})
        if len(points) >= MAX_POINTS:
            break
    if not points:
        points = [{"category": "neutral", "text": raw[:1000], "evidence": raw[:700]}]
    conclusion = _clean(data.get("main_conclusion")) or None
    return {
        "clean_text": str(data.get("clean_text") or raw).strip()[:50000],
        "important_points": points,
        "main_conclusion": conclusion[:1200] if conclusion else None,
    }


def _segment_id(user_id: int, day: date, scope: str, raw: str) -> str:
    return hashlib.sha256(f"{user_id}:{day}:{scope}:{raw}".encode()).hexdigest()[:20]


def _legacy_from_segments(segments: list[dict[str, Any]]) -> dict[str, Any]:
    out = {k: [] for k in ("good", "difficulties", "time_losses", "lessons", "ideas", "tomorrow_focus")}
    mapping = {"result": "good", "problem": "difficulties", "idea": "ideas", "decision": "lessons", "lesson": "lessons", "tomorrow": "tomorrow_focus"}
    for seg in segments:
        for point in seg.get("important_points") or []:
            target = mapping.get(str(point.get("category") or "")) if isinstance(point, dict) else None
            text = _clean(point.get("text")) if isinstance(point, dict) else ""
            if target and text and text not in out[target]:
                out[target].append(text)
    return {**out, "improvement_signals": [], "needs_followup": False, "followup_question": None}


def _chunks(text: str, size: int = 1800) -> list[str]:
    value = str(text or "").strip()
    return [value[i:i + size] for i in range(0, len(value), size)] if value else []


def _rt(text: str) -> list[dict[str, Any]]:
    return [{"type": "text", "text": {"content": str(text)[:1900]}}]


async def _notion_append(scope: str, day: date, segment: dict[str, Any]) -> tuple[str, str | None]:
    page_id = _page_id(scope)
    if not page_id:
        return "not_configured", None
    marker = f"BBS-REF-{segment['id']}"
    cursor = None
    for _ in range(20):
        params: dict[str, Any] = {"page_size": 100}
        if cursor:
            params["start_cursor"] = cursor
        listing = await notion_gateway._request("GET", f"/blocks/{page_id}/children", params=params)  # noqa: SLF001
        for block in listing.get("results") or []:
            kind = str(block.get("type") or "")
            plain = "".join(str(x.get("plain_text") or "") for x in (block.get(kind) or {}).get("rich_text") or [])
            if marker in plain:
                return "ok", notion_gateway.notion_page_url(page_id)
        if not listing.get("has_more"):
            break
        cursor = listing.get("next_cursor")
    scope_label = "Личное" if scope == PERSONAL else "Фирма"
    blocks: list[dict[str, Any]] = [
        {"object": "block", "type": "heading_2", "heading_2": {"rich_text": _rt(f"{day.strftime('%d.%m.%Y')} · {scope_label}")}},
        {"object": "block", "type": "heading_3", "heading_3": {"rich_text": _rt("Мой рассказ — очищенный текст")}},
    ]
    blocks += [{"object": "block", "type": "paragraph", "paragraph": {"rich_text": _rt(c)}} for c in _chunks(segment.get("clean_text") or "")]
    blocks.append({"object": "block", "type": "heading_3", "heading_3": {"rich_text": _rt("Как ИИ структурировал")}})
    for p in segment.get("important_points") or []:
        label = LABELS.get(str(p.get("category") or "neutral"), LABELS["neutral"])
        blocks.append({"object": "block", "type": "bulleted_list_item", "bulleted_list_item": {"rich_text": _rt(f"{label}: {_clean(p.get('text'))}")}})
    if segment.get("main_conclusion"):
        blocks += [
            {"object": "block", "type": "heading_3", "heading_3": {"rich_text": _rt("Вывод")}},
            {"object": "block", "type": "paragraph", "paragraph": {"rich_text": _rt(segment["main_conclusion"])}},
        ]
    blocks += [
        {"object": "block", "type": "paragraph", "paragraph": {"rich_text": _rt(marker)}},
        {"object": "block", "type": "divider", "divider": {}},
    ]
    await notion_gateway._request("PATCH", f"/blocks/{page_id}/children", json={"children": blocks[:100]})  # noqa: SLF001
    return "ok", notion_gateway.notion_page_url(page_id)


async def _set_scope(db: Any, session: Any, day: date, scope: str) -> None:
    ctx = dict(session.context or {})
    pending = dict(ctx.get(_PENDING_KEY) or {})
    now = datetime.now(timezone.utc)
    pending.update({
        "local_date": day.isoformat(), "scope": scope,
        "started_at": pending.get("started_at") or now.isoformat(),
        "expires_at": kaizen_journal_service._pending_expiry(now).isoformat(),  # noqa: SLF001
        "source": pending.get("source") or "command",
    })
    ctx[_PENDING_KEY] = pending
    session.context = ctx
    await db.commit()


async def save_reflection(db: Any, *, user_id: int, session: Any, text: str, source: str, scope: str) -> tuple[Any, bool, dict[str, Any]]:
    raw = kaizen_source_guard_runtime.redact_journal_text(text).strip()
    if not kaizen_journal_service.is_meaningful_reflection(raw):
        raise ValueError("Рассказ слишком короткий, чтобы сохранить итоги дня.")
    day = kaizen_journal_service.local_date()
    entry_type = PERSONAL_ENTRY_TYPE if scope == PERSONAL else kaizen_journal_service.DAILY_ENTRY_TYPE
    entry = await kaizen_journal_service.get_or_create_entry(
        db, telegram_user_id=user_id, entry_type=entry_type,
        period_start=day, period_end=day,
        source=kaizen_source_guard_runtime.storage_source(source),
    )
    analysis = dict(entry.analysis or {})
    journal = dict(analysis.get("journal_v2") or {})
    segments = [dict(x) for x in journal.get("segments") or [] if isinstance(x, dict)]
    sid = _segment_id(user_id, day, scope, raw)
    existing = next((x for x in segments if x.get("id") == sid), None)
    if existing and existing.get("ai_status") == "ok":
        await kaizen_journal_service.clear_pending_reflection(db, session=session)
        return entry, True, existing
    if existing is None:
        entry.raw_text = (f"{entry.raw_text}\n\nДополнение:\n{raw}" if entry.raw_text else raw)[:50000]
        entry.status, entry.remind_at = "completed", None
        entry.source = kaizen_source_guard_runtime.storage_source(source)
        existing = {"id": sid, "scope": scope, "raw_text": raw, "clean_text": raw, "important_points": [], "main_conclusion": None, "ai_status": "pending", "notion_status": "pending"}
        segments.append(existing)
    analysis["journal_v2"] = {"version": 2, "segments": segments}
    entry.analysis = analysis
    flag_modified(entry, "analysis")
    await kaizen_journal_service.clear_pending_reflection(db, session=session)
    await db.commit()  # local raw + id before AI/Notion
    try:
        await db.refresh(entry)
    except Exception:
        pass
    if scope == PERSONAL:
        control = await kaizen_journal_service.get_or_create_entry(
            db, telegram_user_id=user_id, entry_type=kaizen_journal_service.DAILY_ENTRY_TYPE,
            period_start=day, period_end=day, source="system",
        )
        if not control.raw_text:
            control.status, control.remind_at = "completed", None
            await db.commit()
    ok = False
    try:
        parsed = await kaizen_journal_service._structured_json(  # noqa: SLF001
            SYSTEM_PROMPT, f"LOCAL DATE: {day}\nSCOPE: {scope}\n\nSOURCE STORY:\n{raw}"
        )
        structured, ok = _normalise(parsed, raw), True
    except Exception as exc:
        logger.warning("Reflection v2 AI unavailable: %s", exc.__class__.__name__)
        structured = _normalise({}, raw)
    existing.update(structured)
    existing["ai_status"] = "ok" if ok else "unavailable"
    analysis = dict(entry.analysis or {})
    journal = dict(analysis.get("journal_v2") or {})
    current = [dict(x) for x in journal.get("segments") or [] if isinstance(x, dict)]
    current = [existing if x.get("id") == sid else x for x in current]
    analysis["journal_v2"] = {"version": 2, "segments": current}
    if scope == COMPANY:
        analysis.update(_legacy_from_segments(current))
    entry.analysis = analysis
    flag_modified(entry, "analysis")
    await db.commit()
    try:
        status, url = await _notion_append(scope, day, existing)
        existing["notion_status"], existing["notion_url"] = status, url
    except Exception as exc:
        logger.warning("Reflection v2 Notion unavailable: %s", exc.__class__.__name__)
        existing["notion_status"], existing["notion_error_type"] = "failed", exc.__class__.__name__
    analysis["journal_v2"] = {"version": 2, "segments": [existing if x.get("id") == sid else x for x in current]}
    entry.analysis = analysis
    flag_modified(entry, "analysis")
    await db.commit()
    return entry, ok, existing


def format_saved(segment: dict[str, Any], ok: bool) -> str:
    scope = str(segment.get("scope") or COMPANY)
    lines = [f"✅ <b>{'Личное' if scope == PERSONAL else 'Фирма'} · итоги дня сохранены</b>"]
    grouped: dict[str, list[str]] = {k: [] for k in LABELS}
    for p in segment.get("important_points") or []:
        if isinstance(p, dict):
            cat = str(p.get("category") or "neutral")
            cat = cat if cat in grouped else "neutral"
            text = _clean(p.get("text"))
            if text and text not in grouped[cat]:
                grouped[cat].append(text)
    for cat in ("result", "problem", "observation", "idea", "decision", "lesson", "tomorrow", "self_observation", "neutral"):
        if grouped[cat]:
            lines += ["", f"<b>{LABELS[cat]}</b>"] + [f"• {html.escape(x)}" for x in grouped[cat]]
    if segment.get("main_conclusion"):
        lines += ["", "<b>Главный вывод</b>", html.escape(str(segment["main_conclusion"]))]
    if not ok:
        lines += ["", "⚠️ Исходный рассказ сохранён полностью, но AI-структурирование сейчас недоступно."]
    ns = segment.get("notion_status")
    if ns == "ok":
        lines += ["", "📚 Полный очищенный текст добавлен в Notion."]
    elif ns == "not_configured":
        key = "NOTION_PERSONAL_JOURNAL_PAGE_ID" if scope == PERSONAL else "NOTION_COMPANY_JOURNAL_PAGE_ID"
        lines += ["", f"📚 Локально сохранено. Для автоматического Notion-дневника задай <code>{key}</code>."]
    elif ns == "failed":
        lines += ["", "⚠️ Локально сохранено, но Notion сейчас не обновился. Данные не потеряны."]
    text = "\n".join(lines)
    return text if len(text) <= 3900 else text[:3820].rstrip() + "\n\n…Полный список и текст сохранены в дневнике."


def install_reflection_journal_v2_runtime() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True
    original_set = kaizen_journal_service.set_pending_reflection
    original_active = kaizen_journal_service.active_pending_reflection

    async def set_choose(*args: Any, **kwargs: Any) -> None:
        await original_set(*args, **kwargs)
        db = args[0] if args else kwargs.get("db")
        session = kwargs.get("session")
        if db is not None and session is not None:
            ctx = dict(session.context or {})
            p = dict(ctx.get(_PENDING_KEY) or {})
            if p:
                p["scope"] = "choose"
                ctx[_PENDING_KEY], session.context = p, ctx
                await db.commit()

    async def active_scope(*args: Any, **kwargs: Any):
        p = await original_active(*args, **kwargs)
        return None if p and str(p.get("scope") or "choose") == "choose" else p

    kaizen_journal_service.set_pending_reflection = set_choose
    kaizen_journal_service.active_pending_reflection = active_scope
    kaizen_journal_service.reflection_invitation_text = invitation_text
    kaizen_journal_service.reflection_invitation_markup = invitation_markup

    original_message = agent_service.handle_message
    async def message(db: Any, *, chat_id: int, telegram_user_id: int, text: str, source: str = "text", allow_conversation_passthrough: bool = False, active_kommo_lead_id: int | None = None):
        session = await memory.get_or_create_session(db, telegram_user_id=telegram_user_id)
        p = dict((session.context or {}).get(_PENDING_KEY) or {})
        scope = str(p.get("scope") or "")
        if scope in {COMPANY, PERSONAL}:
            context = await memory.build_context(db, telegram_user_id=telegram_user_id, session=session)
            try:
                from app.services import kaizen_runtime
                explicit = kaizen_runtime._explicit_command_during_reflection(text, context)  # noqa: SLF001
            except Exception:
                explicit = str(text or "").strip().startswith("/")
            if not explicit:
                try:
                    entry, ok, seg = await save_reflection(db, user_id=telegram_user_id, session=session, text=text, source=source, scope=scope)
                    other = PERSONAL if scope == COMPANY else COMPANY
                    label = "👤 Добавить личные итоги" if other == PERSONAL else "🏢 Добавить итоги фирмы"
                    return AgentReply(
                        format_saved(seg, ok),
                        reply_markup={"inline_keyboard": [[{"text": label, "callback_data": f"agent:reflection:{other}:{entry.period_start.isoformat()}"}]]},
                        intent=f"daily_reflection_{scope}_saved",
                        metadata={"entry_id": int(entry.id), "scope": scope, "segment_id": seg.get("id"), "notion_status": seg.get("notion_status")},
                    )
                except ValueError as exc:
                    return AgentReply(f"❓ {html.escape(str(exc))}", intent="daily_reflection_followup")
        return await original_message(
            db, chat_id=chat_id, telegram_user_id=telegram_user_id, text=text, source=source,
            allow_conversation_passthrough=allow_conversation_passthrough,
            active_kommo_lead_id=active_kommo_lead_id,
        )
    agent_service.handle_message = message

    original_callback = agent_service.handle_callback
    async def callback(db: Any, *, callback_data: str, telegram_user_id: int, chat_id: int | None = None):
        parts = callback_data.split(":")
        if len(parts) == 4 and parts[:2] == ["agent", "reflection"] and parts[2] in {COMPANY, PERSONAL}:
            try:
                day = date.fromisoformat(parts[3])
            except ValueError:
                return AgentReply("⌛ Дата дневника устарела.", intent="reflection_v2_stale")
            if day != kaizen_journal_service.local_date():
                return AgentReply("⌛ Этот вечерний запрос уже устарел.", intent="reflection_v2_stale")
            session = await memory.get_or_create_session(db, telegram_user_id=telegram_user_id)
            await _set_scope(db, session, day, parts[2])
            return AgentReply(_prompt(parts[2]), intent=f"daily_reflection_{parts[2]}_waiting")
        return await original_callback(db, callback_data=callback_data, telegram_user_id=telegram_user_id, chat_id=chat_id)
    agent_service.handle_callback = callback
