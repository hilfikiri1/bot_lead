"""Reliable Telegram audio -> transcription -> Russian AI analysis pipeline."""

from __future__ import annotations

import asyncio
import logging

from redis.asyncio import Redis

from app.celery_app import celery_app
from app.config import get_settings
from app.database import AsyncSessionLocal
from app.services import (
    ai_analysis_service,
    command_router_service,
    crm_service,
    notion_service,
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


async def _acquire_processing_lock(user_id: int, message_id: int) -> bool:
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    try:
        return bool(
            await redis.set(
                _processing_key(user_id, message_id),
                "processing",
                nx=True,
                ex=PROCESSING_LOCK_TTL_SECONDS,
            )
        )
    except Exception as exc:
        logger.warning("Could not acquire audio processing lock: %s", exc)
        return True
    finally:
        await redis.aclose()


async def _mark_processing_done(user_id: int, message_id: int) -> None:
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    try:
        await redis.set(
            _processing_key(user_id, message_id),
            "done",
            ex=PROCESSING_LOCK_TTL_SECONDS,
        )
    except Exception as exc:
        logger.warning("Could not mark audio processing as done: %s", exc)
    finally:
        await redis.aclose()


async def _release_processing_lock(user_id: int, message_id: int) -> None:
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    try:
        await redis.delete(_processing_key(user_id, message_id))
    except Exception as exc:
        logger.warning("Could not release audio processing lock: %s", exc)
    finally:
        await redis.aclose()


@celery_app.task(
    bind=True,
    name="app.tasks.voice_note_tasks.process_voice_note",
    max_retries=2,
    default_retry_delay=15,
    acks_late=True,
    soft_time_limit=15 * 60,
    time_limit=17 * 60,
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
                notify_failure=False,
            )
        )
    except Exception as exc:
        logger.exception("Audio processing failed")
        if self.request.retries < self.max_retries:
            raise self.retry(exc=exc)
        _run(
            telegram_service.send_message(
                chat_id,
                "❌ <b>Не удалось обработать аудио</b>\n\n"
                "Откройте <b>Статус обработки</b> в меню и попробуйте отправить файл ещё раз.",
            )
        )
        raise


async def process_voice_note_async(
    *,
    chat_id: int,
    telegram_user_id: int,
    telegram_message_id: int,
    file_id: str,
    file_extension: str = "ogg",
    target_kommo_lead_id: int | None = None,
    notify_failure: bool = True,
) -> None:
    await _process(
        chat_id=chat_id,
        telegram_user_id=telegram_user_id,
        telegram_message_id=telegram_message_id,
        file_id=file_id,
        file_extension=file_extension,
        target_kommo_lead_id=target_kommo_lead_id,
        notify_failure=notify_failure,
    )


