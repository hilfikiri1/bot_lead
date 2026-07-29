"""Unified project communication timeline and deterministic promise analysis."""
from __future__ import annotations

import hashlib
import html
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.client_message_draft import ClientMessageDraft
from app.models.project_event import ProjectEvent
from app.services import kommo_chat_service, kommo_service

_DIRECTION_LABELS = {"incoming": "Клиент", "outgoing": "Мы", "internal": "Внутреннее"}
_PROMISE_MARKERS = (
    "skontaktuję",
    "zadzwonię",
    "wyślę",
    "prześlę",
    "przygotuję",
    "sprawdzę",
    "wrócę",
    "skontaktujemy",
    "wyślemy",
    "przygotujemy",
    "sprawdzimy",
    "позвоню",
    "свяжусь",
    "отправлю",
    "пришлю",
    "подготовлю",
    "проверю",
    "уточню",
    "вернусь",
    "позвоним",
    "отправим",
    "подготовим",
    "зателефоную",
    "зв'яжусь",
    "надішлю",
    "підготую",
    "перевірю",
    "уточню",
    "i will",
    "we will",
    "i'll",
    "we'll",
)
_REQUEST_MARKERS = (
    "proszę",
    "potrzebuję",
    "chciałbym",
    "chciałabym",
    "просим",
    "прошу",
    "нужно",
    "хочу",
    "потрібно",
    "прошу",
    "please",
    "need",
    "could you",
)


@dataclass
class CommunicationEntry:
    id: str
    occurred_at: datetime
    channel: str
    direction: str
    text: str
    author: str | None = None
    source: str = "unknown"
    external_id: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class PromiseItem:
    text: str
    made_at: datetime
    due_at: datetime | None
    channel: str
    overdue: bool = False


@dataclass
class CommunicationAnalysis:
    last_contact_at: datetime | None = None
    last_channel: str | None = None
    last_direction: str | None = None
    waiting_on: str | None = None
    last_client_message: str | None = None
    last_manager_message: str | None = None
    client_requests: list[str] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)
    promises_by_us: list[PromiseItem] = field(default_factory=list)
    overdue_promises: list[PromiseItem] = field(default_factory=list)
    recommended_action: str | None = None
    summary: str = ""


@dataclass
class UnifiedCommunicationResult:
    lead_id: int
    entries: list[CommunicationEntry]
    analysis: CommunicationAnalysis
    source_errors: list[str] = field(default_factory=list)


