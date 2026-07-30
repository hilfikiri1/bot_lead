"""Local-first goals and product QA services.

PostgreSQL is the system of record for intake. Notion is a projection: failures never
remove the local record and can be retried later.
"""
from __future__ import annotations

import hashlib
import html
import os
import re
from calendar import monthrange
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.agent import notion_gateway
from app.agent.security import sanitize_text
from app.models.goal_qa import BusinessGoal, QAAttachment, QAIssue

OPEN_ISSUE_STATUSES = {
    "New", "Need details", "Confirmed", "In progress", "Ready for test", "Testing", "Blocked"
}
ISSUE_TYPES = {
    "bug": "Bug",
    "improvement": "Improvement",
    "idea": "Improvement",
    "ux": "UX",
    "concern": "Concern",
    "question": "Question",
    "data": "Data issue",
    "integration": "Integration issue",
}
RETEST_RESULTS = {
    "исправлено": "Исправлено",
    "частично": "Частично исправлено",
    "не исправлено": "Не исправлено",
    "новая проблема": "Появилась новая проблема",
}

_SECRET_PATTERNS = (
    re.compile(r"(?i)(authorization\s*:\s*bearer\s+)[^\s]+"),
    re.compile(r"(?i)((?:api[_-]?key|access[_-]?token|refresh[_-]?token|app[_-]?secret|client[_-]?secret|database_url|redis_url)\s*[:=]\s*)[^\s,;]+"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
    re.compile(r"\beyJ[a-zA-Z0-9_-]{10,}\.[a-zA-Z0-9_-]{10,}\.[a-zA-Z0-9_-]{10,}\b"),
)


def redact_sensitive(value: str | None) -> str:
    text = sanitize_text(str(value or ""), limit=20_000) or ""
    for pattern in _SECRET_PATTERNS:
        if pattern.groups:
            text = pattern.sub(r"\1[REDACTED]", text)
        else:
            text = pattern.sub("[REDACTED]", text)
    return text


def normalize(value: str | None) -> str:
    return " ".join(str(value or "").strip().casefold().replace("ё", "е").split())


def classify_issue(text: str, requested: str | None = None) -> str:
    if requested and requested in ISSUE_TYPES.values():
        return requested
    normalized = normalize(text)
    if any(token in normalized for token in ("опасение", "риск", "боюсь", "может смешаться")):
        return "Concern"
    if any(token in normalized for token in ("неудоб", "интерфейс", "кнопк", "ux")):
        return "UX"
    if any(token in normalized for token in ("синхрон", "неверные данные", "дубли", "колонк")):
        return "Data issue"
    if any(token in normalized for token in ("notion", "drive", "kommo", "whatsapp", "railway", "webhook", "интеграц")):
        if any(token in normalized for token in ("ошиб", "не работает", "не груз", "failed", "fail")):
            return "Integration issue"
    if any(token in normalized for token in ("идея", "предложение", "улучш", "добавить", "было бы удобно")):
        return "Improvement"
    if normalized.endswith("?") or normalized.startswith(("почему", "как ", "что ")):
        return "Question"
    return "Bug"


def infer_priority(text: str) -> str:
    normalized = normalize(text)
    if any(token in normalized for token in ("критич", "данные потер", "массово", "секрет", "чужие клиенты")):
        return "Critical"
    if any(token in normalized for token in ("не работает", "невозможно", "блокирует", "ошибка", "не загружается")):
        return "High"
    if any(token in normalized for token in ("мелочь", "космет", "неудобно", "потом")):
        return "Low"
    return "Medium"


def infer_module(text: str) -> str | None:
    normalized = normalize(text)
    mapping = (
        (("whatsapp", "ватсап", "вацап"), "WhatsApp"),
        (("notion", "ноушн"), "Notion"),
        (("drive", "драйв", "папк", "файл"), "Google Drive"),
        (("kommo", "коммо", "сделк"), "Kommo"),
        (("sheets", "таблиц", "колонк"), "Google Sheets"),
        (("telegram", "телеграм", "бот", "кнопк"), "Telegram"),
        (("railway", "deploy", "деплой"), "Railway"),
        (("голос", "аудио", "расшифров"), "Voice pipeline"),
        (("календар", "calendar"), "Calendar"),
    )
    for tokens, module in mapping:
        if any(token in normalized for token in tokens):
            return module
    return None


def issue_title(text: str, issue_type: str) -> str:
    clean = redact_sensitive(text).strip()
    clean = re.sub(
        r"(?i)^\s*(?:я\s+)?(?:наш[её]л\s+)?(?:баг|ошибк[ау]?|иде[яю]|опасение|предложение|feedback)\s*[:—-]?\s*",
        "",
        clean,
    ).strip()
    first = re.split(r"[\n.!?]", clean, maxsplit=1)[0].strip()
    return (first or f"{issue_type} без названия")[:180]


def issue_dedupe_key(*, issue_type: str, module: str | None, title: str, description: str) -> str:
    material = "|".join(
        (issue_type.casefold(), (module or "").casefold(), normalize(title), normalize(description)[:1000])
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


async def find_similar_issue(
    db: AsyncSession,
    *,
    telegram_user_id: int,
    issue_type: str,
    module: str | None,
    title: str,
    description: str,
) -> QAIssue | None:
    key = issue_dedupe_key(
        issue_type=issue_type, module=module, title=title, description=description
    )
    exact = (
        await db.execute(
            select(QAIssue)
            .where(
                QAIssue.telegram_user_id == int(telegram_user_id),
                QAIssue.dedupe_key == key,
                QAIssue.status.in_(OPEN_ISSUE_STATUSES),
            )
            .order_by(QAIssue.id.desc())
        )
    ).scalars().first()
    if exact:
        return exact
    title_words = [word for word in normalize(title).split() if len(word) >= 5][:3]
    if not title_words:
        return None
    candidates = (
        await db.execute(
            select(QAIssue)
            .where(
                QAIssue.telegram_user_id == int(telegram_user_id),
                QAIssue.status.in_(OPEN_ISSUE_STATUSES),
                QAIssue.issue_type == issue_type,
                or_(QAIssue.module == module, QAIssue.module.is_(None)),
            )
            .order_by(QAIssue.id.desc())
            .limit(30)
        )
    ).scalars().all()
    for candidate in candidates:
        haystack = normalize(f"{candidate.title} {candidate.description or ''}")
        if sum(word in haystack for word in title_words) >= min(2, len(title_words)):
            return candidate
    return None


async def create_local_issue(
    db: AsyncSession,
    *,
    telegram_user_id: int,
    text: str,
    issue_type: str | None = None,
    active_project_number: str | None = None,
    kommo_lead_id: int | None = None,
    trace_id: str | None = None,
    source: str = "text",
    force_new: bool = False,
    metadata: dict[str, Any] | None = None,
) -> tuple[QAIssue, QAIssue | None]:
    safe_text = redact_sensitive(text)
    resolved_type = classify_issue(safe_text, issue_type)
    module = infer_module(safe_text)
    title = issue_title(safe_text, resolved_type)
    duplicate = await find_similar_issue(
        db,
        telegram_user_id=telegram_user_id,
        issue_type=resolved_type,
        module=module,
        title=title,
        description=safe_text,
    )
    if duplicate is not None and not force_new:
        return duplicate, duplicate
    key = issue_dedupe_key(
        issue_type=resolved_type, module=module, title=title, description=safe_text
    )
    if force_new:
        key = f"{key}:{datetime.now(timezone.utc).timestamp()}"
    issue = QAIssue(
        telegram_user_id=int(telegram_user_id),
        issue_type=resolved_type,
        status="New" if len(safe_text) >= 12 else "Need details",
        priority=infer_priority(safe_text),
        module=module,
        environment=os.getenv("APP_ENV", "production"),
        title=title,
        description=safe_text,
        actual_result=safe_text,
        trace_id=redact_sensitive(trace_id)[:128] or None,
        active_project_number=(str(active_project_number)[:32] if active_project_number else None),
        kommo_lead_id=int(kommo_lead_id) if kommo_lead_id else None,
        app_version=os.getenv("APP_VERSION", "5.0.0")[:64],
        railway_deployment=redact_sensitive(os.getenv("RAILWAY_DEPLOYMENT_ID", "")) or None,
        dedupe_key=key[:128],
        metadata_json={"source": source, **(metadata or {})},
    )
    db.add(issue)
    await db.flush()
    prefix = {
        "Bug": "BUG", "Improvement": "IMP", "UX": "UX", "Concern": "RSK",
        "Question": "Q", "Data issue": "DATA", "Integration issue": "INT",
    }.get(resolved_type, "QA")
    issue.issue_code = f"{prefix}-{int(issue.id):04d}"
    await db.commit()
    await db.refresh(issue)
    return issue, duplicate


async def append_issue_comment(db: AsyncSession, issue: QAIssue, text: str) -> QAIssue:
    addition = redact_sensitive(text).strip()
    previous = str(issue.user_comment or "").strip()
    issue.user_comment = (previous + ("\n\n" if previous else "") + addition)[:20_000]
    issue.updated_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(issue)
    return issue


async def add_attachment_record(
    db: AsyncSession,
    *,
    issue: QAIssue,
    original_name: str,
    mime_type: str | None,
    size_bytes: int | None,
    telegram_file_id: str | None,
    storage_path: str | None = None,
    checksum: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> QAAttachment:
    item = QAAttachment(
        issue_id=int(issue.id),
        original_name=redact_sensitive(original_name)[:500] or "attachment",
        mime_type=str(mime_type or "")[:255] or None,
        size_bytes=int(size_bytes) if size_bytes is not None else None,
        telegram_file_id=redact_sensitive(telegram_file_id) or None,
        storage_path=redact_sensitive(storage_path) or None,
        checksum=str(checksum or "")[:128] or None,
        upload_status="pending",
        metadata_json=metadata or {},
    )
    db.add(item)
    await db.commit()
    await db.refresh(item)
    return item


async def mark_attachment_uploaded(
    db: AsyncSession,
    *,
    attachment: QAAttachment,
    drive_file_id: str,
    drive_url: str,
) -> QAAttachment:
    attachment.drive_file_id = str(drive_file_id)[:255]
    attachment.drive_url = str(drive_url)
    attachment.upload_status = "uploaded"
    attachment.error_message = None
    attachment.uploaded_at = datetime.now(timezone.utc)
    await db.commit()
    return attachment


async def mark_attachment_failed(db: AsyncSession, *, attachment: QAAttachment, error: str) -> QAAttachment:
    attachment.upload_status = "failed"
    attachment.error_message = redact_sensitive(error)[:2000]
    await db.commit()
    return attachment


async def get_issue(db: AsyncSession, *, telegram_user_id: int, issue_ref: str | int) -> QAIssue | None:
    text = str(issue_ref).strip().upper()
    condition = QAIssue.issue_code == text
    if text.isdigit():
        condition = QAIssue.id == int(text)
    return (
        await db.execute(
            select(QAIssue)
            .options(selectinload(QAIssue.attachments))
            .where(QAIssue.telegram_user_id == int(telegram_user_id), condition)
        )
    ).scalar_one_or_none()


async def list_issues(db: AsyncSession, *, telegram_user_id: int, limit: int = 20) -> list[QAIssue]:
    return list(
        (
            await db.execute(
                select(QAIssue)
                .options(selectinload(QAIssue.attachments))
                .where(QAIssue.telegram_user_id == int(telegram_user_id))
                .order_by(QAIssue.created_at.desc())
                .limit(max(1, min(limit, 50)))
            )
        ).scalars().all()
    )


def format_issue(issue: QAIssue) -> str:
    lines = [
        f"<b>{html.escape(issue.issue_code or str(issue.id))} · {html.escape(issue.title)}</b>",
        f"Тип: {html.escape(issue.issue_type)} · Статус: {html.escape(issue.status)} · Приоритет: {html.escape(issue.priority)}",
    ]
    if issue.module:
        lines.append(f"Модуль: {html.escape(issue.module)}")
    if issue.active_project_number:
        lines.append(f"Проект: №{html.escape(issue.active_project_number)}")
    if issue.trace_id:
        lines.append(f"Trace ID: <code>{html.escape(issue.trace_id)}</code>")
    if issue.description:
        lines.extend(["", html.escape(issue.description[:2400])])
    attachments = list(issue.attachments or [])
    if attachments:
        uploaded = sum(item.upload_status == "uploaded" for item in attachments)
        failed = sum(item.upload_status == "failed" for item in attachments)
        pending = len(attachments) - uploaded - failed
        lines.extend(["", f"Вложения: {len(attachments)} · загружено {uploaded} · ожидает {pending} · ошибок {failed}"])
    if issue.notion_url:
        lines.extend(["", f'<a href="{html.escape(issue.notion_url, quote=True)}">Открыть в Notion</a>'])
    return "\n".join(lines)[:3900]


def format_issue_list(items: list[QAIssue]) -> str:
    if not items:
        return "✅ В журнале QA пока нет записей."
    groups: dict[str, list[QAIssue]] = {}
    for item in items:
        groups.setdefault(item.status, []).append(item)
    lines = ["<b>🧪 Баги и улучшения</b>"]
    for status in ("New", "Need details", "Confirmed", "In progress", "Ready for test", "Testing", "Blocked", "Verified", "Closed"):
        current = groups.get(status) or []
        if not current:
            continue
        lines.extend(["", f"<b>{html.escape(status)}</b>"])
        for item in current[:10]:
            lines.append(f"• {html.escape(item.issue_code or str(item.id))} · {html.escape(item.title[:100])} [{html.escape(item.priority)}]")
    return "\n".join(lines)[:3900]


def _prop_payload(meta: dict[str, Any], value: Any) -> dict[str, Any] | None:
    kind = str(meta.get("type") or "")
    if value is None or value == "":
        return None
    if kind == "title":
        return notion_gateway._title(str(value))  # noqa: SLF001
    if kind in {"rich_text", "text"}:
        return notion_gateway._rich_text(str(value))  # noqa: SLF001
    if kind == "select":
        return {"select": {"name": str(value)}}
    if kind == "status":
        return {"status": {"name": str(value)}}
    if kind == "number":
        try:
            return {"number": float(value)}
        except (TypeError, ValueError):
            return None
    if kind == "date":
        return {"date": {"start": str(value)}}
    if kind == "url":
        return {"url": str(value)}
    if kind == "files" and isinstance(value, list):
        files = []
        for item in value[:20]:
            if not isinstance(item, dict) or not item.get("url"):
                continue
            files.append({
                "name": str(item.get("name") or "attachment")[:255],
                "type": "external",
                "external": {"url": str(item["url"])},
            })
        return {"files": files}
    return None


def _first_property(properties: dict[str, Any], names: tuple[str, ...], kinds: set[str] | None = None) -> str | None:
    for name in names:
        meta = properties.get(name) or {}
        if name in properties and (not kinds or str(meta.get("type")) in kinds):
            return name
    if kinds:
        return next((name for name, meta in properties.items() if str(meta.get("type")) in kinds), None)
    return None


async def sync_issue_to_notion(db: AsyncSession, issue: QAIssue) -> QAIssue:
    source_id = os.getenv("NOTION_QA_DATA_SOURCE_ID", "").strip()
    if not source_id:
        raise notion_gateway.OperationalNotionError("NOTION_QA_DATA_SOURCE_ID не настроен.")
    source = await notion_gateway.retrieve_data_source(source_id)
    meta = source.get("properties") or {}
    title_name = _first_property(meta, ("Название", "Name", "Задача"), {"title"})
    if not title_name:
        raise notion_gateway.OperationalNotionError("В QA-базе нет title-свойства.")
    values: tuple[tuple[tuple[str, ...], Any], ...] = (
        ((title_name,), f"{issue.issue_code} — {issue.title}"),
        (("Тип", "Type"), issue.issue_type),
        (("Статус", "Status"), issue.status),
        (("Приоритет", "Priority"), issue.priority),
        (("Модуль", "Module"), issue.module),
        (("Среда", "Environment"), issue.environment),
        (("Описание", "Description"), issue.description),
        (("Ожидаемый результат",), issue.expected_result),
        (("Фактический результат",), issue.actual_result),
        (("Шаги воспроизведения",), issue.reproduction_steps),
        (("Trace ID",), issue.trace_id),
        (("Telegram user",), str(issue.telegram_user_id)),
        (("Связанный проект",), issue.active_project_number),
        (("Kommo ID",), issue.kommo_lead_id),
        (("Версия приложения",), issue.app_version),
        (("Railway deployment",), issue.railway_deployment),
        (("GitHub PR",), issue.github_pr),
        (("Корневая причина",), issue.root_cause),
        (("Решение",), issue.resolution),
        (("Комментарий пользователя",), issue.user_comment),
        (("Результат проверки",), issue.retest_result),
    )
    properties: dict[str, Any] = {}
    for names, value in values:
        name = next((candidate for candidate in names if candidate in meta), None)
        if not name:
            continue
        payload = _prop_payload(meta[name], value)
        if payload is not None:
            properties[name] = payload
    files_name = next((name for name in ("Вложения", "Скриншоты", "Видео") if name in meta and str(meta[name].get("type")) == "files"), None)
    if files_name:
        links = [
            {"name": item.original_name, "url": item.drive_url}
            for item in (issue.attachments or [])
            if item.upload_status == "uploaded" and item.drive_url
        ]
        payload = _prop_payload(meta[files_name], links)
        if payload is not None:
            properties[files_name] = payload
    body = [
        {"object": "block", "type": "heading_2", "heading_2": {"rich_text": [{"type": "text", "text": {"content": "Описание"}}]}},
        {"object": "block", "type": "paragraph", "paragraph": {"rich_text": [{"type": "text", "text": {"content": str(issue.description or "—")[:1900]}}]}},
        {"object": "block", "type": "heading_2", "heading_2": {"rich_text": [{"type": "text", "text": {"content": "Диагностика"}}]}},
        {"object": "block", "type": "paragraph", "paragraph": {"rich_text": [{"type": "text", "text": {"content": f"Trace ID: {issue.trace_id or '—'}; Kommo ID: {issue.kommo_lead_id or '—'}; project: {issue.active_project_number or '—'}"[:1900]}}]}},
    ]
    response = await notion_gateway._request(  # noqa: SLF001
        "POST", "/pages", json={
            "parent": {"type": "data_source_id", "data_source_id": notion_gateway._data_source_id(source_id)},  # noqa: SLF001
            "properties": properties,
            "children": body,
        }
    )
    issue.notion_page_id = str(response.get("id") or "") or None
    issue.notion_url = response.get("url") or (notion_gateway.notion_page_url(issue.notion_page_id) if issue.notion_page_id else None)
    await db.commit()
    return issue


def month_bounds(day: date | None = None) -> tuple[date, date]:
    current = day or date.today()
    return current.replace(day=1), current.replace(day=monthrange(current.year, current.month)[1])


async def list_month_goals(db: AsyncSession, *, telegram_user_id: int, day: date | None = None) -> list[BusinessGoal]:
    start, end = month_bounds(day)
    return list(
        (
            await db.execute(
                select(BusinessGoal)
                .where(
                    BusinessGoal.telegram_user_id == int(telegram_user_id),
                    BusinessGoal.period_start <= end,
                    BusinessGoal.period_end >= start,
                )
                .order_by(BusinessGoal.status.asc(), BusinessGoal.period_end.asc())
            )
        ).scalars().all()
    )


def calculated_progress(goal: BusinessGoal) -> Decimal | None:
    if goal.progress_percent is not None:
        return Decimal(goal.progress_percent)
    if goal.current_value is None or goal.target_value in (None, 0):
        return None
    try:
        return max(Decimal(0), min(Decimal(100), Decimal(goal.current_value) / Decimal(goal.target_value) * 100))
    except (InvalidOperation, ZeroDivisionError):
        return None


def format_month_goals(goals: list[BusinessGoal], *, progress_view: bool = False) -> str:
    start, end = month_bounds()
    heading = "Прогресс целей месяца" if progress_view else "Цели месяца"
    lines = [f"<b>🎯 {heading}</b>", f"{start.strftime('%d.%m')}–{end.strftime('%d.%m.%Y')}"]
    if not goals:
        lines.extend(["", "Цели пока не заполнены. Добавь их в базе B&BS — Цели или командой «Добавь цель месяца: …»."])
        return "\n".join(lines)
    for index, goal in enumerate(goals, 1):
        progress = calculated_progress(goal)
        progress_text = f"{progress.quantize(Decimal('1'))}%" if progress is not None else "нет измеримых данных"
        lines.extend(["", f"<b>{index}. {html.escape(goal.title)}</b>", f"Статус: {html.escape(goal.status)} · Прогресс: {html.escape(progress_text)}"])
        if goal.next_step:
            lines.append(f"Следующий шаг: {html.escape(goal.next_step[:300])}")
        if goal.obstacles:
            lines.append(f"Риск: {html.escape(goal.obstacles[:300])}")
    return "\n".join(lines)[:3900]


async def create_goal(
    db: AsyncSession,
    *,
    telegram_user_id: int,
    title: str,
    goal_type: str = "month",
    period_start: date | None = None,
    period_end: date | None = None,
    target_value: Decimal | None = None,
    metric_name: str | None = None,
) -> BusinessGoal:
    if period_start is None or period_end is None:
        period_start, period_end = month_bounds()
    safe_title = redact_sensitive(title)[:500]
    external_id = "goal:" + hashlib.sha256(
        f"{telegram_user_id}:{goal_type}:{period_start}:{period_end}:{normalize(safe_title)}".encode("utf-8")
    ).hexdigest()[:32]
    existing = (
        await db.execute(
            select(BusinessGoal).where(
                BusinessGoal.telegram_user_id == int(telegram_user_id),
                BusinessGoal.external_id == external_id,
            )
        )
    ).scalar_one_or_none()
    if existing:
        return existing
    goal = BusinessGoal(
        telegram_user_id=int(telegram_user_id),
        external_id=external_id,
        title=safe_title,
        goal_type=goal_type,
        status="planned",
        period_start=period_start,
        period_end=period_end,
        target_value=target_value,
        metric_name=metric_name,
    )
    db.add(goal)
    await db.commit()
    await db.refresh(goal)
    return goal
