"""
telegram.py — FastAPI router for Telegram webhook
"""
from __future__ import annotations

import hmac
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Request, Header, HTTPException, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import get_db
from app.services import telegram_service, approval_service
from app.tasks.voice_note_tasks import _process as process_voice_note

router = APIRouter(prefix="/webhook", tags=["telegram"])
logger = logging.getLogger(__name__)
settings = get_settings()


def _verify_secret(x_telegram_bot_api_secret_token: str | None) -> bool:
    expected = settings.telegram_webhook_secret
    if not expected:
        return True
    return hmac.compare_digest(x_telegram_bot_api_secret_token or "", expected)


def _is_allowed_user(user_id: int) -> bool:
    """Check if a Telegram user ID is in the allowed list."""
    allowed = settings.get_allowed_user_ids()
    # If no list configured, deny all admin commands by default
    if not allowed:
        return False
    return user_id in allowed


async def _handle_kommo_test(chat_id: int, user_id: int, db: AsyncSession) -> None:
    """
    Handle /kommo_test command.
    Read-only connection test: account info + leads count.
    Saves result to PostgreSQL. Never exposes tokens.
    """
    from app.services.kommo_service import test_connection
    from app.models.integration_check import IntegrationCheck

    # Access check
    if not _is_allowed_user(user_id):
        await telegram_service.send_message(chat_id, "Доступ запрещен.")
        return

    await telegram_service.send_message(chat_id, "🔄 Проверяю связь с Kommo...")

    result = await test_connection()
    checked_at = datetime.now(tz=timezone.utc)

    # Save to PostgreSQL (no tokens stored)
    try:
        account = result.get("account") or {}
        check = IntegrationCheck(
            integration_name="kommo",
            status="ok" if result["success"] else "error",
            account_id=account.get("account_id"),
            account_name=account.get("account_name"),
            details={
                "subdomain": account.get("subdomain"),
                "timezone": account.get("timezone"),
                "leads_accessible": result.get("leads_accessible"),
                "leads_count": result.get("leads_count"),
            },
            error_message=result.get("error"),
            telegram_user_id=user_id,
            checked_at=checked_at,
        )
        db.add(check)
        await db.commit()
        db_saved = True
    except Exception as e:
        logger.error("Failed to save integration check to DB: %s", e)
        db_saved = False

    # Build Telegram response
    ts = checked_at.strftime("%Y-%m-%d %H:%M UTC")

    if result["success"]:
        account = result["account"]
        leads_status = "✅ работает" if result["leads_accessible"] else "⚠️ нет доступа"
        leads_count = result["leads_count"]
        db_status = "✅ запись сохранена" if db_saved else "⚠️ ошибка записи"

        msg = (
            "✅ <b>Связь с Kommo работает</b>\n\n"
            f"Аккаунт: <b>{account.get('account_name') or '—'}</b>\n"
            f"Account ID: <code>{account.get('account_id') or '—'}</code>\n"
            f"Поддомен: {account.get('subdomain') or '—'}.kommo.com\n"
            f"Часовой пояс: {account.get('timezone') or '—'}\n\n"
            f"Доступ к сделкам: {leads_status}\n"
            f"Найдено сделок: {leads_count}\n\n"
            f"PostgreSQL: {db_status}\n"
            f"Время проверки: {ts}"
        )
    else:
        error = result.get("error", "Неизвестная ошибка")
        msg = (
            "❌ <b>Не удалось подключиться к Kommo</b>\n\n"
            f"Ошибка: {error}\n\n"
            "Проверь переменные Railway:\n"
            "• <code>KOMMO_BASE_URL</code>\n"
            "• <code>KOMMO_ACCESS_TOKEN</code>"
        )

    await telegram_service.send_message(chat_id, msg)


@router.post("/telegram")
async def telegram_webhook(
    request: Request,
    db: AsyncSession = Depends(get_db),
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
):
    if not _verify_secret(x_telegram_bot_api_secret_token):
        raise HTTPException(status_code=403, detail="Invalid secret token")

    try:
        body = await request.json()
    except Exception:
        return {"ok": True}

    logger.debug("Telegram update: %s", str(body)[:300])

    try:
        # Handle callback queries (inline button presses)
        if "callback_query" in body:
            cq = body["callback_query"]
            callback_data = cq.get("data", "")
            chat_id = cq["message"]["chat"]["id"]
            user_id = cq["from"]["id"]
            cq_id = cq["id"]

            try:
                await telegram_service.answer_callback_query(cq_id)
            except Exception as e:
                logger.warning("answer_callback_query failed: %s", e)

            try:
                result_msg = await approval_service.handle_callback(
                    db=db,
                    callback_data=callback_data,
                    telegram_user_id=user_id,
                    chat_id=chat_id,
                )
                await telegram_service.send_message(chat_id, result_msg)
            except Exception as e:
                logger.error("Callback handling failed: %s", e)

            return {"ok": True}

        # Handle incoming messages
        message = body.get("message", {})
        if not message:
            return {"ok": True}

        chat_id = message["chat"]["id"]
        user_id = message["from"]["id"]
        message_id = message["message_id"]

        # Voice message
        voice = message.get("voice") or message.get("audio")
        if voice:
            file_id = voice["file_id"]
            mime = voice.get("mime_type", "audio/ogg")
            extension = mime.split("/")[-1].replace("mpeg", "mp3")

            try:
                await telegram_service.send_message(
                    chat_id,
                    "🎙 Voice message received. Processing it now… please wait.",
                )
            except Exception as e:
                logger.warning("Could not send acknowledgement: %s", e)

            try:
                await process_voice_note(
                    chat_id=chat_id,
                    telegram_user_id=user_id,
                    telegram_message_id=message_id,
                    file_id=file_id,
                    file_extension=extension,
                )
            except Exception as e:
                logger.error("Voice note processing failed: %s", e)
                try:
                    await telegram_service.send_message(
                        chat_id,
                        "❌ Sorry, something went wrong while processing your voice message. Please try again.",
                    )
                except Exception:
                    pass
            return {"ok": True}

        # Text messages / commands
        text = message.get("text", "").strip()

        # ─── /kommo_test ──────────────────────────────────────────────────
        if text.startswith("/kommo_test"):
            try:
                await _handle_kommo_test(chat_id, user_id, db)
            except Exception as e:
                logger.error("kommo_test handler error: %s", e)
                try:
                    await telegram_service.send_message(chat_id, "❌ Внутренняя ошибка при проверке Kommo.")
                except Exception:
                    pass
            return {"ok": True}

        # ─── /start ───────────────────────────────────────────────────────
        if text.startswith("/start"):
            try:
                await telegram_service.send_message(
                    chat_id,
                    (
                        "👋 <b>Welcome to Buy & Bring Voice Bot</b>\n\n"
                        "Send me a voice note after your client call and I'll:\n"
                        "• Transcribe it\n"
                        "• Analyse what was discussed\n"
                        "• Prepare an email, WhatsApp, and calendar follow-up\n\n"
                        "Just send a voice message to get started!"
                    ),
                )
            except Exception as e:
                logger.warning("Could not send start message: %s", e)
        else:
            try:
                await telegram_service.send_message(
                    chat_id,
                    "Please send a voice message to process.",
                )
            except Exception as e:
                logger.warning("Could not send message: %s", e)

    except Exception as e:
        logger.error("Webhook handler error: %s", e)

    # Always return 200 to Telegram
    return {"ok": True}