async def _process(
    *,
    chat_id: int,
    telegram_user_id: int,
    telegram_message_id: int,
    file_id: str,
    file_extension: str,
    target_kommo_lead_id: int | None = None,
    notify_failure: bool = True,
) -> None:
    if not await _acquire_processing_lock(telegram_user_id, telegram_message_id):
        logger.info(
            "Audio processing already claimed: user_id=%s message_id=%s",
            telegram_user_id,
            telegram_message_id,
        )
        return

    voice_note = None
    try:
        async with AsyncSessionLocal() as db:
            voice_note, created = await crm_service.get_or_create_voice_note_job(
                db,
                telegram_user_id=telegram_user_id,
                telegram_message_id=telegram_message_id,
                processing_status="received",
            )
            if not created and voice_note.processing_status == "ready":
                logger.info(
                    "Completed duplicate audio job skipped: voice_note_id=%s",
                    voice_note.id,
                )
                await _mark_processing_done(telegram_user_id, telegram_message_id)
                return
            if not created and voice_note.processing_status in {
                "downloading",
                "transcribing",
                "analyzing",
                "saving",
            }:
                logger.info(
                    "Active duplicate audio job skipped: voice_note_id=%s",
                    voice_note.id,
                )
                return

            await crm_service.update_voice_note_status(db, voice_note, "downloading")
            await telegram_service.send_processing_step(
                chat_id,
                "download",
                target_kommo_lead_id=target_kommo_lead_id,
            )
            logger.info("Downloading Telegram audio")
            audio_bytes = await telegram_service.download_voice(file_id)
            audio_url = await storage_service.save_audio(
                audio_bytes, extension=file_extension
            )

            await crm_service.update_voice_note_status(db, voice_note, "transcribing")
            await telegram_service.send_processing_step(
                chat_id,
                "transcribe",
                target_kommo_lead_id=target_kommo_lead_id,
            )
            transcript, language = await transcription_service.transcribe_audio(
                audio_bytes,
                filename=f"audio.{file_extension}",
            )
            logger.info("Transcription complete: %d chars", len(transcript))

            command_context = await crm_service.get_user_command_context(
                db, telegram_user_id=telegram_user_id
            )
            if target_kommo_lead_id:
                command_context["kommo_lead_id"] = target_kommo_lead_id
            plan = await command_router_service.classify_message(
                transcript, context=command_context
            )
            if plan.intent != "analyze_conversation":
                command_reply = await command_router_service.execute_plan(
                    db,
                    plan=plan,
                    chat_id=chat_id,
                    telegram_user_id=telegram_user_id,
                    context=command_context,
                )
                if command_reply is not None:
                    voice_note.audio_url = audio_url
                    voice_note.transcript = transcript
                    voice_note.language = language
                    await crm_service.update_voice_note_status(
                        db,
                        voice_note,
                        "ready",
                        finished=True,
                    )
                    if command_reply:
                        await telegram_service.send_message(chat_id, command_reply)
                    await _mark_processing_done(telegram_user_id, telegram_message_id)
                    return

            await crm_service.update_voice_note_status(db, voice_note, "analyzing")
            await telegram_service.send_processing_step(
                chat_id,
                "analyze",
                target_kommo_lead_id=target_kommo_lead_id,
            )
            analysis = await ai_analysis_service.analyse_transcript(transcript)

            await crm_service.update_voice_note_status(db, voice_note, "saving")
            client = await crm_service.upsert_client(db, analysis.get("client", {}))
            lead = await crm_service.create_lead(db, client, analysis.get("lead", {}))
            voice_note = await crm_service.complete_voice_note_job(
                db,
                voice_note,
                lead=lead,
                audio_url=audio_url,
                transcript=transcript,
                language=language,
            )
            await crm_service.save_ai_report(db, voice_note, analysis)

            lead_title = str(
                analysis.get("lead", {}).get("proposed_name")
                or lead.product_requested
                or "Новый запрос"
            )
            try:
                notion_result = await notion_service.sync_analyzed_call(
                    client_id=client.id,
                    client_name=client.name,
                    client_company=client.company,
                    client_phone=client.phone,
                    client_email=client.email,
                    client_language=client.language,
                    client_notion_page_id=client.notion_page_id,
                    lead_id=lead.id,
                    lead_title=lead_title,
                    lead_product=lead.product_requested,
                    lead_budget=lead.budget,
                    lead_country=lead.country,
                    lead_city=lead.city,
                    lead_kommo_url=lead.kommo_url,
                    lead_kommo_id=lead.kommo_lead_id,
                    lead_notion_page_id=lead.notion_page_id,
                    voice_note_id=voice_note.id,
                    transcript=transcript,
                    audio_url=audio_url,
                    analysis=analysis,
                )
                await crm_service.save_notion_mapping(
                    db,
                    client_id=client.id,
                    lead_id=lead.id,
                    voice_note_id=voice_note.id,
                    client_page_id=notion_result.client_page_id,
                    lead_page_id=notion_result.lead_page_id,
                    call_page_id=notion_result.call_page_id,
                )
                notion_line = (
                    f"\n\n📓 {notion_result.message}"
                    if notion_result.call_page_id
                    else ""
                )
            except Exception as exc:
                logger.warning("Notion sync failed for voice_note_id=%s: %s", voice_note.id, exc)
                notion_line = "\n\n⚠️ Notion: не удалось сохранить автоматически."

            report_text = telegram_service.format_report(analysis, transcript)
            report_text += notion_line
            await telegram_service.send_report(
                chat_id=chat_id,
                report_text=report_text,
                lead_id=lead.id,
                voice_note_id=voice_note.id,
                target_kommo_lead_id=target_kommo_lead_id,
            )
            logger.info("Approval report sent for voice_note_id=%d", voice_note.id)

        await _mark_processing_done(telegram_user_id, telegram_message_id)
    except Exception as exc:
        logger.exception("Voice note pipeline failed")
        if voice_note is not None:
            try:
                async with AsyncSessionLocal() as error_db:
                    refreshed = await error_db.get(type(voice_note), int(voice_note.id))
                    if refreshed:
                        await crm_service.update_voice_note_status(
                            error_db,
                            refreshed,
                            "failed",
                            error=str(exc),
                            finished=True,
                        )
            except Exception:
                logger.exception("Could not persist failed audio status")
        if notify_failure:
            try:
                await telegram_service.send_message(
                    chat_id,
                    "❌ <b>Обработка аудио остановлена</b>\n\n"
                    "Ошибка сохранена в статусе задания. Откройте /jobs и повторите отправку файла.",
                )
            except Exception:
                logger.exception("Could not notify user about failed audio processing")
        await _release_processing_lock(telegram_user_id, telegram_message_id)
        raise
