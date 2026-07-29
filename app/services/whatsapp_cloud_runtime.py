"""Telegram runtime wiring for confirmed WhatsApp Cloud API sends."""
from __future__ import annotations

import html
from typing import Any

from app.services import (
    client_message_service,
    followup_service,
    identity_service,
    telegram_service,
    whatsapp_cloud_service,
)

_INSTALLED = False
_MANUAL_HINT = "Сообщение ещё не отправлено. Отправка выполняется вручную с вашего телефона."


def _assert_writer() -> None:
    if not identity_service.can_write(identity_service.current_user()):
        raise PermissionError("Роль Viewer позволяет только просматривать данные.")


def _cloud_button(record: Any) -> dict[str, str] | None:
    if (
        getattr(record, "channel", None) == "whatsapp"
        and getattr(record, "recipient", None)
        and getattr(record, "status", "prepared") not in {"sent", "cancelled"}
        and whatsapp_cloud_service.enabled()
    ):
        return {
            "text": "📤 Отправить через WhatsApp API",
            "callback_data": f"clientmsg:sendapi:{int(record.id)}",
        }
    return None


def install_whatsapp_cloud_runtime() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True

    from app.api import telegram as telegram_api

    original_markup = client_message_service.message_draft_markup

    def markup_with_cloud_send(record: Any) -> dict[str, Any]:
        markup = dict(original_markup(record) or {})
        rows = [list(row) for row in (markup.get("inline_keyboard") or [])]
        button = _cloud_button(record)
        if button:
            rows.insert(0, [button])
        return {"inline_keyboard": rows}

    client_message_service.message_draft_markup = markup_with_cloud_send

    original_format = client_message_service.format_client_message_draft

    def format_with_cloud_status(record: Any) -> str:
        text = original_format(record)
        if getattr(record, "channel", None) != "whatsapp":
            return text
        if whatsapp_cloud_service.enabled():
            text = text.replace(
                _MANUAL_HINT,
                "Сообщение ещё не отправлено. Можно отправить через API или открыть WhatsApp вручную.",
            )
            extra = (
                "\n\n<b>WhatsApp Cloud API:</b> настроен. "
                "Свободный текст отправляется только внутри 24-часового окна после сообщения клиента."
            )
        else:
            extra = (
                "\n\n<b>WhatsApp Cloud API:</b> не настроен. "
                "Нужны WHATSAPP_ACCESS_TOKEN и WHATSAPP_PHONE_NUMBER_ID."
            )
        return (text + extra)[:4000]

    client_message_service.format_client_message_draft = format_with_cloud_status

    original_client_callback = telegram_api._handle_client_message_callback

    async def client_callback_with_cloud(**kwargs: Any) -> bool:
        callback_data = str(kwargs.get("callback_data") or "")
        if not callback_data.startswith("clientmsg:sendapi:"):
            return await original_client_callback(**kwargs)

        _assert_writer()
        draft_id = int(callback_data.rsplit(":", 1)[1])
        chat_id = int(kwargs["chat_id"])
        user_id = int(kwargs["user_id"])
        db = kwargs["db"]
        await telegram_service.send_message(
            chat_id, "📤 Отправляю сообщение через Meta WhatsApp Cloud API…"
        )
        try:
            result = await whatsapp_cloud_service.send_draft(
                db,
                draft_id=draft_id,
                telegram_user_id=user_id,
            )
        except whatsapp_cloud_service.WhatsAppWindowClosedError as exc:
            await telegram_service.send_message(
                chat_id,
                (
                    "⚠️ <b>Нельзя отправить обычный текст</b>\n\n"
                    f"{html.escape(str(exc))}\n\n"
                    "Клиент должен сначала написать сам либо нужно использовать одобренный шаблон Meta. "
                    "Шаблоны будут подключены в следующем модуле."
                ),
            )
            return True
        except Exception as exc:
            await telegram_service.send_message(
                chat_id,
                "❌ <b>WhatsApp API не отправил сообщение</b>\n\n"
                + html.escape(str(exc)[:1500]),
            )
            return True

        provider_id = str(result.get("provider_message_id") or "—")
        idempotent = bool(result.get("idempotent"))
        await telegram_service.send_message(
            chat_id,
            (
                "✅ <b>СООБЩЕНИЕ ПРИНЯТО META</b>\n\n"
                f"Meta message ID: <code>{html.escape(provider_id)}</code>\n"
                + (
                    "Повторная отправка не выполнялась: этот черновик уже был отправлен."
                    if idempotent
                    else "Статусы доставки и прочтения будут обновляться по webhook."
                )
            ),
        )
        if followup_service.enabled():
            await telegram_service.send_message(
                chat_id,
                "⏰ <b>Когда проверить ответ клиента?</b>",
                reply_markup=followup_service.followup_prompt_markup(draft_id),
            )
        return True

    telegram_api._handle_client_message_callback = client_callback_with_cloud
