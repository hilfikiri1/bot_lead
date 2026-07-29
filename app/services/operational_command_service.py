"""Execution and Telegram formatting for B&BS operational commands."""
from __future__ import annotations

import html
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.services import digest_service, integration_event_service, operational_agent_service
from app.services import operational_notion_service as notion
from app.services.operational_command_router import RoutedCommand


@dataclass
class OperationalReply:
    text: str
    reply_markup: dict[str, Any] | None = None


def _digest_markup(items: list[dict[str, Any]]) -> dict[str, Any] | None:
    rows: list[list[dict[str, str]]] = []
    for item in items:
        lead = item.get("lead") or {}
        task = item.get("notion_task") or {}
        page_id = str(task.get("id") or "").replace("-", "")
        lead_id = int(lead.get("id") or 0)
        if not page_id or not lead_id:
            continue
        rows.append(
            [
                {
                    "text": f"✅ {str(lead.get('name') or lead_id)[:24]}",
                    "callback_data": f"opdone:{lead_id}:{page_id}",
                },
                {
                    "text": "↪️ +1 день",
                    "callback_data": f"opdelay:{lead_id}:{page_id}",
                },
            ]
        )
        if len(rows) >= 8:
            break
    return {"inline_keyboard": rows} if rows else None


def format_digest(result: dict[str, Any]) -> OperationalReply:
    lines = [
        f"<b>📋 Дайджест B&BS · {html.escape(str(result.get('date_label') or ''))}</b>",
        "",
        f"Открытых сделок: <b>{int(result.get('open_count') or 0)}</b>",
        f"Новых задач Notion: <b>{int(result.get('created_count') or 0)}</b>",
        f"Уже существовали: <b>{int(result.get('reused_count') or 0)}</b>",
    ]
    if result.get("failed_count"):
        lines.append(f"Ошибок синхронизации: <b>{int(result['failed_count'])}</b>")
    lines.append("")

    items = result.get("items") or []
    if not items:
        lines.append("Активных сделок для дайджеста не найдено.")
    for index, item in enumerate(items, 1):
        lead = item.get("lead") or {}
        name = html.escape(str(lead.get("name") or f"Kommo {lead.get('id')}"))
        url = html.escape(str(lead.get("url") or ""), quote=True)
        lines.extend(
            [
                f"<b>{index}. {name}</b>",
                f"Приоритет: {html.escape(str(item.get('priority') or '—'))}",
                f"Почему: {html.escape(str(item.get('reason') or '—'))}",
                f"Шаг: {html.escape(str(item.get('next_step') or '—'))}",
                (f'<a href="{url}">Открыть Kommo</a>' if url else ""),
            ]
        )
        task = item.get("notion_task") or {}
        if task.get("url"):
            notion_url = html.escape(str(task["url"]), quote=True)
            lines.append(f'<a href="{notion_url}">Задача в Notion</a>')
        if item.get("error"):
            lines.append(f"⚠️ {html.escape(str(item['error'])[:300])}")
        lines.append("")

    if result.get("truncated"):
        lines.append("⚠️ Kommo достиг лимита страниц; список может быть неполным.")
    return OperationalReply(
        text="\n".join(line for line in lines if line is not None),
        reply_markup=_digest_markup(items),
    )


def format_draft(result: dict[str, Any]) -> OperationalReply:
    draft = result.get("draft") or {}
    lead = result.get("lead") or {}
    record = result.get("record") or {}
    task = result.get("task") or {}
    missing = draft.get("missing_data") or []
    body = str(draft.get("body") or "")
    body_preview = body[:2800]
    if len(body) > len(body_preview):
        body_preview += "\n\n…полный текст сохранён в Notion"
    lines = [
        f"<b>🧠 {html.escape(str(draft.get('title') or 'Рабочий черновик'))}</b>",
        f"Сделка: <b>{html.escape(str(lead.get('name') or lead.get('id') or '—'))}</b>",
        "",
        html.escape(body_preview),
    ]
    if missing:
        lines.extend(
            [
                "",
                "<b>Нужно уточнить:</b>",
                *[f"• {html.escape(str(item))}" for item in missing],
            ]
        )
    lines.extend(
        [
            "",
            f"Следующий шаг: {html.escape(str(draft.get('next_action') or 'Проверить черновик'))}",
        ]
    )
    if task.get("url"):
        lines.append(
            f'<a href="{html.escape(str(task["url"]), quote=True)}">Задача в Notion</a>'
        )
    if record.get("url"):
        lines.append(
            f'<a href="{html.escape(str(record["url"]), quote=True)}">Черновик в профильной базе</a>'
        )
    lines.append("")
    lines.append("<i>Ничего не отправлено клиенту автоматически.</i>")
    return OperationalReply("\n".join(lines))


async def execute(
    db: AsyncSession,
    *,
    command: RoutedCommand,
    telegram_user_id: int,
) -> OperationalReply:
    if command.intent == "notion_test":
        result = await notion.validate_schema()
        return OperationalReply(notion.format_schema_report(result))

    if command.intent == "digest":
        result = await digest_service.build_digest(
            db,
            telegram_user_id=telegram_user_id,
        )
        return format_digest(result)

    if command.intent == "sync_leads":
        result = await digest_service.sync_all_open_leads(
            db,
            telegram_user_id=telegram_user_id,
        )
        return OperationalReply(
            "<b>🔄 Синхронизация Kommo → Notion</b>\n\n"
            f"Всего: <b>{result['total']}</b>\n"
            f"Создано: <b>{result['created']}</b>\n"
            f"Обновлено: <b>{result['updated']}</b>\n"
            f"Ошибок: <b>{result['failed']}</b>"
        )

    if command.intent == "errors":
        events = await integration_event_service.recent_errors(db, limit=10)
        if not events:
            return OperationalReply("✅ В журнале интеграций нет ошибок.")
        lines = ["<b>⚠️ Последние ошибки интеграций</b>", ""]
        for event in events:
            created = event.created_at.strftime("%d.%m %H:%M") if event.created_at else "—"
            lines.append(
                f"• <b>{html.escape(event.service)}</b> / "
                f"{html.escape(event.operation)} · {created}"
            )
            if event.error_message:
                lines.append(f"  <code>{html.escape(event.error_message[:300])}</code>")
        return OperationalReply("\n".join(lines))

    if command.intent == "draft":
        result = await operational_agent_service.generate_and_store_draft(
            db,
            kind=str(command.args["kind"]),
            lead_id=int(command.args["lead_id"]),
            telegram_user_id=telegram_user_id,
        )
        return format_draft(result)

    raise ValueError(f"Unsupported operational intent: {command.intent}")
