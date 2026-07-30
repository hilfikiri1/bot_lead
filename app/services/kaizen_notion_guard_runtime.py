"""Read-only Notion capability guard for weekly Kaizen improvement cards."""
from __future__ import annotations

from typing import Any

from app.agent import notion_gateway, service as agent_service
from app.agent.contracts import AgentReply
from app.config import get_settings

settings = get_settings()
_INSTALLED = False


def _named_property(properties: dict[str, Any], names: tuple[str, ...]) -> tuple[str | None, dict[str, Any]]:
    for name in names:
        if name in properties:
            return name, dict(properties[name] or {})
    return None, {}


def _option_names(prop: dict[str, Any]) -> set[str]:
    prop_type = str(prop.get("type") or "")
    payload = prop.get(prop_type) or {}
    return {
        str(item.get("name") or "").casefold()
        for item in payload.get("options") or []
        if item.get("name")
    }


async def notion_improvement_capability() -> tuple[bool, str | None]:
    if not settings.notion_api_token.strip() or not settings.notion_tasks_data_source_id.strip():
        return False, "Notion Tasks не настроен"
    try:
        source = await notion_gateway.retrieve_data_source(
            settings.notion_tasks_data_source_id
        )
    except Exception as exc:
        return False, f"Notion Tasks недоступен: {exc.__class__.__name__}"
    properties = dict(source.get("properties") or {})
    title = next(
        (name for name, prop in properties.items() if str((prop or {}).get("type")) == "title"),
        None,
    )
    if not title:
        return False, "в Tasks нет title-свойства"

    checks = (
        (("Тип", "Type"), "Improvement"),
        (("Статус", "Status"), "Todo"),
        (("Источник", "Source"), "Kaizen"),
    )
    for names, required_value in checks:
        name, prop = _named_property(properties, names)
        if not name:
            return False, f"в Tasks нет свойства {names[0]}"
        prop_type = str(prop.get("type") or "")
        if prop_type not in {"select", "status", "rich_text"}:
            return False, f"свойство {name} имеет неподдерживаемый тип {prop_type or 'unknown'}"
        options = _option_names(prop)
        if options and required_value.casefold() not in options:
            return False, f"в свойстве {name} нет значения {required_value}"
    return True, None


def _without_create_button(reply: AgentReply, reason: str | None) -> AgentReply:
    markup = dict(reply.reply_markup or {})
    rows = []
    for row in markup.get("inline_keyboard") or []:
        filtered = [
            button
            for button in row
            if not str(button.get("callback_data") or "").startswith(
                ("agent:kaizen:weekcreate:", "agent:kaizen:weekcancel:")
            )
        ]
        if filtered:
            rows.append(filtered)
    reply.reply_markup = {"inline_keyboard": rows} if rows else None
    if "Создание карточек скрыто" not in reply.text:
        reply.text = (
            reply.text.rstrip()
            + "\n\nℹ️ Создание карточек скрыто: "
            + (reason or "проверь базу Tasks командой /notion_test")
            + "."
        )[:4000]
    return reply


async def guard_weekly_reply(reply: AgentReply | None) -> AgentReply | None:
    if reply is None or reply.intent != "weekly_review":
        return reply
    has_create = any(
        str(button.get("callback_data") or "").startswith("agent:kaizen:weekcreate:")
        for row in (reply.reply_markup or {}).get("inline_keyboard") or []
        for button in row
    )
    if not has_create:
        return reply
    ready, reason = await notion_improvement_capability()
    return reply if ready else _without_create_button(reply, reason)


def install_kaizen_notion_guard_runtime() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True

    original_message = agent_service.handle_message
    original_callback = agent_service.handle_callback

    async def handle_message_with_notion_guard(*args, **kwargs):
        return await guard_weekly_reply(await original_message(*args, **kwargs))

    async def handle_callback_with_notion_guard(
        db: Any,
        *,
        callback_data: str,
        telegram_user_id: int,
        chat_id: int | None = None,
    ):
        if callback_data.startswith("agent:kaizen:weekcreate:"):
            ready, reason = await notion_improvement_capability()
            if not ready:
                return AgentReply(
                    "⚠️ Карточки не созданы: "
                    + (reason or "база Tasks недоступна")
                    + ". Проверь <code>/notion_test</code>.",
                    intent="weekly_improvements_unavailable",
                )
        reply = await original_callback(
            db,
            callback_data=callback_data,
            telegram_user_id=telegram_user_id,
            chat_id=chat_id,
        )
        return await guard_weekly_reply(reply)

    agent_service.handle_message = handle_message_with_notion_guard
    agent_service.handle_callback = handle_callback_with_notion_guard
