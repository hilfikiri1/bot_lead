"""Telegram entry points for the B&BS diagnostic support bundle."""
from __future__ import annotations

import html
import logging
import re
from typing import Any

from app.agent import service as agent_service
from app.agent.contracts import AgentReply
from app.services import identity_service, telegram_service
from app.services.system_diagnostics import (
    format_diagnostic_summary,
    render_diagnostic_json,
    render_diagnostic_markdown,
    run_system_diagnostics,
)

logger = logging.getLogger(__name__)
_INSTALLED = False
_COMMAND_RE = re.compile(
    r"^\s*(?:/diag(?:nostic)?|диагностика(?:\s+системы)?)\b\s*(.*)$",
    flags=re.I,
)


def parse_diagnostic_command(text: str) -> tuple[bool, str | None, bool]:
    """Return (matched, project_query, help_requested)."""
    match = _COMMAND_RE.match(str(text or ""))
    if not match:
        return False, None, False
    tail = " ".join((match.group(1) or "").split()).strip()
    if tail.casefold() in {"help", "помощь", "?"}:
        return True, None, True
    if tail.casefold() in {"full", "полная", "система", "system"}:
        tail = ""
    return True, tail or None, False


def _help_reply() -> AgentReply:
    return AgentReply(
        "<b>🧪 Диагностика B&BS</b>\n\n"
        "<code>/diag</code> — полный read-only аудит системы.\n"
        "<code>/diag 107</code> — аудит системы плюс проверка конкретного проекта.\n\n"
        "Бот проверит PostgreSQL, Redis, Telegram webhook, Kommo, Google Sheets, "
        "Notion, Google Drive и WhatsApp Cloud API. Затем пришлёт Markdown и JSON.\n\n"
        "Никаких внешних записей диагностика не выполняет и секреты не выводит.",
        intent="system_diagnostics_help",
    )


def _allowed() -> bool:
    actor = identity_service.current_user()
    if actor is None:
        return True
    return str(getattr(actor, "role", "")).casefold() in {"owner", "admin"}


async def _run_and_send(
    db: Any,
    *,
    chat_id: int,
    telegram_user_id: int,
    project_query: str | None,
) -> AgentReply:
    if not _allowed():
        return AgentReply(
            "🔒 Полная диагностика и экспорт журнала доступны только Owner/Admin.",
            intent="system_diagnostics_denied",
        )

    progress = "🧪 Запускаю read-only аудит системы"
    if project_query:
        progress += f" и проекта {html.escape(project_query)}"
    progress += ". Ничего не изменяю…"
    await telegram_service.send_message(chat_id, progress)

    report = await run_system_diagnostics(
        db,
        telegram_user_id=telegram_user_id,
        project_query=project_query,
    )
    trace_id = str(report.get("trace_id") or "diagnostic")
    markdown = render_diagnostic_markdown(report)
    json_data = render_diagnostic_json(report)

    await telegram_service.send_document(
        chat_id,
        filename=f"BBS_diagnostic_{trace_id}.md",
        content=markdown,
        caption=f"📋 Читаемый отчёт · <code>{html.escape(trace_id)}</code>",
        mime_type="text/markdown",
    )
    await telegram_service.send_document(
        chat_id,
        filename=f"BBS_diagnostic_{trace_id}.json",
        content=json_data,
        caption=f"🧰 Полный пакет для дебага · <code>{html.escape(trace_id)}</code>",
        mime_type="application/json",
    )

    summary = format_diagnostic_summary(report)
    return AgentReply(
        summary,
        reply_markup={
            "inline_keyboard": [
                [
                    {
                        "text": "🔄 Повторить полный аудит",
                        "callback_data": "agent:diag:run",
                    }
                ],
                [
                    {
                        "text": "🏠 Главное меню",
                        "callback_data": "menu:home",
                    }
                ],
            ]
        },
        intent="system_diagnostics",
        metadata={
            "trace_id": trace_id,
            "overall_status": report.get("overall_status"),
            "project_query": project_query,
        },
    )


def install_diagnostic_runtime() -> None:
    """Install command and callback wrappers exactly once."""
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True

    original_handle_message = agent_service.handle_message
    original_handle_callback = agent_service.handle_callback

    async def handle_message_with_diagnostics(
        db: Any,
        *,
        chat_id: int,
        telegram_user_id: int,
        text: str,
        source: str = "text",
        allow_conversation_passthrough: bool = False,
        active_kommo_lead_id: int | None = None,
    ) -> AgentReply:
        matched, project_query, help_requested = parse_diagnostic_command(text)
        if matched:
            if help_requested:
                return _help_reply()
            try:
                return await _run_and_send(
                    db,
                    chat_id=chat_id,
                    telegram_user_id=telegram_user_id,
                    project_query=project_query,
                )
            except Exception as exc:
                logger.exception("System diagnostic command failed")
                return AgentReply(
                    "❌ Не удалось собрать диагностический пакет. "
                    f"Ошибка: <code>{html.escape(exc.__class__.__name__)}</code>. "
                    "Открой Railway logs по времени этого сообщения.",
                    intent="system_diagnostics_failed",
                )
        return await original_handle_message(
            db,
            chat_id=chat_id,
            telegram_user_id=telegram_user_id,
            text=text,
            source=source,
            allow_conversation_passthrough=allow_conversation_passthrough,
            active_kommo_lead_id=active_kommo_lead_id,
        )

    async def handle_callback_with_diagnostics(
        db: Any,
        *,
        callback_data: str,
        telegram_user_id: int,
        chat_id: int | None = None,
    ) -> AgentReply | None:
        if callback_data == "agent:diag:run":
            try:
                return await _run_and_send(
                    db,
                    chat_id=int(chat_id or 0),
                    telegram_user_id=telegram_user_id,
                    project_query=None,
                )
            except Exception as exc:
                logger.exception("System diagnostic callback failed")
                return AgentReply(
                    "❌ Не удалось повторить диагностику: "
                    f"<code>{html.escape(exc.__class__.__name__)}</code>.",
                    intent="system_diagnostics_failed",
                )
        return await original_handle_callback(
            db,
            callback_data=callback_data,
            telegram_user_id=telegram_user_id,
            chat_id=chat_id,
        )

    agent_service.handle_message = handle_message_with_diagnostics
    agent_service.handle_callback = handle_callback_with_diagnostics
    logger.info("System diagnostic runtime installed")
