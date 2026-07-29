"""Telegram runtime integration for the unified communication timeline."""
from __future__ import annotations

from typing import Any

from app.agent import tools as agent_tools
from app.services import telegram_service, unified_communication_service

_INSTALLED = False


def _add_timeline_button(markup: dict[str, Any], lead_id: int) -> dict[str, Any]:
    result = {"inline_keyboard": [list(row) for row in (markup.get("inline_keyboard") or [])]}
    button = {
        "text": "💬 Вся переписка",
        "callback_data": f"agent:comms:{int(lead_id)}:0",
    }
    rows = result["inline_keyboard"]
    insert_at = max(0, len(rows) - 1) if rows and any("url" in item for item in rows[-1]) else len(rows)
    rows.insert(insert_at, [button])
    return result


async def _handle_timeline_callback(
    *,
    callback_data: str,
    chat_id: int,
    user_id: int,
    db: Any,
) -> bool:
    _ = user_id
    if not callback_data.startswith("agent:comms:"):
        return False
    parts = callback_data.split(":")
    if len(parts) != 4:
        raise ValueError("Некорректная команда истории переписки.")
    lead_id = int(parts[2])
    offset = max(0, int(parts[3]))
    lead = await agent_tools.kommo_service.get_lead_details(lead_id)
    result = await unified_communication_service.build_unified_timeline(
        db, lead_id=lead_id
    )
    await telegram_service.send_message(
        chat_id,
        unified_communication_service.format_timeline(
            result,
            lead_name=str(lead.get("name") or lead_id),
            offset=offset,
        ),
        reply_markup=unified_communication_service.timeline_markup(
            lead_id=lead_id,
            offset=offset,
            total=len(result.entries),
            lead_url=lead.get("url"),
        ),
    )
    return True


def install_communication_timeline_runtime() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True

    original_actions_markup = agent_tools.lead_card_actions_markup

    def lead_card_actions_with_timeline(lead: dict[str, Any]) -> dict[str, Any]:
        markup = original_actions_markup(lead)
        lead_id = int(lead.get("id") or lead.get("kommo_lead_id") or 0)
        return _add_timeline_button(markup, lead_id) if lead_id else markup

    agent_tools.lead_card_actions_markup = lead_card_actions_with_timeline

    from app.api import telegram as telegram_api

    original_manager_callback = telegram_api._handle_manager_callback

    async def manager_callback_with_timeline(**kwargs: Any) -> bool:
        callback_data = str(kwargs.get("callback_data") or "")
        if callback_data.startswith("agent:comms:"):
            return await _handle_timeline_callback(**kwargs)
        return await original_manager_callback(**kwargs)

    telegram_api._handle_manager_callback = manager_callback_with_timeline