def _aware_timestamp(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(int(value), tz=timezone.utc)
    raw = str(value or "").strip()
    if raw:
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return datetime.fromtimestamp(0, tz=timezone.utc)


def _clean(value: Any, *, limit: int = 6000) -> str:
    return " ".join(str(value or "").replace("\x00", " ").split())[:limit]


def _entry_id(*parts: Any) -> str:
    raw = ":".join(str(part or "") for part in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _normalize_direction(value: Any) -> str:
    raw = str(value or "").casefold()
    if raw in {"incoming", "client", "contact", "in"}:
        return "incoming"
    if raw in {"outgoing", "manager", "operator", "out", "us"}:
        return "outgoing"
    return "internal"


def _channel(value: Any, *, fallback: str = "Kommo") -> str:
    raw = _clean(value, limit=80)
    if not raw:
        return fallback
    lowered = raw.casefold()
    if "facebook" in lowered or lowered == "fb":
        return "Facebook"
    if "whatsapp" in lowered or lowered in {"wa", "waba"}:
        return "WhatsApp"
    if "instagram" in lowered:
        return "Instagram"
    if "mail" in lowered or "email" in lowered:
        return "Email"
    if "telegram" in lowered:
        return "Telegram"
    if "call" in lowered or "звон" in lowered or "телефон" in lowered:
        return "Звонок"
    return raw[:80]


def _note_direction_and_channel(text: str) -> tuple[str, str]:
    lowered = text.casefold()
    if "[bbs-wa-in-" in lowered or "входящее whatsapp" in lowered:
        return "incoming", "WhatsApp"
    if "[bbs-msg-" in lowered or "сообщение отправлено вручную" in lowered:
        return "outgoing", "WhatsApp"
    if "facebook" in lowered:
        return ("incoming" if "входящ" in lowered else "internal"), "Facebook"
    if "email" in lowered or "e-mail" in lowered:
        return ("outgoing" if "отправ" in lowered else "internal"), "Email"
    return "internal", "Примечание Kommo"


def _extract_message_from_note(text: str) -> str:
    for marker in ("Текст:", "Сообщение:"):
        if marker in text:
            return _clean(text.split(marker, 1)[1])
    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    if lines and lines[0].startswith("[BBS-"):
        lines = lines[1:]
    return _clean("\n".join(lines))


async def _chat_entries(lead_id: int) -> tuple[list[CommunicationEntry], str | None]:
    entries: list[CommunicationEntry] = []
    try:
        talks = await kommo_chat_service.get_lead_talks(lead_id, limit=20)
        for talk in talks[:8]:
            talk_id = int(talk.get("talk_id") or 0)
            if not talk_id:
                continue
            messages = await kommo_chat_service.get_talk_messages(talk_id, limit=100)
            origin = _channel(talk.get("origin"), fallback="Чат Kommo")
            for item in messages:
                text = _clean(item.get("text") or "[вложение]")
                if not text:
                    continue
                external_id = str(item.get("id") or "") or None
                entries.append(
                    CommunicationEntry(
                        id=_entry_id("chat", talk_id, external_id, text),
                        occurred_at=_aware_timestamp(item.get("created_at")),
                        channel=_channel(item.get("origin"), fallback=origin),
                        direction=_normalize_direction(item.get("direction")),
                        text=text,
                        author=_clean(item.get("author_name"), limit=200) or None,
                        source="kommo_chat",
                        external_id=external_id,
                        payload={"talk_id": talk_id},
                    )
                )
        return entries, None
    except PermissionError:
        return entries, "Kommo: нет разрешения External chat history"
    except Exception as exc:
        return entries, f"Kommo chat: {type(exc).__name__}"


async def _note_entries(lead_id: int) -> tuple[list[CommunicationEntry], str | None]:
    try:
        notes = await kommo_service.get_recent_common_notes(lead_id, limit=100)
    except Exception as exc:
        return [], f"Kommo notes: {type(exc).__name__}"
    entries: list[CommunicationEntry] = []
    for note in notes:
        raw = str(note.get("text") or "").strip()
        if not raw:
            continue
        direction, channel = _note_direction_and_channel(raw)
        text = _extract_message_from_note(raw)
        external_id = str(note.get("id") or "") or None
        entries.append(
            CommunicationEntry(
                id=_entry_id("note", external_id, text),
                occurred_at=_aware_timestamp(note.get("created_at")),
                channel=channel,
                direction=direction,
                text=text,
                author=None,
                source="kommo_note",
                external_id=external_id,
            )
        )
    return entries, None


async def _draft_entries(db: AsyncSession, lead_id: int) -> list[CommunicationEntry]:
    result = await db.execute(
        select(ClientMessageDraft)
        .where(
            ClientMessageDraft.kommo_lead_id == int(lead_id),
            ClientMessageDraft.status == "sent",
        )
        .order_by(desc(ClientMessageDraft.sent_at), desc(ClientMessageDraft.id))
        .limit(100)
    )
    entries: list[CommunicationEntry] = []
    for draft in result.scalars().all():
        occurred = draft.sent_at or draft.sent_confirmed_at or draft.updated_at or draft.created_at
        entries.append(
            CommunicationEntry(
                id=_entry_id("draft", draft.id, draft.delivery_marker),
                occurred_at=_aware_timestamp(occurred),
                channel=_channel(draft.channel, fallback="WhatsApp"),
                direction="outgoing",
                text=_clean(draft.body),
                author=None,
                source="client_message_draft",
                external_id=str(draft.id),
                payload={
                    "delivery_marker": draft.delivery_marker,
                    "language": draft.communication_language,
                },
            )
        )
    return entries


async def _project_event_entries(db: AsyncSession, lead_id: int) -> list[CommunicationEntry]:
    result = await db.execute(
        select(ProjectEvent)
        .where(
            ProjectEvent.kommo_lead_id == int(lead_id),
            ProjectEvent.event_type.in_(
                (
                    "note",
                    "call",
                    "whatsapp",
                    "email",
                    "voice",
                    "followup",
                    "conversation",
                    "promise",
                )
            ),
        )
        .order_by(desc(ProjectEvent.occurred_at))
        .limit(100)
    )
    entries: list[CommunicationEntry] = []
    for event in result.scalars().all():
        payload = dict(event.payload_json or {})
        direction = _normalize_direction(payload.get("direction"))
        entries.append(
            CommunicationEntry(
                id=_entry_id("event", event.id, event.idempotency_key),
                occurred_at=_aware_timestamp(event.occurred_at),
                channel=_channel(payload.get("channel") or event.event_type),
                direction=direction,
                text=_clean(event.summary or event.title),
                author=event.actor,
                source=event.source or "project_event",
                external_id=event.external_id,
                payload=payload,
            )
        )
    return entries


def _deduplicate(entries: list[CommunicationEntry]) -> list[CommunicationEntry]:
    result: list[CommunicationEntry] = []
    seen_external: set[tuple[str, str]] = set()
    seen_content: set[str] = set()
    for entry in sorted(entries, key=lambda item: (item.occurred_at, item.id)):
        if entry.external_id:
            external_key = (entry.channel.casefold(), str(entry.external_id))
            if external_key in seen_external:
                continue
            seen_external.add(external_key)
        normalized = re.sub(r"\W+", "", entry.text.casefold())[:1000]
        minute = int(entry.occurred_at.timestamp() // 60) if entry.occurred_at.timestamp() > 0 else 0
        content_key = _entry_id(entry.direction, normalized, minute)
        if normalized and content_key in seen_content:
            continue
        if normalized:
            seen_content.add(content_key)
        result.append(entry)
    return result


def _sentences(text: str) -> list[str]:
    return [
        part.strip()
        for part in re.split(r"(?<=[.!?？])\s+|\n+", str(text or ""))
        if part.strip()
    ]


def _promise_due(sentence: str, made_at: datetime) -> datetime | None:
    lower = sentence.casefold()
    local = made_at.astimezone()
    if any(token in lower for token in ("jutro", "завтра", "завтра", "tomorrow")):
        return (local + timedelta(days=1)).replace(hour=17, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    match = re.search(r"(?:do|до|before|by)\s+(\d{1,2})[:.]([0-5]\d)", lower)
    if match:
        return local.replace(hour=int(match.group(1)), minute=int(match.group(2)), second=0, microsecond=0).astimezone(timezone.utc)
    date_match = re.search(r"\b(\d{1,2})[./-](\d{1,2})(?:[./-](\d{2,4}))?\b", lower)
    if date_match:
        day, month = int(date_match.group(1)), int(date_match.group(2))
        year_raw = date_match.group(3)
        year = local.year if not year_raw else int(year_raw)
        if year < 100:
            year += 2000
        try:
            target = local.replace(year=year, month=month, day=day, hour=17, minute=0, second=0, microsecond=0)
            if target < local and not year_raw:
                target = target.replace(year=year + 1)
            return target.astimezone(timezone.utc)
        except ValueError:
            return None
    return None


def analyze_entries(
    entries: list[CommunicationEntry], *, now: datetime | None = None
) -> CommunicationAnalysis:
    now = now or datetime.now(timezone.utc)
    directional = [item for item in entries if item.direction in {"incoming", "outgoing"}]
    last = directional[-1] if directional else None
    incoming = [item for item in directional if item.direction == "incoming"]
    outgoing = [item for item in directional if item.direction == "outgoing"]
    analysis = CommunicationAnalysis()
    if last:
        analysis.last_contact_at = last.occurred_at
        analysis.last_channel = last.channel
        analysis.last_direction = last.direction
        analysis.waiting_on = "us" if last.direction == "incoming" else "client"
    if incoming:
        analysis.last_client_message = incoming[-1].text
    if outgoing:
        analysis.last_manager_message = outgoing[-1].text

    requests: list[str] = []
    for item in incoming[-15:]:
        for sentence in _sentences(item.text):
            lowered = sentence.casefold()
            if "?" in sentence or "？" in sentence or any(marker in lowered for marker in _REQUEST_MARKERS):
                requests.append(sentence[:600])
    analysis.client_requests = list(dict.fromkeys(requests))[-10:]
    if last and last.direction == "incoming":
        analysis.open_questions = [
            sentence[:600]
            for sentence in _sentences(last.text)
            if "?" in sentence or "？" in sentence
        ][:8]

    promises: list[PromiseItem] = []
    for item in outgoing[-30:]:
        for sentence in _sentences(item.text):
            lowered = sentence.casefold()
            if any(marker in lowered for marker in _PROMISE_MARKERS):
                due_at = _promise_due(sentence, item.occurred_at)
                promises.append(
                    PromiseItem(
                        text=sentence[:700],
                        made_at=item.occurred_at,
                        due_at=due_at,
                        channel=item.channel,
                    )
                )
    for promise in promises:
        later_outgoing = any(
            item.direction == "outgoing" and item.occurred_at > (promise.due_at or promise.made_at)
            for item in directional
        )
        promise.overdue = bool(promise.due_at and promise.due_at < now and not later_outgoing)
    analysis.promises_by_us = promises[-10:]
    analysis.overdue_promises = [item for item in promises if item.overdue]

    if analysis.overdue_promises:
        analysis.recommended_action = "Срочно выполнить просроченное обещание клиенту"
    elif analysis.waiting_on == "us":
        analysis.recommended_action = "Ответить клиенту или выполнить обещанное действие"
    elif analysis.waiting_on == "client":
        analysis.recommended_action = "Проверить срок ожидания и при необходимости сделать follow-up"
    else:
        analysis.recommended_action = "Зафиксировать следующее действие"

    if last:
        actor = "клиент" if last.direction == "incoming" else "менеджер"
        analysis.summary = f"Последним написал {actor} через {last.channel}."
    else:
        analysis.summary = "Коммуникации по проекту не найдены."
    if analysis.overdue_promises:
        analysis.summary += f" Просроченных обещаний: {len(analysis.overdue_promises)}."
    return analysis


async def build_unified_timeline(
    db: AsyncSession,
    *,
    lead_id: int,
) -> UnifiedCommunicationResult:
    entries: list[CommunicationEntry] = []
    errors: list[str] = []
    chat_entries, chat_error = await _chat_entries(int(lead_id))
    entries.extend(chat_entries)
    if chat_error:
        errors.append(chat_error)
    note_entries, note_error = await _note_entries(int(lead_id))
    entries.extend(note_entries)
    if note_error:
        errors.append(note_error)
    try:
        entries.extend(await _draft_entries(db, int(lead_id)))
    except Exception as exc:
        errors.append(f"drafts: {type(exc).__name__}")
    try:
        entries.extend(await _project_event_entries(db, int(lead_id)))
    except Exception as exc:
        errors.append(f"project events: {type(exc).__name__}")
    entries = _deduplicate(entries)
    return UnifiedCommunicationResult(
        lead_id=int(lead_id),
        entries=entries,
        analysis=analyze_entries(entries),
        source_errors=errors,
    )


def as_context(result: UnifiedCommunicationResult, *, limit: int = 30) -> dict[str, Any]:
    return {
        "lead_id": result.lead_id,
        "analysis": {
            **asdict(result.analysis),
            "promises_by_us": [asdict(item) for item in result.analysis.promises_by_us],
            "overdue_promises": [asdict(item) for item in result.analysis.overdue_promises],
        },
        "messages": [asdict(item) for item in result.entries[-max(1, min(limit, 50)) :]],
        "source_errors": result.source_errors,
    }


def format_timeline(
    result: UnifiedCommunicationResult,
    *,
    lead_name: str,
    offset: int = 0,
    page_size: int = 10,
) -> str:
    entries = list(reversed(result.entries))
    page = entries[max(0, offset) : max(0, offset) + max(1, page_size)]
    analysis = result.analysis
    lines = [f"<b>💬 Переписка · {html.escape(lead_name)}</b>", ""]
    if analysis.last_contact_at:
        lines.extend(
            [
                f"Последний контакт: <b>{analysis.last_contact_at.astimezone().strftime('%d.%m.%Y %H:%M')}</b>",
                f"Канал: <b>{html.escape(str(analysis.last_channel or '—'))}</b>",
                f"Последним написал: <b>{'клиент' if analysis.last_direction == 'incoming' else 'менеджер'}</b>",
                f"Сейчас действует: <b>{'мы' if analysis.waiting_on == 'us' else 'клиент'}</b>",
            ]
        )
    if analysis.promises_by_us:
        last_promise = analysis.promises_by_us[-1]
        lines.append(f"Наше последнее обещание: {html.escape(last_promise.text[:400])}")
        if last_promise.due_at:
            lines.append(
                f"Срок обещания: <b>{last_promise.due_at.astimezone().strftime('%d.%m %H:%M')}</b>"
            )
    if analysis.overdue_promises:
        lines.append(f"⚠️ Просроченных обещаний: <b>{len(analysis.overdue_promises)}</b>")
    lines.extend(["", f"<b>Рекомендация:</b> {html.escape(str(analysis.recommended_action or '—'))}", ""])

    if not page:
        lines.append("Сообщений на этой странице нет.")
    for entry in page:
        icon = "📥" if entry.direction == "incoming" else "📤" if entry.direction == "outgoing" else "🗒"
        when = entry.occurred_at.astimezone().strftime("%d.%m %H:%M") if entry.occurred_at.timestamp() > 0 else "—"
        speaker = _DIRECTION_LABELS.get(entry.direction, entry.direction)
        text = entry.text[:650] + ("…" if len(entry.text) > 650 else "")
        lines.extend(
            [
                f"{icon} <b>{html.escape(when)} · {html.escape(entry.channel)} · {speaker}</b>",
                html.escape(text),
                "",
            ]
        )
    if result.source_errors:
        lines.append("<i>Недоступные источники: " + html.escape(", ".join(result.source_errors)) + "</i>")
    return "\n".join(lines)[:4000]


def timeline_markup(
    *,
    lead_id: int,
    offset: int,
    total: int,
    page_size: int = 10,
    lead_url: str | None = None,
) -> dict[str, Any]:
    rows: list[list[dict[str, str]]] = []
    nav: list[dict[str, str]] = []
    if offset > 0:
        nav.append(
            {
                "text": "⬅️ Новее",
                "callback_data": f"agent:comms:{lead_id}:{max(0, offset - page_size)}",
            }
        )
    if offset + page_size < total:
        nav.append(
            {
                "text": "Старше ➡️",
                "callback_data": f"agent:comms:{lead_id}:{offset + page_size}",
            }
        )
    if nav:
        rows.append(nav)
    rows.append(
        [
            {
                "text": "✍️ Подготовить ответ",
                "callback_data": f"followup:prepare:{lead_id}",
            }
        ]
    )
    if lead_url:
        rows.append([{"text": "🔗 Открыть Kommo", "url": str(lead_url)}])
    return {"inline_keyboard": rows}
