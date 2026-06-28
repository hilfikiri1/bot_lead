"""FastAPI router for the Telegram webhook."""
from __future__ import annotations

import hmac
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import get_db
from app.services import approval_service, telegram_service
from app.tasks.voice_note_tasks import _process as process_voice_note

router = APIRouter(prefix="/webhook", tags=["telegram"])
logger = logging.getLogger(__name__)
settings = get_settings()

SUPPORTED_AUDIO_EXTENSIONS = {
    "ogg",
    "oga",
    "mp3",
    "mp4",
    "mpeg",
    "mpga",
    "m4a",
    "wav",
    "webm",
}
MIME_EXTENSION_MAP = {
    "audio/ogg": "ogg",
    "application/ogg": "ogg",
    "audio/mpeg": "mp3",
    "audio/mp3": "mp3",
    "audio/mp4": "m4a",
    "audio/x-m4a": "m4a",
    "video/mp4": "mp4",
    "audio/wav": "wav",
    "audio/x-wav": "wav",
    "audio/webm": "webm",
    "video/webm": "webm",
}


def _verify_secret(x_telegram_bot_api_secret_token: str | None) -> bool:
    expected = settings.telegram_webhook_secret
    if not expected:
        return True
    return hmac.compare_digest(x_telegram_bot_api_secret_token or "", expected)


def _is_allowed_user(user_id: int) -> bool:
    allowed = settings.get_allowed_user_ids()
    if not allowed:
        return False
    return user_id in allowed


def _safe_extension(file_name: str | None, mime_type: str | None, default: str) -> str:
    if file_name:
        suffix = Path(file_name).suffix.lower().lstrip(".")
        if suffix in SUPPORTED_AUDIO_EXTENSIONS:
            return suffix
    mapped = MIME_EXTENSION_MAP.get((mime_type or "").lower())
    if mapped:
        return mapped
    return default


def _extract_audio_attachment(message: dict[str, Any]) -> dict[str, Any] | None:
    """Accept Telegram voice/audio and audio files uploaded as documents, including .m4a."""
    voice = message.get("voice")
    if voice:
        return {
            "file_id": voice["file_id"],
            "file_size": voice.get("file_size"),
            "file_extension": _safe_extension(None, voice.get("mime_type"), "ogg"),
            "kind": "voice",
        }

    audio = message.get("audio")
    if audio:
        return {
            "file_id": audio["file_id"],
            "file_size": audio.get("file_size"),
            "file_extension": _safe_extension(
                audio.get("file_name"), audio.get("mime_type"), "mp3"
            ),
            "kind": "audio",
        }

    document = message.get("document")
    if not document:
        return None

    file_name = document.get("file_name") or ""
    mime_type = (document.get("mime_type") or "").lower()
    extension = Path(file_name).suffix.lower().lstrip(".")
    looks_like_audio = (
        extension in SUPPORTED_AUDIO_EXTENSIONS
        or mime_type.startswith("audio/")
        or mime_type in MIME_EXTENSION_MAP
    )
    if not looks_like_audio:
        return None

    return {
        "file_id": document["file_id"],
        "file_size": document.get("file_size"),
        "file_extension": _safe_extension(file_name, mime_type, "m4a"),
        "kind": "document",
    }


async def _handle_kommo_test(chat_id: int, user_id: int, db: AsyncSession) -> None:
    from app.models.integration_check import IntegrationCheck
    from app.services.kommo_service import test_connection

    if not _is_allowed_user(user_id):
        await telegram_service.send_message(chat_id, "Доступ запрещён.")
        return

    await telegram_service.send_message(chat_id, "🔄 Проверяю связь с Kommo...")
    result = await test_connection()
    checked_at = datetime.now(tz=timezone.utc)

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
    except Exception as exc:
        await db.rollback()
        logger.error("Failed to save integration check: %s", exc)
        db_saved = False

    timestamp = checked_at.strftime("%Y-%m-%d %H:%M UTC")
    if result["success"]:
        account = result["account"]
        leads_status = "✅ работает" if result["leads_accessible"] else "⚠️ нет доступа"
        db_status = "✅ запись сохранена" if db_saved else "⚠️ ошибка записи"
        message = (
            "✅ <b>Связь с Kommo работает</b>\n\n"
            f"Аккаунт: <b>{account.get('account_name') or '—'}</b>\n"
            f"Account ID: <code>{account.get('account_id') or '—'}</code>\n"
            f"Поддомен: {account.get('subdomain') or '—'}.kommo.com\n"
            f"Часовой пояс: {account.get('timezone') or '—'}\n\n"
            f"Доступ к сделкам: {leads_status}\n"
            f"Получено тестовых сделок: {result['leads_count']}\n\n"
            f"PostgreSQL: {db_status}\n"
            f"Время проверки: {timestamp}"
        )
    else:
        message = (
            "❌ <b>Не удалось подключиться к Kommo</b>\n\n"
            f"Ошибка: {result.get('error', 'Неизвестная ошибка')}\n\n"
            "Проверьте Railway Variables:\n"
            "• <code>KOMMO_BASE_URL</code>\n"
            "• <code>KOMMO_ACCESS_TOKEN</code>"
        )
    await telegram_service.send_message(chat_id, message)


