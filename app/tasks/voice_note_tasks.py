"""Reliable Telegram audio -> transcription -> Russian AI analysis pipeline."""

from __future__ import annotations

import asyncio
import html
import logging

from redis.asyncio import Redis

from app.celery_app import celery_app
from app.agent import actions as agent_actions
from app.agent import service as agent_service
from app.config import get_settings
from app.database import AsyncSessionLocal
from app.services import (
    ai_analysis_service,
    command_router_service,
    crm_service,
    notion_service,
    identity_service,
    storage_service,
    telegram_service,
    transcription_service,
)

logger = logging.getLogger(__name__)
settings = get_settings()
PROCESSING_LOCK_TTL_SECONDS = 24 * 60 * 60


def _user_error_text(exc: Exception, *, limit: int = 420) -> str:
    message = str(exc).strip() or exc.__class__.__name__
    return html.escape(message[:limit])


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
    audio_intent: str = "command",
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
                audio_intent=audio_intent,
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
    audio_intent: str = "command",
    notify_failure: bool = True,
) -> None:
    await _process(
        chat_id=chat_id,
        telegram_user_id=telegram_user_id,
        telegram_message_id=telegram_message_id,
        file_id=file_id,
        file_extension=file_extension,
        target_kommo_lead_id=target_kommo_lead_id,
        audio_intent=audio_intent,
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
    audio_intent: str = "command",
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
            actor = await identity_service.authorize_telegram_user(
                db, telegram_user_id=telegram_user_id
            )
            if actor is None:
                raise PermissionError("Telegram-пользователь больше не имеет доступа к агенту.")
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
                command_mode=audio_intent == "command",
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
                command_mode=audio_intent == "command",
            )
            transcript, language = await transcription_service.transcribe_audio(
                audio_bytes,
                filename=f"audio.{file_extension}",
            )
            logger.info("Transcription complete: %d chars", len(transcript))

            if audio_intent == "command":
                if settings.agent_enabled:
                    agent_reply = await agent_service.handle_message(
                        db,
                        chat_id=chat_id,
                        telegram_user_id=telegram_user_id,
                        text=transcript,
                        source="voice",
                        allow_conversation_passthrough=settings.agent_auto_voice_mode,
                        active_kommo_lead_id=target_kommo_lead_id,
                    )
                    if agent_reply.handled:
                        voice_note.audio_url = audio_url
                        voice_note.transcript = transcript
                        voice_note.language = language
                        await crm_service.update_voice_note_status(
                            db, voice_note, "ready", finished=True
                        )
                        if agent_reply.text:
                            await telegram_service.send_message(
                                chat_id,
                                agent_reply.text,
                                reply_markup=agent_reply.reply_markup,
                            )
                        await _mark_processing_done(
                            telegram_user_id, telegram_message_id
                        )
                        return
                    # The agent classified this as a client conversation. Continue
                    # through the existing structured call-analysis pipeline.
                    audio_intent = "new_lead"
                else:
                    command_context = await crm_service.get_user_command_context(
                        db, telegram_user_id=telegram_user_id
                    )
                    try:
                        plan = await command_router_service.classify_message(
                            transcript,
                            context=command_context,
                            command_only=True,
                        )
                    except Exception as exc:
                        logger.warning("Command classification failed: %s", exc)
                        plan = command_router_service.CommandPlan("unknown", 0.0, {})
                    try:
                        command_reply = await command_router_service.execute_plan(
                            db,
                            plan=plan,
                            chat_id=chat_id,
                            telegram_user_id=telegram_user_id,
                            context=command_context,
                            source_text=transcript,
                        )
                    except Exception as exc:
                        logger.exception("Voice command execution failed")
                        await crm_service.update_voice_note_status(
                            db,
                            voice_note,
                            "failed",
                            error=f"command:{_user_error_text(exc)}",
                            finished=True,
                        )
                        await telegram_service.send_message(
                            chat_id,
                            (
                                "❌ <b>Команда не выполнена</b>\n\n"
                                f"<code>{_user_error_text(exc)}</code>"
                            ),
                        )
                        await _mark_processing_done(
                            telegram_user_id, telegram_message_id
                        )
                        return

                    voice_note.audio_url = audio_url
                    voice_note.transcript = transcript
                    voice_note.language = language
                    await crm_service.update_voice_note_status(
                        db, voice_note, "ready", finished=True
                    )
                    if command_reply:
                        await telegram_service.send_message(chat_id, command_reply)
                    elif plan.intent in {"unknown", "analyze_conversation"}:
                        await telegram_service.send_message(
                            chat_id, command_router_service.COMMAND_NOT_RECOGNIZED
                        )
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
            notion_action_id: int | None = None
            if settings.agent_enabled:
                # Notion is an external write. In Agent v3 it is never performed
                # silently: the manager receives a separate confirmation button.
                action = await agent_actions.stage_action(
                    db,
                    telegram_user_id=telegram_user_id,
                    chat_id=chat_id,
                    action_type="sync_call_analysis_to_notion",
                    payload={
                        "local_lead_id": int(lead.id),
                        "voice_note_id": int(voice_note.id),
                    },
                    preview_text=(
                        "📓 Сохранить анализ разговора в Notion\n\n"
                        f"Проект: {lead_title}\n"
                        "Будут созданы или обновлены связанные записи клиента, "
                        "проекта и звонка."
                    ),
                )
                notion_action_id = int(action.id)
                notion_line = (
                    "\n\n📓 <b>Notion</b>: анализ ещё не сохранён. "
                    "Нажми кнопку ниже, чтобы подтвердить запись."
                )
            else:
                # Compatibility path for installations that explicitly disable
                # the unified agent and keep the legacy automatic integration.
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
                except notion_service.NotionAPIError as exc:
                    logger.warning(
                        "Notion sync failed for voice_note_id=%s: %s",
                        voice_note.id,
                        exc,
                    )
                    notion_line = f"\n\n{notion_service.format_user_error(exc)}"
                except Exception as exc:
                    logger.warning(
                        "Notion sync failed for voice_note_id=%s: %s",
                        voice_note.id,
                        exc,
                    )
                    notion_line = (
                        "\n\n⚠️ Notion: не удалось сохранить.\n"
                        f"{notion_service.notion_access_instructions(compact=True)}"
                    )

            report_text = telegram_service.format_report(analysis, transcript)
            report_text += notion_line
            await telegram_service.send_report(
                chat_id=chat_id,
                report_text=report_text,
                lead_id=lead.id,
                voice_note_id=voice_note.id,
                target_kommo_lead_id=target_kommo_lead_id,
                notion_action_id=notion_action_id,
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
                    (
                        "❌ <b>Обработка аудио остановлена</b>\n\n"
                        f"<code>{_user_error_text(exc)}</code>\n\n"
                        "Ошибка сохранена в /jobs. Проверьте OPENAI_API_KEY, DATABASE_URL "
                        "и что сервис перезапущен после обновления."
                    ),
                )
            except Exception:
                logger.exception("Could not notify user about failed audio processing")
        await _release_processing_lock(telegram_user_id, telegram_message_id)
        raise
