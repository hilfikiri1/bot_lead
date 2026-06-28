"""Telegram audio -> transcription -> AI analysis -> local DB -> approval report."""
from __future__ import annotations

import asyncio
import logging

from redis.asyncio import Redis
from sqlalchemy import select

from app.celery_app import celery_app
from app.config import get_settings
from app.database import AsyncSessionLocal
from app.models import VoiceNote
from app.services import (
    ai_analysis_service,
    crm_service,
    storage_service,
    telegram_service,
    transcription_service,
)

logger = logging.getLogger(__name__)
settings = get_settings()
PROCESSING_LOCK_TTL_SECONDS = 24 * 60 * 60


def _run(coro):
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    return loop.run_until_complete(coro)


def _processing_key(telegram_user_id: int, telegram_message_id: int) -> str:
    return f"telegram:audio:processing:{telegram_user_id}:{telegram_message_id}"


async def _acquire_processing_lock(
    telegram_user_id: int,
    telegram_message_id: int,
) -> bool:
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    try:
        acquired = await redis.set(
            _processing_key(telegram_user_id, telegram_message_id),
            "processing",
            nx=True,
            ex=PROCESSING_LOCK_TTL_SECONDS,
        )
        return bool(acquired)
    except Exception as exc:
        # PostgreSQL duplicate protection still exists, so a Redis outage must not
        # make audio permanently unprocessable.
        logger.warning("Could not acquire audio processing lock: %s", exc)
        return True
    finally:
        await redis.aclose()


async def _mark_processing_done(
    telegram_user_id: int,
    telegram_message_id: int,
) -> None:
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    try:
        await redis.set(
            _processing_key(telegram_user_id, telegram_message_id),
            "done",
            ex=PROCESSING_LOCK_TTL_SECONDS,
        )
    except Exception as exc:
        logger.warning("Could not mark audio processing as done: %s", exc)
    finally:
        await redis.aclose()


async def _release_processing_lock(
    telegram_user_id: int,
    telegram_message_id: int,
) -> None:
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    try:
        await redis.delete(_processing_key(telegram_user_id, telegram_message_id))
    except Exception as exc:
        logger.warning("Could not release audio processing lock: %s", exc)
    finally:
        await redis.aclose()


@celery_app.task(
    bind=True,
    name="app.tasks.voice_note_tasks.process_voice_note",
    max_retries=3,
    default_retry_delay=10,
    acks_late=True,
)
def process_voice_note(
    self,
    chat_id: int,
    telegram_user_id: int,
    telegram_message_id: int,
    file_id: str,
    file_extension: str = "ogg",
    target_kommo_lead_id: int | None = None,
):
    try:
        _run(
            process_voice_note_async(
                chat_id=chat_id,
                telegram_user_id=telegram_user_id,
                telegram_message_id=telegram_message_id,
                file_id=file_id,
                file_extension=file_extension,
                target_kommo_lead_id=target_kommo_lead_id,
            )
        )
    except Exception as exc:
        logger.exception("Audio processing failed")
        _run(
            telegram_service.send_message(
                chat_id=chat_id,
                text="❌ Ошибка обработки аудио. Подробности сохранены в Railway Logs.",
            )
        )
        raise self.retry(exc=exc)


async def process_voice_note_async(
    *,
    chat_id: int,
    telegram_user_id: int,
    telegram_message_id: int,
    file_id: str,
    file_extension: str = "ogg",
    target_kommo_lead_id: int | None = None,
) -> None:
    """Run the same pipeline inside the web process as a safe queue fallback."""
    await _process(
        chat_id=chat_id,
        telegram_user_id=telegram_user_id,
        telegram_message_id=telegram_message_id,
        file_id=file_id,
        file_extension=file_extension,
        target_kommo_lead_id=target_kommo_lead_id,
    )


async def _process(
    chat_id: int,
    telegram_user_id: int,
    telegram_message_id: int,
    file_id: str,
    file_extension: str,
    target_kommo_lead_id: int | None = None,
):
    if not await _acquire_processing_lock(telegram_user_id, telegram_message_id):
        logger.info(
            "Audio processing already claimed: user_id=%s message_id=%s",
            telegram_user_id,
            telegram_message_id,
        )
        return

    try:
        async with AsyncSessionLocal() as db:
            existing_result = await db.execute(
                select(VoiceNote.id).where(
                    VoiceNote.telegram_user_id == telegram_user_id,
                    VoiceNote.telegram_message_id == telegram_message_id,
                )
            )
            existing_voice_note_id = existing_result.scalar_one_or_none()
            if existing_voice_note_id is not None:
                logger.info(
                    "Duplicate audio task skipped: user_id=%s message_id=%s existing_voice_note_id=%s",
                    telegram_user_id,
                    telegram_message_id,
                    existing_voice_note_id,
                )
                await _mark_processing_done(telegram_user_id, telegram_message_id)
                return

            logger.info("Downloading Telegram audio")
            audio_bytes = await telegram_service.download_voice(file_id)

            audio_url = await storage_service.save_audio(audio_bytes, extension=file_extension)
            logger.info("Audio saved to configured storage")

            await telegram_service.send_message(chat_id, "🔄 Расшифровываю аудио...")
            transcript, language = await transcription_service.transcribe_audio(
                audio_bytes,
                filename=f"audio.{file_extension}",
            )
            logger.info("Transcription complete: %d chars", len(transcript))

            await telegram_service.send_message(chat_id, "🧠 Анализирую разговор...")
            analysis = await ai_analysis_service.analyse_transcript(transcript)

            client_data = analysis.get("client", {})
            lead_data = analysis.get("lead", {})

            client = await crm_service.upsert_client(db, client_data)
            lead = await crm_service.create_lead(db, client, lead_data)
            voice_note = await crm_service.save_voice_note(
                db=db,
                lead=lead,
                telegram_user_id=telegram_user_id,
                telegram_message_id=telegram_message_id,
                audio_url=audio_url,
                transcript=transcript,
                language=language,
            )
            await crm_service.save_ai_report(db, voice_note, analysis)

            report_text = telegram_service.format_report(analysis, transcript)
            await telegram_service.send_report(
                chat_id=chat_id,
                report_text=report_text,
                lead_id=lead.id,
                voice_note_id=voice_note.id,
                target_kommo_lead_id=target_kommo_lead_id,
            )
            logger.info("Approval report sent for voice_note_id=%d", voice_note.id)

        await _mark_processing_done(telegram_user_id, telegram_message_id)
    except Exception:
        await _release_processing_lock(telegram_user_id, telegram_message_id)
        raise
