"""
telegram.py — FastAPI router for Telegram webhook
"""
from __future__ import annotations

import hmac
import logging

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

        # Text messages
        text = message.get("text", "")
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

    # Always return 200 to Telegram — never let it retry endlessly
    return {"ok": True}