async def _handle_open_leads(chat_id: int, user_id: int) -> None:
    if not _is_allowed_user(user_id):
        await telegram_service.send_message(chat_id, "Доступ запрещён.")
        return

    from app.services.kommo_service import get_all_open_leads

    await telegram_service.send_message(chat_id, "🔄 Загружаю открытые сделки из Kommo...")
    try:
        result = await get_all_open_leads()
        chunks = telegram_service.format_open_leads_messages(result)
        await telegram_service.send_message_chunks(chat_id, chunks)
    except Exception as exc:
        logger.exception("Open leads listing failed")
        await telegram_service.send_message(
            chat_id,
            f"❌ Не удалось получить открытые сделки: {str(exc)[:500]}",
        )


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

    logger.debug("Telegram update keys: %s", list(body.keys()))

    try:
        if "callback_query" in body:
            callback = body["callback_query"]
            callback_data = callback.get("data", "")
            chat_id = callback["message"]["chat"]["id"]
            user_id = callback["from"]["id"]
            callback_id = callback["id"]

            try:
                await telegram_service.answer_callback_query(callback_id)
            except Exception as exc:
                logger.warning("answer_callback_query failed: %s", exc)

            if not _is_allowed_user(user_id):
                await telegram_service.send_message(chat_id, "Доступ запрещён.")
                return {"ok": True}

            try:
                result_message = await approval_service.handle_callback(
                    db=db,
                    callback_data=callback_data,
                    telegram_user_id=user_id,
                    chat_id=chat_id,
                )
                await telegram_service.send_message(chat_id, result_message)
            except Exception as exc:
                logger.exception("Callback handling failed")
                await telegram_service.send_message(
                    chat_id,
                    f"❌ Ошибка выполнения действия: {str(exc)[:500]}",
                )
            return {"ok": True}

        message = body.get("message", {})
        if not message:
            return {"ok": True}

        chat_id = message["chat"]["id"]
        user_id = message["from"]["id"]
        message_id = message["message_id"]
        text = (message.get("text") or "").strip()

        if text.startswith("/kommo_test"):
            await _handle_kommo_test(chat_id, user_id, db)
            return {"ok": True}

        if text.startswith(("/kommo_leads", "/open_deals", "/deals")):
            await _handle_open_leads(chat_id, user_id)
            return {"ok": True}

        if text.startswith("/start"):
            await telegram_service.send_message(
                chat_id,
                (
                    "👋 <b>Buy & Bring Assistant</b>\n\n"
                    "Доступные действия:\n"
                    "• отправьте голосовое сообщение или аудиофайл .m4a/.mp3/.wav/.mp4/.webm;\n"
                    "• после анализа нажмите <b>«Добавить лид в Kommo»</b>;\n"
                    "• <code>/kommo_leads</code> — показать все открытые сделки;\n"
                    "• <code>/kommo_test</code> — проверить интеграцию.\n\n"
                    "Лид в Kommo создаётся только после вашего подтверждения кнопкой."
                ),
            )
            return {"ok": True}

        attachment = _extract_audio_attachment(message)
        if attachment:
            if not _is_allowed_user(user_id):
                await telegram_service.send_message(chat_id, "Доступ запрещён.")
                return {"ok": True}

            max_bytes = min(settings.max_audio_file_size_mb, 20) * 1024 * 1024
            file_size = attachment.get("file_size")
            if file_size and file_size > max_bytes:
                await telegram_service.send_message(
                    chat_id,
                    (
                        f"❌ Файл слишком большой: максимум {min(settings.max_audio_file_size_mb, 20)} МБ "
                        "для загрузки через Telegram Bot API."
                    ),
                )
                return {"ok": True}

            await telegram_service.send_message(
                chat_id,
                "🎙 Аудио получено. Загружаю и распознаю...",
            )
            try:
                await process_voice_note(
                    chat_id=chat_id,
                    telegram_user_id=user_id,
                    telegram_message_id=message_id,
                    file_id=attachment["file_id"],
                    file_extension=attachment["file_extension"],
                )
            except Exception:
                logger.exception("Voice/audio processing failed")
                await telegram_service.send_message(
                    chat_id,
                    "❌ Ошибка обработки аудио. Посмотрите Deploy Logs Railway.",
                )
            return {"ok": True}

        if message.get("document"):
            await telegram_service.send_message(
                chat_id,
                "Формат файла не поддерживается. Отправьте .m4a, .mp3, .mp4, .wav, .ogg или .webm размером до 20 МБ.",
            )
            return {"ok": True}

        await telegram_service.send_message(
            chat_id,
            "Отправьте голосовое сообщение/аудиофайл или используйте /kommo_leads.",
        )

    except Exception:
        logger.exception("Telegram webhook handler error")

    return {"ok": True}
