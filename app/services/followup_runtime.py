"""Telegram runtime wiring for the automatic follow-up engine."""
from __future__ import annotations

import html
import logging
from typing import Any

from app.services import (
    client_message_service,
    followup_service,
    identity_service,
    telegram_service,
    telegram_state_service,
)

logger = logging.getLogger(__name__)
_INSTALLED = False


def _assert_writer() -> None:
    if not identity_service.can_write(identity_service.current_user()):
        raise PermissionError("Роль Viewer позволяет только просматривать данные.")


async def _handle_followup_callback(
    *,
    callback_data: str,
    chat_id: int,
    user_id: int,
    db: Any,
) -> bool:
    if not callback_data.startswith("followup:"):
        return False
    _assert_writer()
    parts = callback_data.split(":")
    command = parts[1] if len(parts) > 1 else ""

    if command == "set" and len(parts) == 4:
        draft_id = int(parts[2])
        preset = parts[3]
        result = await followup_service.schedule_from_draft(
            db,
            draft_id=draft_id,
            telegram_user_id=user_id,
            due_at=followup_service.preset_due_at(preset),
            preset=preset,
        )
        await telegram_service.send_message(
            chat_id,
            (
                "✅ <b>Follow-up запланирован</b>\n\n"
                f"Сделка: {html.escape(str(result.get('lead_name') or result['lead_id']))}\n"
                f"Проверить ответ: <b>{followup_service.format_due_at(result['due_at'])}</b>\n"
                + (
                    f"Задача Kommo: <code>{result['kommo_task_id']}</code>"
                    if result.get("kommo_task_id")
                    else "⚠️ Локальное напоминание создано, но задача Kommo не подтвердилась."
                )
            ),
            reply_markup={
                "inline_keyboard": [
                    [{"text": "🔗 Открыть Kommo", "url": str(result.get("lead_url"))}]
                ]
            }
            if result.get("lead_url")
            else None,
        )
        return True

    if command == "custom" and len(parts) == 3:
        draft_id = int(parts[2])
        await telegram_state_service.set_state(
            user_id,
            {
                "mode": "followup_custom_due",
                "chat_id": chat_id,
                "client_message_draft_id": draft_id,
            },
            ttl_seconds=60 * 30,
        )
        await telegram_service.send_message(
            chat_id,
            (
                "📅 <b>Укажите дату проверки ответа</b>\n\n"
                "Примеры:\n"
                "• <code>завтра 10:00</code>\n"
                "• <code>31.07.2026 15:00</code>\n"
                "• <code>2026-07-31 15:00</code>"
            ),
            reply_markup={
                "inline_keyboard": [
                    [{"text": "❌ Отмена", "callback_data": f"followup:none:{draft_id}"}]
                ]
            },
        )
        return True

    if command == "none" and len(parts) == 3:
        draft_id = int(parts[2])
        await followup_service.skip_for_draft(
            db, draft_id=draft_id, telegram_user_id=user_id
        )
        await telegram_state_service.clear_state(user_id)
        await telegram_service.send_message(
            chat_id, "👌 Напоминание по этому сообщению не создаётся."
        )
        return True

    if command == "snooze" and len(parts) == 4:
        lead_id = int(parts[2])
        row = await followup_service.snooze_followup(
            db,
            lead_id=lead_id,
            due_at=followup_service.preset_due_at(parts[3]),
        )
        await telegram_service.send_message(
            chat_id,
            f"⏰ Напоминание перенесено на <b>{followup_service.format_due_at(row.due_at)}</b>.",
        )
        return True

    if command == "replied" and len(parts) == 3:
        lead_id = int(parts[2])
        await followup_service.close_followup(
            db,
            lead_id=lead_id,
            reason="manager_confirmed_client_reply",
            waiting_on="us",
            action_text="Ответить клиенту",
            incoming_at=followup_service.utcnow(),
        )
        await telegram_service.send_message(
            chat_id,
            "💬 Ответ клиента зафиксирован. Состояние изменено на <b>«Клиент ждёт нас»</b>.",
        )
        return True

    if command == "close" and len(parts) == 3:
        lead_id = int(parts[2])
        await followup_service.close_followup(
            db,
            lead_id=lead_id,
            reason="manager_closed_waiting",
            waiting_on=None,
            action_text=None,
        )
        await telegram_service.send_message(chat_id, "✅ Ожидание закрыто.")
        return True

    if command == "prepare" and len(parts) == 3:
        lead_id = int(parts[2])
        await telegram_service.send_message(chat_id, "✍️ Готовлю follow-up с учётом переписки…")
        record = await followup_service.prepare_followup_draft(
            db, lead_id=lead_id, telegram_user_id=user_id
        )
        await telegram_service.send_message(
            chat_id,
            client_message_service.format_client_message_draft(record),
            reply_markup=client_message_service.message_draft_markup(record),
        )
        return True

    raise ValueError("Неизвестная команда follow-up.")


def install_followup_runtime_extensions() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True

    from app.api import telegram as telegram_api

    original_manager_callback = telegram_api._handle_manager_callback

    async def manager_callback_with_followup(**kwargs: Any) -> bool:
        callback_data = str(kwargs.get("callback_data") or "")
        if callback_data.startswith("followup:"):
            return await _handle_followup_callback(**kwargs)
        return await original_manager_callback(**kwargs)

    telegram_api._handle_manager_callback = manager_callback_with_followup

    original_client_callback = telegram_api._handle_client_message_callback

    async def client_callback_with_followup(**kwargs: Any) -> bool:
        callback_data = str(kwargs.get("callback_data") or "")
        handled = await original_client_callback(**kwargs)
        if handled and callback_data.startswith("clientmsg:sent:"):
            draft_id = int(callback_data.rsplit(":", 1)[1])
            await telegram_service.send_message(
                int(kwargs["chat_id"]),
                "⏰ <b>Когда проверить ответ клиента?</b>",
                reply_markup=followup_service.followup_prompt_markup(draft_id),
            )
        return handled

    telegram_api._handle_client_message_callback = client_callback_with_followup

    original_text_state = telegram_api._handle_text_state

    async def text_state_with_followup(**kwargs: Any) -> bool:
        user_id = int(kwargs["user_id"])
        state = await telegram_state_service.get_state(user_id)
        if state and state.get("mode") == "followup_custom_due":
            _assert_writer()
            draft_id = int(state.get("client_message_draft_id") or 0)
            try:
                due_at = followup_service.parse_custom_due_at(str(kwargs.get("text") or ""))
            except ValueError as exc:
                await telegram_service.send_message(
                    int(kwargs["chat_id"]), f"❌ {html.escape(str(exc))}"
                )
                return True
            result = await followup_service.schedule_from_draft(
                kwargs["db"],
                draft_id=draft_id,
                telegram_user_id=user_id,
                due_at=due_at,
                preset="custom",
            )
            await telegram_state_service.clear_state(user_id)
            await telegram_service.send_message(
                int(kwargs["chat_id"]),
                (
                    "✅ <b>Follow-up запланирован</b>\n\n"
                    f"Проверить ответ: <b>{followup_service.format_due_at(result['due_at'])}</b>"
                ),
            )
            return True
        return await original_text_state(**kwargs)

    telegram_api._handle_text_state = text_state_with_followup
