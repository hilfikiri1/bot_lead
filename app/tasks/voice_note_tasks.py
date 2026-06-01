"""
voice_note_tasks.py
Celery task: end-to-end voice note processing pipeline.
Telegram → Download → Transcribe → Analyse → Save → Report
"""
from __future__ import annotations

import asyncio
import logging

from app.celery_app import celery_app
from app.database import AsyncSessionLocal
from app.services import (
    telegram_service,
    transcription_service,
    ai_analysis_service,
    storage_service,
    crm_service,
)

logger = logging.getLogger(__name__)


def _run(coro):
    """Run an async coroutine in Celery's sync context."""
    return asyncio.get_event_loop().run_until_complete(coro)


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
):
    """
    Full pipeline for processing a voice note received via Telegram.
    """
    try:
        _run(_process(
            chat_id=chat_id,
            telegram_user_id=telegram_user_id,
            telegram_message_id=telegram_message_id,
            file_id=file_id,
            file_extension=file_extension,
        ))
    except Exception as exc:
        logger.exception("Voice note processing failed: %s", exc)
        _run(telegram_service.send_message(
            chat_id=chat_id,
            text=f"❌ Processing failed: {exc}\n\nPlease try again.",
        ))
        raise self.retry(exc=exc)


async def _process(
    chat_id: int,
    telegram_user_id: int,
    telegram_message_id: int,
    file_id: str,
    file_extension: str,
):
    async with AsyncSessionLocal() as db:
        # 1. Download audio
        logger.info("Downloading audio file_id=%s", file_id)
        audio_bytes = await telegram_service.download_voice(file_id)

        # 2. Save to storage
        audio_url = await storage_service.save_audio(audio_bytes, extension=file_extension)
        logger.info("Audio saved: %s", audio_url)

        # 3. Transcribe
        await telegram_service.send_message(chat_id, "🔄 Transcribing audio...")
        transcript, language = await transcription_service.transcribe_audio(
            audio_bytes, filename=f"audio.{file_extension}"
        )
        logger.info("Transcript: %s...", transcript[:100])

        # 4. AI analysis
        await telegram_service.send_message(chat_id, "🧠 Analysing with AI...")
        analysis = await ai_analysis_service.analyse_transcript(transcript)

        # 5. Save to database
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
        report = await crm_service.save_ai_report(db, voice_note, analysis)

        # 6. Send report to manager with action buttons
        report_text = telegram_service.format_report(analysis, transcript)
        await telegram_service.send_report(
            chat_id=chat_id,
            report_text=report_text,
            lead_id=lead.id,
            voice_note_id=voice_note.id,
        )
        logger.info("Report sent for voice_note_id=%d", voice_note.id)
