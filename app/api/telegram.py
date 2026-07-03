"""FastAPI router for the Telegram webhook and Kommo manager menu."""

from __future__ import annotations

import asyncio
import hmac
import html
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from celery.result import AsyncResult
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.celery_app import celery_app
from app.config import get_settings
from app.database import get_db
from app.services import (
    approval_service,
    calendar_event_builder,
    calendar_scheduling_service,
    calendar_service,
    command_router_service,
    crm_service,
    google_sheets_service,
    kommo_service,
    notion_service,
    telegram_service,
    telegram_state_service,
    unreviewed_leads_service,
)
from app.tasks.voice_note_tasks import process_voice_note, process_voice_note_async

router = APIRouter(prefix="/webhook", tags=["telegram"])
logger = logging.getLogger(__name__)
settings = get_settings()
BACKGROUND_TASKS: set[asyncio.Task[Any]] = set()

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


async def _claim_audio_message(user_id: int, message_id: int) -> bool:
    """Atomically claim a Telegram audio message so duplicate webhook deliveries are ignored."""
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    key = f"telegram:audio:queued:{user_id}:{message_id}"
    try:
        claimed = await redis.set(key, "1", nx=True, ex=24 * 60 * 60)
        return bool(claimed)
    except Exception as exc:
        logger.warning("Could not claim Telegram audio message in Redis: %s", exc)
        return True
    finally:
        await redis.aclose()


async def _release_audio_claim(user_id: int, message_id: int) -> None:
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    key = f"telegram:audio:queued:{user_id}:{message_id}"
    try:
        await redis.delete(key)
    except Exception as exc:
        logger.warning("Could not release Telegram audio claim: %s", exc)
    finally:
        await redis.aclose()


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


def _spawn_background(coro: Any) -> None:
    task = asyncio.create_task(coro)
    BACKGROUND_TASKS.add(task)

    def _done(completed: asyncio.Task[Any]) -> None:
        BACKGROUND_TASKS.discard(completed)
        try:
            exc = completed.exception()
        except asyncio.CancelledError:
            return
        if exc:
            logger.exception("Background Telegram task failed", exc_info=exc)

    task.add_done_callback(_done)


def _manager_tz() -> ZoneInfo:
    try:
        return ZoneInfo(settings.manager_timezone)
    except Exception:
        logger.warning(
            "Invalid MANAGER_TIMEZONE=%s, falling back to UTC",
            settings.manager_timezone,
        )
        return ZoneInfo("UTC")


def _parse_manager_datetime(value: str) -> datetime:
    raw = " ".join(value.strip().split())
    if not raw:
        raise ValueError("Дата и время не указаны.")

    tz = _manager_tz()
    now = datetime.now(tz=tz)
    lowered = raw.casefold().replace("ё", "е")

    for prefix, days in (("сегодня ", 0), ("завтра ", 1)):
        if lowered.startswith(prefix):
            time_part = raw[len(prefix) :].strip()
            try:
                parsed_time = datetime.strptime(time_part, "%H:%M").time()
            except ValueError as exc:
                raise ValueError("Используйте формат: завтра 10:00") from exc
            target = (now + timedelta(days=days)).date()
            result = datetime.combine(target, parsed_time, tzinfo=tz)
            if result <= now:
                raise ValueError("Дата и время должны быть в будущем.")
            return result

    formats = ("%d.%m.%Y %H:%M", "%Y-%m-%d %H:%M", "%d.%m %H:%M")
    for fmt in formats:
        try:
            parsed = datetime.strptime(raw, fmt)
        except ValueError:
            continue
        if fmt == "%d.%m %H:%M":
            parsed = parsed.replace(year=now.year)
            candidate = parsed.replace(tzinfo=tz)
            if candidate <= now:
                candidate = candidate.replace(year=now.year + 1)
            parsed = candidate.replace(tzinfo=None)
        result = parsed.replace(tzinfo=tz)
        if result <= now:
            raise ValueError("Дата и время должны быть в будущем.")
        return result

    raise ValueError(
        "Не удалось распознать дату. Пример: 30.06.2026 10:00, 2026-06-30 10:00 или завтра 10:00."
    )


def _format_manager_datetime(value: datetime) -> str:
    return (
        value.astimezone(_manager_tz()).strftime("%d.%m.%Y %H:%M")
        + f" ({settings.manager_timezone})"
    )


def _quick_manager_datetime(choice: str) -> datetime:
    tz = _manager_tz()
    now = datetime.now(tz=tz)
    mapping = {
        "today17": (0, 17, 0),
        "tomorrow10": (1, 10, 0),
        "tomorrow15": (1, 15, 0),
        "tomorrow18": (1, 18, 0),
    }
    if choice not in mapping:
        raise ValueError("Неизвестный быстрый срок.")
    days, hour, minute = mapping[choice]
    target = (now + timedelta(days=days)).replace(
        hour=hour, minute=minute, second=0, microsecond=0
    )
    if target <= now:
        target += timedelta(days=1)
    return target


async def _audio_queue_watchdog(
    *,
    task_id: str,
    process_kwargs: dict[str, Any],
    chat_id: int,
) -> None:
    delay = max(15, min(int(settings.audio_queue_fallback_seconds), 300))
    await asyncio.sleep(delay)

    try:
        state = await asyncio.to_thread(
            lambda: AsyncResult(task_id, app=celery_app).state
        )
    except Exception as exc:
        logger.warning("Could not read Celery task state %s: %s", task_id, exc)
        state = "PENDING"

    if state != "PENDING":
        logger.info("Audio task %s was picked up by Celery, state=%s", task_id, state)
        return

    logger.warning(
        "Audio task %s stayed PENDING for %ss; starting safe in-process fallback",
        task_id,
        delay,
    )
    await telegram_service.send_message(
        chat_id,
        "⚠️ Celery worker не забрал аудио вовремя. Запускаю резервную обработку на основном сервере.",
    )
    await process_voice_note_async(**process_kwargs)


async def _handle_kommo_test(chat_id: int, user_id: int, db: AsyncSession) -> None:
    from app.models.integration_check import IntegrationCheck

    if not _is_allowed_user(user_id):
        await telegram_service.send_message(chat_id, "Доступ запрещён.")
        return

    await telegram_service.send_message(chat_id, "🔄 Проверяю связь с Kommo...")
    result = await kommo_service.test_connection()
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
            f"Ошибка: {html.escape(result.get('error', 'Неизвестная ошибка'))}\n\n"
            "Проверьте Railway Variables:\n"
            "• <code>KOMMO_BASE_URL</code>\n"
            "• <code>KOMMO_ACCESS_TOKEN</code>"
        )
    await telegram_service.send_message(chat_id, message)


async def _handle_text_command(
    chat_id: int,
    user_id: int,
    text: str,
    db: AsyncSession,
) -> bool:
    if not text or text.startswith("/"):
        return False
    context = await crm_service.get_user_command_context(db, telegram_user_id=user_id)
    state = await telegram_state_service.get_state(user_id)
    if state:
        if state.get("kommo_lead_id"):
            context["kommo_lead_id"] = state.get("kommo_lead_id")
        if state.get("notion_lead_page_id"):
            context["notion_lead_page_id"] = state.get("notion_lead_page_id")
    plan = await command_router_service.classify_message(text, context=context)
    if plan.intent == "analyze_conversation":
        return False
    reply = await command_router_service.execute_plan(
        db,
        plan=plan,
        chat_id=chat_id,
        telegram_user_id=user_id,
        context=context,
    )
    if reply is not None:
        if reply:
            await telegram_service.send_message(chat_id, reply)
        return True
    return plan.intent != "analyze_conversation"


async def _handle_morning_digest(chat_id: int) -> None:
    if not notion_service.is_configured():
        await telegram_service.send_message(
            chat_id,
            "Notion не настроен. Добавьте NOTION_API_TOKEN и database IDs в Railway.",
        )
        return
    await telegram_service.send_message(chat_id, await notion_service.get_morning_digest())


async def _handle_calendar_test(
    chat_id: int, user_id: int, *, include_write_probe: bool = False
) -> None:
    if not _is_allowed_user(user_id):
        await telegram_service.send_message(chat_id, "Доступ запрещён.")
        return

    provider = (settings.calendar_provider or "google").strip().lower()
    await telegram_service.send_message(
        chat_id, f"🔄 Проверяю {html.escape(calendar_service.provider_label())}…"
    )
    try:
        if provider == "icloud":
            details = await asyncio.to_thread(calendar_service.test_icloud_connection)
            manager_tz = _manager_tz()
            tomorrow = datetime.now(tz=manager_tz) + timedelta(days=1)
            test_start = tomorrow.replace(hour=10, minute=0, second=0, microsecond=0)
            test_result = await asyncio.to_thread(
                calendar_service.create_event_with_fallback,
                "Тест BBS Bot",
                "Проверка напоминания из Telegram",
                test_start.isoformat(),
                15,
            )
            if test_result["success"]:
                await telegram_service.send_message(
                    chat_id,
                    (
                        f"✅ <b>{html.escape(calendar_service.provider_label())} работает</b>\n\n"
                        f"{html.escape(details)}\n\n"
                        f"Тестовое событие создано на <b>{test_start.strftime('%d.%m.%Y %H:%M')}</b>."
                    ),
                )
                return
            await telegram_service.send_message(
                chat_id,
                (
                    "⚠️ <b>Подключение есть, но событие не записалось</b>\n\n"
                    f"{html.escape(details)}\n\n"
                    f"Ошибка: {html.escape(str(test_result.get('error') or '—'))}"
                ),
            )
            return

        report = await asyncio.to_thread(
            calendar_service.test_google_connection,
            include_write_probe=include_write_probe,
        )
        await telegram_service.send_message(chat_id, report)
    except calendar_service.CalendarIntegrationError as exc:
        await telegram_service.send_message(
            chat_id,
            f"❌ <b>Календарь не настроен</b>\n\n{html.escape(str(exc))}",
        )


def _calendar_preview_payload(state: dict[str, Any]) -> dict[str, Any]:
    event_type = str(state.get("event_type") or "call")
    return {
        "lead_name": state.get("lead_name"),
        "event_label": calendar_event_builder.EVENT_TYPE_LABELS.get(
            event_type, state.get("calendar_title")
        ),
        "date_display": calendar_event_builder.format_date_ru(
            datetime.fromisoformat(str(state.get("start_iso")))
        )
        if state.get("start_iso")
        else "—",
        "time_display": str(state.get("start_display") or "—"),
        "timezone": settings.google_calendar_timezone or settings.manager_timezone,
        "duration_label": f"{int(state.get('duration_minutes') or 30)} минут",
        "reminder_label": calendar_event_builder.format_reminder_label(
            int(state.get("reminder_minutes") or 0)
        ),
        "needs_calendar": event_type in calendar_event_builder.CALENDAR_EVENT_TYPES,
        "needs_kommo_task": event_type
        in calendar_event_builder.CALENDAR_EVENT_TYPES
        or event_type in calendar_event_builder.TASK_ONLY_EVENT_TYPES,
    }


async def _show_calendar_preview_from_state(
    chat_id: int, user_id: int, state: dict[str, Any]
) -> None:
    lead_id = int(state.get("kommo_lead_id") or 0)
    return_page = int(state.get("return_page") or 1)
    await telegram_state_service.set_state(
        user_id,
        {**state, "mode": "pending_calendar_confirmation"},
        ttl_seconds=settings.telegram_state_ttl_minutes * 60,
    )
    await telegram_service.send_calendar_preview(
        chat_id,
        lead_id=lead_id,
        return_page=return_page,
        preview=_calendar_preview_payload(state),
    )


async def _build_calendar_draft_from_state(
    state: dict[str, Any],
) -> calendar_event_builder.ScheduledEventDraft:
    lead_id = int(state.get("kommo_lead_id") or 0)
    details = await kommo_service.get_lead_details(lead_id)
    start_at = datetime.fromisoformat(str(state.get("start_iso")))
    return calendar_event_builder.draft_from_lead_details(
        event_type=str(state.get("event_type") or "call"),
        lead_details=details,
        start_at=start_at,
        duration_minutes=int(state.get("duration_minutes") or 30),
        reminder_minutes=int(state.get("reminder_minutes") or 30),
        custom_title=str(state.get("calendar_title") or "") or None,
    )


async def _deliver_calendar_result(
    chat_id: int,
    *,
    title: str,
    description: str,
    start_iso: str,
    duration_minutes: int,
    lead_name: str | None = None,
    start_display: str | None = None,
) -> None:
    await telegram_service.send_calendar_result(
        chat_id,
        title=title,
        start_iso=start_iso,
        duration_minutes=duration_minutes,
        description=description,
        start_display=start_display,
        lead_name=lead_name,
    )


async def _show_audio_jobs(
    chat_id: int,
    user_id: int,
    db: AsyncSession,
) -> None:
    jobs = await crm_service.recent_audio_jobs(db, telegram_user_id=user_id, limit=8)
    await telegram_service.send_message(
        chat_id,
        telegram_service.format_audio_jobs(jobs),
        reply_markup={
            "inline_keyboard": [
                [{"text": "🔄 Обновить", "callback_data": "menu:jobs"}],
                [{"text": "🏠 Главное меню", "callback_data": "menu:home"}],
            ]
        },
    )


async def _show_creation_preview(
    chat_id: int,
    user_id: int,
    db: AsyncSession | None,
    *,
    lead_id: int,
    voice_note_id: int,
    draft: dict[str, Any] | None = None,
) -> None:
    if draft is None:
        if db is None:
            raise ValueError("Database session is required for a new preview")
        draft = await approval_service.build_kommo_creation_draft(
            db, lead_id=lead_id, voice_note_id=voice_note_id
        )
    await telegram_state_service.set_state(
        user_id,
        {
            "mode": "kommo_create_preview",
            "chat_id": chat_id,
            "local_lead_id": lead_id,
            "voice_note_id": voice_note_id,
            "draft": draft,
        },
        ttl_seconds=settings.telegram_state_ttl_minutes * 60,
    )
    await telegram_service.send_kommo_creation_preview(chat_id, draft)


async def _show_lead_page(chat_id: int, user_id: int, page: int = 1) -> None:
    if not _is_allowed_user(user_id):
        await telegram_service.send_message(chat_id, "Доступ запрещён.")
        return
    await telegram_service.send_message(chat_id, "🔄 Загружаю открытые сделки...")
    result = await kommo_service.get_open_leads_page(page=page)
    await telegram_service.send_lead_selection_menu(chat_id, result, page=page)


async def _show_lead_details(
    chat_id: int,
    lead_id: int,
    *,
    return_page: int = 1,
) -> None:
    details = await kommo_service.get_lead_details(lead_id)
    await telegram_service.send_lead_details(
        chat_id,
        details,
        return_page=return_page,
    )


def _lead_edit_snapshot(details: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": details.get("name"),
        "price": details.get("price"),
        "status_id": details.get("status_id"),
        "status_name": details.get("status_name"),
        "pipeline_id": details.get("pipeline_id"),
    }


async def _show_lead_edit_preview(
    chat_id: int,
    user_id: int,
    lead_id: int,
    *,
    return_page: int = 1,
    draft: dict[str, Any] | None = None,
    original: dict[str, Any] | None = None,
) -> None:
    details = await kommo_service.get_lead_details(lead_id)
    snapshot = _lead_edit_snapshot(details)
    original = original or snapshot
    draft = draft or dict(snapshot)
    await telegram_state_service.set_state(
        user_id,
        {
            "mode": "kommo_lead_edit_preview",
            "chat_id": chat_id,
            "kommo_lead_id": lead_id,
            "return_page": return_page,
            "original": original,
            "draft": draft,
        },
        ttl_seconds=settings.telegram_state_ttl_minutes * 60,
    )
    await telegram_service.send_lead_edit_preview(
        chat_id,
        lead_id=lead_id,
        draft=draft,
        original=original,
        return_page=return_page,
    )


async def _show_unreviewed_page(chat_id: int, user_id: int, page: int = 1) -> None:
    if not _is_allowed_user(user_id):
        await telegram_service.send_message(chat_id, "Доступ запрещён.")
        return
    await telegram_service.send_message(
        chat_id, "🔄 Загружаю неразобранные сделки из Kommo…"
    )
    try:
        result = await kommo_service.get_unreviewed_leads_page(page=page)
    except Exception as exc:
        await telegram_service.send_message(
            chat_id,
            f"❌ <b>Не удалось загрузить сделки</b>\n\n{html.escape(str(exc)[:500])}",
        )
        return
    await telegram_service.send_unreviewed_lead_selection_menu(
        chat_id, result, page=page
    )


async def _show_unreviewed_lead_card(
    chat_id: int,
    lead_id: int,
    *,
    return_page: int = 1,
) -> None:
    details = await kommo_service.get_lead_details(lead_id)
    await telegram_service.send_unreviewed_lead_card(
        chat_id, details, return_page=return_page
    )


async def _store_unreviewed_preview_state(
    user_id: int,
    chat_id: int,
    *,
    lead_id: int,
    return_page: int,
    current_name: str,
    preview: dict[str, Any],
) -> None:
    await telegram_state_service.set_state(
        user_id,
        {
            "mode": "unreviewed_preview",
            "chat_id": chat_id,
            "kommo_lead_id": lead_id,
            "return_page": return_page,
            "lead_name": current_name,
            "preview": preview,
        },
        ttl_seconds=settings.telegram_state_ttl_minutes * 60,
    )


async def _show_unreviewed_preview(
    chat_id: int,
    user_id: int,
    *,
    lead_id: int,
    return_page: int,
    current_name: str,
    preview: dict[str, Any],
) -> None:
    await _store_unreviewed_preview_state(
        user_id,
        chat_id,
        lead_id=lead_id,
        return_page=return_page,
        current_name=current_name,
        preview=preview,
    )
    await telegram_service.send_unreviewed_rename_preview(
        chat_id,
        lead_id=lead_id,
        return_page=return_page,
        current_name=current_name,
        preview=preview,
    )


def _candidate_display(candidate: Any) -> dict[str, Any]:
    row = candidate.row
    return {
        "row_number": row.row_number,
        "lead_number": row.lead_number,
        "product": row.product,
        "phone": row.phone,
        "client_name": row.client_name,
        "company": row.company,
    }


async def _start_unreviewed_matching(
    chat_id: int,
    user_id: int,
    lead_id: int,
    return_page: int,
    *,
    force_refresh: bool = False,
) -> None:
    if not google_sheets_service.is_configured():
        await telegram_service.send_message(
            chat_id,
            (
                "❌ <b>Google Sheets не настроен</b>\n\n"
                "Задайте GOOGLE_SHEETS_SPREADSHEET_ID, GOOGLE_SHEETS_WORKSHEET_NAME "
                "и GOOGLE_SHEETS_SERVICE_ACCOUNT_JSON в Railway Variables."
            ),
        )
        return
    await telegram_service.send_message(
        chat_id, "🔄 Ищу строку в таблице и готовлю название…"
    )
    try:
        details = await kommo_service.get_lead_details(lead_id)
        match = await unreviewed_leads_service.match_lead_from_sheets(
            details, force_refresh=force_refresh
        )
    except google_sheets_service.GoogleSheetsError as exc:
        await telegram_service.send_message(
            chat_id, f"❌ <b>Ошибка Google Sheets</b>\n\n{html.escape(str(exc)[:500])}"
        )
        return
    except Exception as exc:
        await telegram_service.send_message(
            chat_id,
            f"❌ <b>Не удалось сопоставить строку</b>\n\n{html.escape(str(exc)[:500])}",
        )
        return

    if match.is_empty:
        await telegram_service.send_unreviewed_no_match(
            chat_id, lead_id=lead_id, return_page=return_page
        )
        return
    if len(match.candidates) > 1:
        await telegram_service.send_unreviewed_match_candidates(
            chat_id,
            lead_id=lead_id,
            return_page=return_page,
            candidates=[_candidate_display(item) for item in match.candidates],
        )
        return

    candidate = match.single
    if not candidate:
        await telegram_service.send_unreviewed_no_match(
            chat_id, lead_id=lead_id, return_page=return_page
        )
        return
    try:
        preview = await unreviewed_leads_service.build_preview_from_row(
            candidate.row, candidate=candidate
        )
    except ValueError as exc:
        await telegram_service.send_message(chat_id, f"❌ {html.escape(str(exc))}")
        return
    await _show_unreviewed_preview(
        chat_id,
        user_id,
        lead_id=lead_id,
        return_page=return_page,
        current_name=str(details.get("name") or ""),
        preview=preview,
    )


async def _confirm_unreviewed_rename(
    chat_id: int,
    user_id: int,
    db: AsyncSession,
    *,
    lead_id: int,
    return_page: int,
    allow_replace: bool = False,
) -> None:
    state = await telegram_state_service.get_state(user_id)
    if not state or state.get("mode") != "unreviewed_preview":
        await telegram_service.send_message(
            chat_id, "⚠️ Сессия устарела. Откройте сделку заново."
        )
        return
    if int(state.get("kommo_lead_id") or 0) != lead_id:
        await telegram_service.send_message(chat_id, "❌ Сделка в сессии не совпадает.")
        return
    preview = dict(state.get("preview") or {})
    current_name = str(state.get("lead_name") or "")
    try:
        details = await kommo_service.get_lead_details(lead_id)
        current_name = str(details.get("name") or current_name)
    except Exception:
        pass

    existing_number, _ = unreviewed_leads_service.parse_internal_lead_name(current_name)
    new_number = str(preview.get("spreadsheet_lead_number") or "").strip()
    if existing_number and existing_number != new_number and not allow_replace:
        await telegram_service.send_unreviewed_replace_warning(
            chat_id,
            lead_id=lead_id,
            return_page=return_page,
            current_name=current_name,
            preview=preview,
        )
        return

    await telegram_service.send_message(chat_id, "⏳ Обновляю название в Kommo…")
    try:
        result = await unreviewed_leads_service.apply_lead_rename(
            db,
            lead_id=lead_id,
            current_name=current_name,
            preview=preview,
            telegram_user_id=user_id,
            allow_replace=allow_replace,
        )
    except ValueError as exc:
        if str(exc) == "replace_required":
            await telegram_service.send_unreviewed_replace_warning(
                chat_id,
                lead_id=lead_id,
                return_page=return_page,
                current_name=current_name,
                preview=preview,
            )
            return
        await telegram_service.send_message(
            chat_id, f"❌ {html.escape(str(exc)[:500])}"
        )
        return
    except kommo_service.KommoAPIError as exc:
        await telegram_service.send_message(
            chat_id,
            f"❌ <b>Ошибка Kommo</b>\n\n{html.escape(str(exc)[:500])}",
        )
        return
    except Exception as exc:
        await telegram_service.send_message(
            chat_id,
            f"❌ <b>Не удалось обновить сделку</b>\n\n{html.escape(str(exc)[:500])}",
        )
        return

    await telegram_state_service.clear_state(user_id)
    await telegram_service.send_unreviewed_success(
        chat_id,
        lead_id=lead_id,
        return_page=return_page,
        result=result,
    )


async def _prompt_search(chat_id: int, user_id: int) -> None:
    await telegram_state_service.set_state(
        user_id,
        {"mode": "awaiting_lead_search", "chat_id": chat_id},
        ttl_seconds=settings.telegram_state_ttl_minutes * 60,
    )
    await telegram_service.send_message(
        chat_id,
        (
            "🔎 <b>Поиск сделки</b>\n\n"
            "Отправьте ID сделки или часть названия клиента/товара."
        ),
        reply_markup={
            "inline_keyboard": [
                [{"text": "❌ Отмена", "callback_data": "state:cancel"}]
            ]
        },
    )


async def _prompt_text_note(
    chat_id: int,
    user_id: int,
    lead_id: int,
    return_page: int,
) -> None:
    details = await kommo_service.get_lead_details(lead_id)
    await telegram_state_service.set_state(
        user_id,
        {
            "mode": "awaiting_text_note",
            "chat_id": chat_id,
            "kommo_lead_id": lead_id,
            "lead_name": details.get("name"),
            "return_page": return_page,
        },
        ttl_seconds=settings.telegram_state_ttl_minutes * 60,
    )
    await telegram_service.send_message(
        chat_id,
        (
            "📝 <b>Новое примечание</b>\n\n"
            f"Сделка: <b>{html.escape(str(details.get('name') or '—'))}</b>\n"
            f"ID: <code>{lead_id}</code>\n\n"
            "Отправьте текст примечания одним сообщением. Перед записью в Kommo бот покажет подтверждение."
        ),
        reply_markup={
            "inline_keyboard": [
                [{"text": "❌ Отмена", "callback_data": "state:cancel"}]
            ]
        },
    )


async def _prompt_followup_audio(
    chat_id: int,
    user_id: int,
    lead_id: int,
    return_page: int,
) -> None:
    details = await kommo_service.get_lead_details(lead_id)
    await telegram_state_service.set_state(
        user_id,
        {
            "mode": "awaiting_audio_for_lead",
            "chat_id": chat_id,
            "kommo_lead_id": lead_id,
            "lead_name": details.get("name"),
            "return_page": return_page,
        },
        ttl_seconds=settings.telegram_state_ttl_minutes * 60,
    )
    await telegram_service.send_message(
        chat_id,
        (
            "🎙 <b>Второй разговор по существующей сделке</b>\n\n"
            f"Сделка: <b>{html.escape(str(details.get('name') or '—'))}</b>\n"
            f"ID: <code>{lead_id}</code>\n\n"
            "Теперь отправьте голосовое сообщение или файл .m4a/.mp3/.wav/.mp4/.webm. "
            "После анализа появится кнопка подтверждения. Новая сделка в Kommo создана не будет."
        ),
        reply_markup={
            "inline_keyboard": [
                [{"text": "❌ Отмена", "callback_data": "state:cancel"}]
            ]
        },
    )


async def _prompt_kommo_task(
    chat_id: int,
    user_id: int,
    lead_id: int,
    return_page: int,
) -> None:
    details = await kommo_service.get_lead_details(lead_id)
    await telegram_state_service.set_state(
        user_id,
        {
            "mode": "awaiting_task_text",
            "chat_id": chat_id,
            "kommo_lead_id": lead_id,
            "lead_name": details.get("name"),
            "responsible_user_id": details.get("responsible_user_id"),
            "return_page": return_page,
        },
        ttl_seconds=settings.telegram_state_ttl_minutes * 60,
    )
    await telegram_service.send_message(
        chat_id,
        (
            "✅ <b>Шаг 1 из 2 · Новая задача</b>\n\n"
            f"Сделка: <b>{html.escape(str(details.get('name') or '—'))}</b>\n\n"
            "Напишите, что нужно сделать. Например: "
            "<i>Позвонить клиенту и уточнить количество</i>."
        ),
        reply_markup={
            "inline_keyboard": [
                [{"text": "❌ Отмена", "callback_data": "state:cancel"}]
            ]
        },
    )


async def _prompt_calendar_event(
    chat_id: int,
    user_id: int,
    lead_id: int,
    return_page: int,
) -> None:
    details = await kommo_service.get_lead_details(lead_id)
    await telegram_state_service.set_state(
        user_id,
        {
            "mode": "awaiting_calendar_event_type",
            "chat_id": chat_id,
            "kommo_lead_id": lead_id,
            "lead_name": details.get("name"),
            "lead_url": details.get("url"),
            "return_page": return_page,
        },
        ttl_seconds=settings.telegram_state_ttl_minutes * 60,
    )
    await telegram_service.send_calendar_event_type_picker(
        chat_id,
        lead_id=lead_id,
        lead_name=str(details.get("name") or "—"),
        return_page=return_page,
    )


async def _handle_manager_callback(
    *,
    callback_data: str,
    chat_id: int,
    user_id: int,
    db: AsyncSession,
) -> bool:
    """Handle menu/lead/note callbacks. Return False for approval callbacks."""
    if callback_data == "noop":
        return True

    if callback_data == "menu:home":
        await telegram_state_service.clear_state(user_id)
        await telegram_service.send_main_menu(chat_id)
        return True
    if callback_data == "menu:test":
        await _handle_kommo_test(chat_id, user_id, db)
        return True
    if callback_data == "menu:calendar":
        await _handle_calendar_test(chat_id, user_id)
        return True
    if callback_data == "menu:jobs":
        await _show_audio_jobs(chat_id, user_id, db)
        return True
    if callback_data == "menu:search":
        await _prompt_search(chat_id, user_id)
        return True
    if callback_data == "menu:new":
        await telegram_state_service.set_state(
            user_id,
            {"mode": "awaiting_audio_for_new_lead", "chat_id": chat_id},
            ttl_seconds=settings.telegram_state_ttl_minutes * 60,
        )
        await telegram_service.send_message(
            chat_id,
            (
                "🎙 <b>Новый разговор с клиентом</b>\n\n"
                "Отправьте голосовое или аудиофайл с записью разговора. "
                "Бот сделает анализ на русском и подготовит карточку нового лида.\n\n"
                "<i>Обычные голосовые без этой кнопки — только ваши команды боту "
                "(календарь, напоминания, поиск сделок).</i>"
            ),
            reply_markup={
                "inline_keyboard": [
                    [{"text": "❌ Отмена", "callback_data": "state:cancel"}],
                    [{"text": "🏠 Меню", "callback_data": "menu:home"}],
                ]
            },
        )
        return True
    if callback_data == "menu:update":
        await telegram_service.send_message(
            chat_id,
            "Выберите сделку, которую нужно дополнить новым разговором или примечанием.",
        )
        await _show_lead_page(chat_id, user_id, 1)
        return True
    if callback_data.startswith("menu:leads:"):
        page = int(callback_data.rsplit(":", 1)[1])
        await _show_lead_page(chat_id, user_id, page)
        return True
    if callback_data.startswith("menu:unrev:"):
        page = int(callback_data.rsplit(":", 1)[1])
        await _show_unreviewed_page(chat_id, user_id, page)
        return True
    if callback_data.startswith("unrev:view:"):
        _, _, lead_id_raw, page_raw = callback_data.split(":", 3)
        await _show_unreviewed_lead_card(
            chat_id, int(lead_id_raw), return_page=int(page_raw)
        )
        return True
    if callback_data.startswith("unrev:add:"):
        _, _, lead_id_raw, page_raw = callback_data.split(":", 3)
        await _start_unreviewed_matching(
            chat_id, user_id, int(lead_id_raw), int(page_raw)
        )
        return True
    if callback_data.startswith("unrev:refresh:"):
        _, _, lead_id_raw, page_raw = callback_data.split(":", 3)
        google_sheets_service.clear_cache()
        await _start_unreviewed_matching(
            chat_id,
            user_id,
            int(lead_id_raw),
            int(page_raw),
            force_refresh=True,
        )
        return True
    if callback_data.startswith("unrev:manual:"):
        _, _, lead_id_raw, page_raw = callback_data.split(":", 3)
        lead_id = int(lead_id_raw)
        return_page = int(page_raw)
        await telegram_state_service.set_state(
            user_id,
            {
                "mode": "unreviewed_manual_entry",
                "chat_id": chat_id,
                "kommo_lead_id": lead_id,
                "return_page": return_page,
            },
            ttl_seconds=settings.telegram_state_ttl_minutes * 60,
        )
        await telegram_service.send_message(
            chat_id,
            (
                "🔎 <b>Ввод номера лида</b>\n\n"
                "Отправьте внутренний номер из колонки Y таблицы. "
                "Например: <code>110</code>"
            ),
            reply_markup={
                "inline_keyboard": [
                    [
                        {
                            "text": "⬅️ Назад",
                            "callback_data": f"unrev:view:{lead_id}:{return_page}",
                        }
                    ]
                ]
            },
        )
        return True
    if callback_data.startswith("unrev:pick:"):
        _, _, row_number_raw, lead_id_raw, page_raw = callback_data.split(":", 4)
        row_number = int(row_number_raw)
        lead_id = int(lead_id_raw)
        return_page = int(page_raw)
        row = google_sheets_service.get_row_by_number(row_number)
        if not row:
            await telegram_service.send_message(
                chat_id, "❌ Строка таблицы не найдена. Обновите кэш."
            )
            return True
        try:
            preview = await unreviewed_leads_service.build_preview_from_row(row)
            details = await kommo_service.get_lead_details(lead_id)
        except ValueError as exc:
            await telegram_service.send_message(chat_id, f"❌ {html.escape(str(exc))}")
            return True
        await _show_unreviewed_preview(
            chat_id,
            user_id,
            lead_id=lead_id,
            return_page=return_page,
            current_name=str(details.get("name") or ""),
            preview=preview,
        )
        return True
    if callback_data.startswith("unrev:confirm:"):
        _, _, lead_id_raw, page_raw = callback_data.split(":", 3)
        await _confirm_unreviewed_rename(
            chat_id,
            user_id,
            db,
            lead_id=int(lead_id_raw),
            return_page=int(page_raw),
        )
        return True
    if callback_data.startswith("unrev:replace:"):
        _, _, lead_id_raw, page_raw = callback_data.split(":", 3)
        await _confirm_unreviewed_rename(
            chat_id,
            user_id,
            db,
            lead_id=int(lead_id_raw),
            return_page=int(page_raw),
            allow_replace=True,
        )
        return True
    if callback_data.startswith("unrev:editname:"):
        _, _, lead_id_raw, page_raw = callback_data.split(":", 3)
        lead_id = int(lead_id_raw)
        return_page = int(page_raw)
        state = await telegram_state_service.get_state(user_id)
        if not state or state.get("mode") != "unreviewed_preview":
            await telegram_service.send_message(chat_id, "⚠️ Сессия устарела.")
            return True
        await telegram_state_service.set_state(
            user_id,
            {**state, "mode": "unreviewed_edit_name"},
            ttl_seconds=settings.telegram_state_ttl_minutes * 60,
        )
        await telegram_service.send_message(
            chat_id,
            (
                "✏️ <b>Изменить название</b>\n\n"
                "Отправьте полное новое название сделки.\n"
                f"Сейчас: <b>{html.escape(str((state.get('preview') or {}).get('proposed_name') or '—'))}</b>"
            ),
            reply_markup={
                "inline_keyboard": [
                    [
                        {
                            "text": "⬅️ Назад",
                            "callback_data": f"unrev:preview:{lead_id}:{return_page}",
                        }
                    ]
                ]
            },
        )
        return True
    if callback_data.startswith("unrev:preview:"):
        _, _, lead_id_raw, page_raw = callback_data.split(":", 3)
        lead_id = int(lead_id_raw)
        return_page = int(page_raw)
        state = await telegram_state_service.get_state(user_id)
        if not state or int(state.get("kommo_lead_id") or 0) != lead_id:
            await telegram_service.send_message(chat_id, "⚠️ Сессия устарела.")
            return True
        preview = dict(state.get("preview") or {})
        current_name = str(state.get("lead_name") or "")
        await _show_unreviewed_preview(
            chat_id,
            user_id,
            lead_id=lead_id,
            return_page=return_page,
            current_name=current_name,
            preview=preview,
        )
        return True
    if callback_data.startswith("unrev:repick:"):
        _, _, lead_id_raw, page_raw = callback_data.split(":", 3)
        await _start_unreviewed_matching(
            chat_id, user_id, int(lead_id_raw), int(page_raw), force_refresh=True
        )
        return True
    if callback_data.startswith("unrev:next:"):
        page = int(callback_data.rsplit(":", 1)[1])
        await _show_unreviewed_page(chat_id, user_id, page)
        return True
    if callback_data.startswith("lead:view:"):
        _, _, lead_id_raw, page_raw = callback_data.split(":", 3)
        await _show_lead_details(chat_id, int(lead_id_raw), return_page=int(page_raw))
        return True
    if callback_data.startswith("lead:text:"):
        _, _, lead_id_raw, page_raw = callback_data.split(":", 3)
        await _prompt_text_note(chat_id, user_id, int(lead_id_raw), int(page_raw))
        return True
    if callback_data.startswith("lead:audio:"):
        _, _, lead_id_raw, page_raw = callback_data.split(":", 3)
        await _prompt_followup_audio(chat_id, user_id, int(lead_id_raw), int(page_raw))
        return True
    if callback_data.startswith("lead:task:"):
        _, _, lead_id_raw, page_raw = callback_data.split(":", 3)
        await _prompt_kommo_task(chat_id, user_id, int(lead_id_raw), int(page_raw))
        return True
    if callback_data.startswith("lead:calendar:"):
        _, _, lead_id_raw, page_raw = callback_data.split(":", 3)
        await _prompt_calendar_event(chat_id, user_id, int(lead_id_raw), int(page_raw))
        return True
    if callback_data.startswith("lead:edit:"):
        _, _, lead_id_raw, page_raw = callback_data.split(":", 3)
        await _show_lead_edit_preview(
            chat_id,
            user_id,
            int(lead_id_raw),
            return_page=int(page_raw),
        )
        return True
    if callback_data.startswith("leadedit:cancel:"):
        _, _, lead_id_raw, page_raw = callback_data.split(":", 3)
        await telegram_state_service.clear_state(user_id)
        await telegram_service.send_message(chat_id, "❌ Редактирование отменено.")
        await _show_lead_details(chat_id, int(lead_id_raw), return_page=int(page_raw))
        return True
    if callback_data.startswith("leadedit:back:"):
        _, _, lead_id_raw, page_raw = callback_data.split(":", 3)
        state = await telegram_state_service.get_state(user_id)
        if not state or state.get("mode") not in {
            "kommo_lead_edit_preview",
            "kommo_lead_edit_field",
        }:
            await _show_lead_edit_preview(
                chat_id,
                user_id,
                int(lead_id_raw),
                return_page=int(page_raw),
            )
            return True
        await _show_lead_edit_preview(
            chat_id,
            user_id,
            int(lead_id_raw),
            return_page=int(page_raw),
            draft=dict(state.get("draft") or {}),
            original=dict(state.get("original") or {}),
        )
        return True
    if callback_data.startswith("leadedit:confirm:"):
        _, _, lead_id_raw, page_raw = callback_data.split(":", 3)
        lead_id = int(lead_id_raw)
        return_page = int(page_raw)
        state = await telegram_state_service.get_state(user_id)
        if not state or state.get("mode") != "kommo_lead_edit_preview":
            await telegram_service.send_message(
                chat_id, "⚠️ Редактирование устарело. Откройте сделку заново."
            )
            return True
        if int(state.get("kommo_lead_id") or 0) != lead_id:
            await telegram_service.send_message(
                chat_id, "❌ Сделка в подтверждении не совпадает."
            )
            return True
        original = dict(state.get("original") or {})
        draft = dict(state.get("draft") or {})
        update_kwargs: dict[str, Any] = {}
        if draft.get("name") != original.get("name"):
            update_kwargs["name"] = str(draft.get("name") or "")
        if draft.get("price") != original.get("price"):
            update_kwargs["price"] = int(draft.get("price") or 0)
        if draft.get("status_id") != original.get("status_id"):
            update_kwargs["status_id"] = int(draft.get("status_id") or 0)
        if not update_kwargs:
            await telegram_service.send_message(chat_id, "Нет изменений для сохранения.")
            return True
        await telegram_service.send_message(chat_id, "⏳ Обновляю сделку в Kommo…")
        try:
            result = await kommo_service.update_kommo_lead(lead_id, **update_kwargs)
        except Exception as exc:
            await telegram_service.send_message(
                chat_id,
                f"❌ <b>Не удалось обновить сделку</b>\n\n{html.escape(str(exc)[:500])}",
            )
            return True
        await telegram_state_service.clear_state(user_id)
        await telegram_service.send_message(
            chat_id,
            (
                "✅ <b>Сделка обновлена</b>\n\n"
                f"Название: <b>{html.escape(str(result.get('lead_name') or '—'))}</b>\n"
                f"Бюджет: {html.escape(str(result.get('price') or '—'))}\n"
                f"Этап: {html.escape(str(result.get('status_name') or '—'))}"
            ),
            reply_markup={
                "inline_keyboard": [
                    [{"text": "🔗 Открыть Kommo", "url": result.get("url")}]
                ]
            },
        )
        await _show_lead_details(chat_id, lead_id, return_page=return_page)
        return True
    if callback_data.startswith("leadedit:edit:"):
        _, _, field, lead_id_raw, page_raw = callback_data.split(":", 4)
        lead_id = int(lead_id_raw)
        return_page = int(page_raw)
        state = await telegram_state_service.get_state(user_id)
        if not state or state.get("mode") != "kommo_lead_edit_preview":
            await telegram_service.send_message(
                chat_id, "⚠️ Редактирование устарело. Откройте сделку заново."
            )
            return True
        if int(state.get("kommo_lead_id") or 0) != lead_id:
            await telegram_service.send_message(
                chat_id, "❌ Сделка в сессии не совпадает."
            )
            return True
        if field == "status":
            pipeline_id = (state.get("draft") or {}).get("pipeline_id")
            if not isinstance(pipeline_id, int):
                details = await kommo_service.get_lead_details(lead_id)
                pipeline_id = details.get("pipeline_id")
            statuses = await kommo_service.get_pipeline_statuses(int(pipeline_id))
            if not statuses:
                await telegram_service.send_message(
                    chat_id, "❌ Не удалось загрузить этапы воронки."
                )
                return True
            await telegram_state_service.set_state(
                user_id,
                {**state, "mode": "kommo_lead_edit_field", "edit_field": "status"},
                ttl_seconds=settings.telegram_state_ttl_minutes * 60,
            )
            await telegram_service.send_lead_status_picker(
                chat_id,
                lead_id=lead_id,
                statuses=statuses,
                return_page=return_page,
            )
            return True
        prompts = {
            "name": "Введите новое название сделки.",
            "price": (
                "Введите новый бюджет числом. Например: <code>15000</code>. "
                "Отправьте <code>0</code>, чтобы сбросить бюджет."
            ),
        }
        if field not in prompts:
            await telegram_service.send_message(chat_id, "❌ Неизвестное поле.")
            return True
        await telegram_state_service.set_state(
            user_id,
            {**state, "mode": "kommo_lead_edit_field", "edit_field": field},
            ttl_seconds=settings.telegram_state_ttl_minutes * 60,
        )
        await telegram_service.send_message(
            chat_id,
            f"✏️ <b>Редактирование</b>\n\n{prompts[field]}",
            reply_markup={
                "inline_keyboard": [
                    [
                        {
                            "text": "↩️ Назад",
                            "callback_data": f"leadedit:back:{lead_id}:{return_page}",
                        }
                    ]
                ]
            },
        )
        return True
    if callback_data.startswith("leadedit:status:"):
        _, _, status_id_raw, lead_id_raw, page_raw = callback_data.split(":", 4)
        lead_id = int(lead_id_raw)
        return_page = int(page_raw)
        status_id = int(status_id_raw)
        state = await telegram_state_service.get_state(user_id)
        if not state or state.get("mode") != "kommo_lead_edit_field":
            await telegram_service.send_message(
                chat_id, "⚠️ Выбор этапа устарел. Откройте редактирование заново."
            )
            return True
        draft = dict(state.get("draft") or {})
        pipeline_id = draft.get("pipeline_id")
        statuses = await kommo_service.get_pipeline_statuses(int(pipeline_id))
        status_name = next(
            (item.get("name") for item in statuses if item.get("id") == status_id),
            f"Этап {status_id}",
        )
        draft["status_id"] = status_id
        draft["status_name"] = status_name
        await _show_lead_edit_preview(
            chat_id,
            user_id,
            lead_id,
            return_page=return_page,
            draft=draft,
            original=dict(state.get("original") or {}),
        )
        return True
    if callback_data.startswith("leadcreate:preview:"):
        _, _, lead_id_raw, voice_note_id_raw = callback_data.split(":", 3)
        await _show_creation_preview(
            chat_id,
            user_id,
            db,
            lead_id=int(lead_id_raw),
            voice_note_id=int(voice_note_id_raw),
        )
        return True
    if callback_data.startswith("leadcreate:edit:"):
        field = callback_data.rsplit(":", 1)[1]
        state = await telegram_state_service.get_state(user_id)
        if not state or state.get("mode") != "kommo_create_preview":
            await telegram_service.send_message(
                chat_id, "⚠️ Предпросмотр устарел. Подготовьте лид заново."
            )
            return True
        prompts = {
            "number": "Введите внутренний номер лида. Например: <code>174</code>. Отправьте <code>-</code>, чтобы убрать номер.",
            "name": "Введите полное название сделки. Например: <code>174 Лазерная резка металла</code>.",
            "client": "Введите имя клиента. Отправьте <code>-</code>, если имя неизвестно.",
            "product": "Кратко укажите, что ищет клиент.",
            "budget": "Введите бюджет или ориентир. Отправьте <code>-</code>, если не указан.",
            "city": "Введите город доставки. Отправьте <code>-</code>, если не указан.",
        }
        if field not in prompts:
            await telegram_service.send_message(chat_id, "❌ Неизвестное поле.")
            return True
        await telegram_state_service.set_state(
            user_id,
            {**state, "mode": "kommo_create_edit", "edit_field": field},
            ttl_seconds=settings.telegram_state_ttl_minutes * 60,
        )
        await telegram_service.send_message(
            chat_id,
            f"✏️ <b>Редактирование</b>\n\n{prompts[field]}",
            reply_markup={
                "inline_keyboard": [
                    [{"text": "↩️ К предпросмотру", "callback_data": "leadcreate:back"}]
                ]
            },
        )
        return True
    if callback_data == "leadcreate:back":
        state = await telegram_state_service.get_state(user_id)
        if not state or not state.get("draft"):
            await telegram_service.send_message(chat_id, "⚠️ Предпросмотр устарел.")
            return True
        await _show_creation_preview(
            chat_id,
            user_id,
            db,
            lead_id=int(state["local_lead_id"]),
            voice_note_id=int(state["voice_note_id"]),
            draft=dict(state["draft"]),
        )
        return True
    if callback_data == "leadcreate:cancel":
        await telegram_state_service.clear_state(user_id)
        await telegram_service.send_message(
            chat_id, "❌ Создание лида отменено. Данные в Kommo не изменялись."
        )
        await telegram_service.send_main_menu(chat_id)
        return True
    if callback_data == "leadcreate:confirm":
        state = await telegram_state_service.get_state(user_id)
        if not state or state.get("mode") != "kommo_create_preview":
            await telegram_service.send_message(
                chat_id, "⚠️ Подтверждение устарело. Подготовьте лид заново."
            )
            return True
        await telegram_service.send_message(chat_id, "⏳ Создаю лид в Kommo…")
        result = await approval_service.execute_kommo_create_from_draft(
            db,
            lead_id=int(state["local_lead_id"]),
            voice_note_id=int(state["voice_note_id"]),
            draft=dict(state["draft"]),
            telegram_user_id=user_id,
        )
        await telegram_state_service.clear_state(user_id)
        await telegram_service.send_message(chat_id, result)
        await telegram_service.send_main_menu(chat_id)
        return True

    if callback_data.startswith("taskdate:"):
        choice = callback_data.split(":", 1)[1]
        state = await telegram_state_service.get_state(user_id)
        if not state or state.get("mode") != "awaiting_task_date":
            await telegram_service.send_message(chat_id, "⚠️ Мастер задачи устарел.")
            return True
        if choice == "custom":
            await telegram_state_service.set_state(
                user_id,
                {**state, "mode": "awaiting_task_custom_date"},
                ttl_seconds=settings.telegram_state_ttl_minutes * 60,
            )
            await telegram_service.send_message(
                chat_id,
                "🕒 Введите дату и время. Например: <code>завтра 10:00</code> или <code>30.06.2026 15:30</code>.",
            )
            return True
        due_dt = _quick_manager_datetime(choice)
        due_display = _format_manager_datetime(due_dt)
        await telegram_state_service.set_state(
            user_id,
            {
                **state,
                "mode": "pending_task_confirmation",
                "complete_till": int(due_dt.astimezone(timezone.utc).timestamp()),
                "due_display": due_display,
            },
            ttl_seconds=settings.telegram_state_ttl_minutes * 60,
        )
        await telegram_service.send_task_confirmation(
            chat_id,
            lead_id=int(state["kommo_lead_id"]),
            lead_name=str(state.get("lead_name") or "—"),
            task_text=str(state.get("task_text") or ""),
            due_display=due_display,
            return_page=int(state.get("return_page") or 1),
        )
        return True

    if callback_data.startswith("calevt:"):
        _, event_type, lead_id_raw, page_raw = callback_data.split(":", 3)
        lead_id = int(lead_id_raw)
        return_page = int(page_raw)
        state = await telegram_state_service.get_state(user_id) or {}
        title = calendar_event_builder.build_event_title(
            event_type, str(state.get("lead_name") or "")
        )
        updated = {
            **state,
            "mode": "awaiting_calendar_date",
            "chat_id": chat_id,
            "kommo_lead_id": lead_id,
            "return_page": return_page,
            "event_type": event_type,
            "calendar_title": title,
        }
        await telegram_state_service.set_state(
            user_id,
            updated,
            ttl_seconds=settings.telegram_state_ttl_minutes * 60,
        )
        if event_type in calendar_event_builder.TASK_ONLY_EVENT_TYPES:
            await telegram_state_service.set_state(
                user_id,
                {
                    **updated,
                    "mode": "awaiting_calendar_custom_date",
                    "event_type": event_type,
                },
                ttl_seconds=settings.telegram_state_ttl_minutes * 60,
            )
            await telegram_service.send_message(
                chat_id,
                (
                    "🕒 <b>Когда выполнить?</b>\n\n"
                    "Напишите дату и время. Например: <code>завтра 10:00</code> "
                    "или <code>пятницу в 15:30</code>."
                ),
            )
            return True
        await telegram_service.send_calendar_date_picker(
            chat_id,
            lead_id=lead_id,
            event_type_label=calendar_event_builder.EVENT_TYPE_LABELS.get(
                event_type, title
            ),
            return_page=return_page,
        )
        return True

    if callback_data.startswith("calday:"):
        _, choice, lead_id_raw, page_raw = callback_data.split(":", 3)
        lead_id = int(lead_id_raw)
        return_page = int(page_raw)
        state = await telegram_state_service.get_state(user_id)
        if not state:
            await telegram_service.send_message(chat_id, "⚠️ Мастер календаря устарел.")
            return True
        if choice == "custom":
            await telegram_state_service.set_state(
                user_id,
                {**state, "mode": "awaiting_calendar_custom_date"},
                ttl_seconds=settings.telegram_state_ttl_minutes * 60,
            )
            await telegram_service.send_message(
                chat_id,
                "🕒 Введите дату и время. Например: <code>4.07.2026 10:00</code> или <code>завтра 10:00</code>.",
            )
            return True
        await telegram_state_service.set_state(
            user_id,
            {**state, "mode": "awaiting_calendar_time", "selected_day": choice},
            ttl_seconds=settings.telegram_state_ttl_minutes * 60,
        )
        await telegram_service.send_calendar_time_picker(
            chat_id, lead_id=lead_id, return_page=return_page
        )
        return True

    if callback_data.startswith("caltime:"):
        _, choice, lead_id_raw, page_raw = callback_data.split(":", 3)
        lead_id = int(lead_id_raw)
        return_page = int(page_raw)
        state = await telegram_state_service.get_state(user_id)
        if not state:
            await telegram_service.send_message(chat_id, "⚠️ Мастер календаря устарел.")
            return True
        if choice == "custom":
            await telegram_state_service.set_state(
                user_id,
                {**state, "mode": "awaiting_calendar_custom_time"},
                ttl_seconds=settings.telegram_state_ttl_minutes * 60,
            )
            await telegram_service.send_message(
                chat_id, "🕒 Введите время. Например: <code>10:30</code>."
            )
            return True
        selected_day = str(state.get("selected_day") or "today")
        if selected_day in {"today", "tomorrow", "dayafter"}:
            base = calendar_event_builder.quick_datetime(selected_day)
            hour = int(choice.replace("time", ""))
            start_dt = base.replace(hour=hour, minute=0, second=0, microsecond=0)
            if start_dt <= datetime.now(tz=start_dt.tzinfo):
                start_dt += timedelta(days=1)
        else:
            start_dt = calendar_event_builder.quick_datetime(choice)
        updated = {
            **state,
            "mode": "awaiting_calendar_duration",
            "start_iso": start_dt.isoformat(),
            "start_display": _format_manager_datetime(start_dt),
        }
        await telegram_state_service.set_state(
            user_id,
            updated,
            ttl_seconds=settings.telegram_state_ttl_minutes * 60,
        )
        await telegram_service.send_calendar_duration_picker(
            chat_id, lead_id=lead_id, return_page=return_page
        )
        return True

    if callback_data.startswith("calrem:"):
        _, reminder_raw, lead_id_raw, page_raw = callback_data.split(":", 3)
        lead_id = int(lead_id_raw)
        return_page = int(page_raw)
        state = await telegram_state_service.get_state(user_id)
        if not state:
            await telegram_service.send_message(chat_id, "⚠️ Мастер календаря устарел.")
            return True
        updated = {
            **state,
            "reminder_minutes": int(reminder_raw),
        }
        await _show_calendar_preview_from_state(chat_id, user_id, updated)
        return True

    if callback_data.startswith("caldur:"):
        parts = callback_data.split(":", 3)
        if len(parts) == 4:
            _, duration_raw, lead_id_raw, page_raw = parts
            lead_id = int(lead_id_raw)
            return_page = int(page_raw)
            state = await telegram_state_service.get_state(user_id)
            if not state:
                await telegram_service.send_message(chat_id, "⚠️ Мастер календаря устарел.")
                return True
            if duration_raw == "custom":
                await telegram_state_service.set_state(
                    user_id,
                    {**state, "mode": "awaiting_calendar_custom_duration"},
                    ttl_seconds=settings.telegram_state_ttl_minutes * 60,
                )
                await telegram_service.send_message(
                    chat_id,
                    "⏱ Введите длительность в минутах. Например: <code>45</code>.",
                )
                return True
            updated = {
                **state,
                "mode": "awaiting_calendar_reminder",
                "duration_minutes": int(duration_raw),
            }
            await telegram_state_service.set_state(
                user_id,
                updated,
                ttl_seconds=settings.telegram_state_ttl_minutes * 60,
            )
            await telegram_service.send_calendar_reminder_picker(
                chat_id, lead_id=lead_id, return_page=return_page
            )
            return True

    if callback_data.startswith("calendar:edit:"):
        _, _, lead_id_raw, page_raw = callback_data.split(":", 3)
        await _prompt_calendar_event(
            chat_id, user_id, int(lead_id_raw), int(page_raw)
        )
        return True

    if callback_data.startswith("calendar:retry_kommo:"):
        _, _, lead_id_raw, page_raw = callback_data.split(":", 3)
        lead_id = int(lead_id_raw)
        state = await telegram_state_service.get_state(user_id)
        if not state or not state.get("start_iso"):
            await telegram_service.send_message(chat_id, "⚠️ Сессия устарела.")
            return True
        try:
            draft = await _build_calendar_draft_from_state(state)
            task = await kommo_service.create_lead_task(
                lead_id=lead_id,
                text=draft.title[:1000],
                complete_till=int(draft.start_at.timestamp()),
            )
            await telegram_service.send_message(
                chat_id,
                (
                    "✅ <b>Задача Kommo создана</b>\n\n"
                    f"Task ID: <code>{task.get('task_id')}</code>"
                ),
            )
        except Exception as exc:
            await telegram_service.send_message(
                chat_id,
                f"❌ <b>Не удалось создать задачу</b>\n\n{html.escape(str(exc)[:500])}",
            )
        return True

    if callback_data == "state:cancel":
        await telegram_state_service.clear_state(user_id)
        await telegram_service.send_message(chat_id, "❌ Действие отменено.")
        await telegram_service.send_main_menu(chat_id)
        return True
    if callback_data.startswith("note:cancel:"):
        _, _, lead_id_raw, page_raw = callback_data.split(":", 3)
        await telegram_state_service.clear_state(user_id)
        await telegram_service.send_message(chat_id, "❌ Примечание не добавлено.")
        await _show_lead_details(chat_id, int(lead_id_raw), return_page=int(page_raw))
        return True
    if callback_data.startswith("note:confirm:"):
        _, _, lead_id_raw, page_raw = callback_data.split(":", 3)
        lead_id = int(lead_id_raw)
        state = await telegram_state_service.get_state(user_id)
        if not state or state.get("mode") != "pending_note_confirmation":
            await telegram_service.send_message(
                chat_id,
                "⚠️ Подтверждение устарело. Откройте сделку и добавьте примечание заново.",
            )
            return True
        if int(state.get("kommo_lead_id") or 0) != lead_id:
            await telegram_service.send_message(
                chat_id, "❌ Сделка в подтверждении не совпадает."
            )
            return True
        result = await kommo_service.add_text_note(
            lead_id,
            str(state.get("note_text") or ""),
            source="Telegram",
        )
        await telegram_state_service.clear_state(user_id)
        await telegram_service.send_message(
            chat_id,
            (
                "✅ <b>Примечание добавлено в Kommo</b>\n\n"
                f"Сделка: {html.escape(str(result.get('lead_name') or '—'))}\n"
                f"ID: <code>{lead_id}</code>\n"
                f"<a href=\"{html.escape(str(result.get('url') or ''), quote=True)}\">Открыть сделку</a>"
            ),
        )
        await _show_lead_details(chat_id, lead_id, return_page=int(page_raw))
        return True

    if callback_data.startswith("task:cancel:"):
        _, _, lead_id_raw, page_raw = callback_data.split(":", 3)
        await telegram_state_service.clear_state(user_id)
        await telegram_service.send_message(chat_id, "❌ Задача не создана.")
        await _show_lead_details(chat_id, int(lead_id_raw), return_page=int(page_raw))
        return True

    if callback_data.startswith("task:confirm:"):
        _, _, lead_id_raw, page_raw = callback_data.split(":", 3)
        lead_id = int(lead_id_raw)
        state = await telegram_state_service.get_state(user_id)
        if not state or state.get("mode") != "pending_task_confirmation":
            await telegram_service.send_message(
                chat_id, "⚠️ Подтверждение задачи устарело."
            )
            return True
        if int(state.get("kommo_lead_id") or 0) != lead_id:
            await telegram_service.send_message(
                chat_id, "❌ Сделка в подтверждении не совпадает."
            )
            return True
        result = await kommo_service.create_lead_task(
            lead_id=lead_id,
            text=str(state.get("task_text") or ""),
            complete_till=int(state.get("complete_till") or 0),
            responsible_user_id=(
                int(state["responsible_user_id"])
                if state.get("responsible_user_id")
                else None
            ),
        )
        await telegram_state_service.clear_state(user_id)
        await telegram_service.send_message(
            chat_id,
            (
                "✅ <b>Задача создана в Kommo</b>\n\n"
                f"Сделка: {html.escape(str(result.get('lead_name') or '—'))}\n"
                f"Задача: {html.escape(str(result.get('text') or '—'))}\n"
                f"Срок: {html.escape(str(state.get('due_display') or '—'))}\n"
                + (
                    f"Task ID: <code>{result.get('task_id')}</code>\n"
                    if result.get("task_id")
                    else ""
                )
                + f"<a href=\"{html.escape(str(result.get('url') or ''), quote=True)}\">Открыть сделку</a>"
            ),
        )
        await _show_lead_details(chat_id, lead_id, return_page=int(page_raw))
        return True

    if callback_data.startswith("calendar:cancel:"):
        _, _, lead_id_raw, page_raw = callback_data.split(":", 3)
        await telegram_state_service.clear_state(user_id)
        await telegram_service.send_message(chat_id, "❌ Событие календаря не создано.")
        await _show_lead_details(chat_id, int(lead_id_raw), return_page=int(page_raw))
        return True

    if callback_data.startswith("calendar:confirm:"):
        _, _, lead_id_raw, page_raw = callback_data.split(":", 3)
        lead_id = int(lead_id_raw)
        return_page = int(page_raw)
        state = await telegram_state_service.get_state(user_id)
        if not state or state.get("mode") != "pending_calendar_confirmation":
            await telegram_service.send_message(
                chat_id, "⚠️ Подтверждение события устарело."
            )
            return True
        if int(state.get("kommo_lead_id") or 0) != lead_id:
            await telegram_service.send_message(
                chat_id, "❌ Сделка в подтверждении не совпадает."
            )
            return True
        await telegram_service.send_message(chat_id, "⏳ Создаю событие…")
        try:
            draft = await _build_calendar_draft_from_state(state)
            idempotency_key = calendar_event_builder.build_idempotency_key(
                telegram_user_id=user_id,
                source_id=str(state.get("confirm_source_id") or f"lead:{lead_id}"),
                kommo_lead_id=lead_id,
                event_type=draft.event_type,
                start_iso=draft.start_iso(),
            )
            result = await calendar_scheduling_service.schedule_confirmed_event(
                db,
                draft=draft,
                telegram_user_id=user_id,
                idempotency_key=idempotency_key,
            )
        except Exception as exc:
            await telegram_service.send_message(
                chat_id,
                (
                    "❌ <b>Не удалось создать событие</b>\n\n"
                    f"{html.escape(str(exc)[:500])}"
                ),
            )
            return True
        await telegram_state_service.set_state(
            user_id,
            {**state, "mode": "calendar_completed", "last_result": result},
            ttl_seconds=settings.telegram_state_ttl_minutes * 60,
        )
        await telegram_service.send_calendar_success(
            chat_id,
            lead_id=lead_id,
            return_page=return_page,
            result=result,
        )
        return True

    return not callback_data.startswith("action:")


async def _handle_text_state(
    *,
    chat_id: int,
    user_id: int,
    text: str,
) -> bool:
    state = await telegram_state_service.get_state(user_id)
    if not state:
        return False

    mode = state.get("mode")
    if mode == "awaiting_lead_search":
        await telegram_state_service.clear_state(user_id)
        await telegram_service.send_message(chat_id, "🔄 Ищу сделки в Kommo...")
        result = await kommo_service.search_open_leads(text, limit=20)
        await telegram_service.send_lead_selection_menu(
            chat_id,
            result,
            page=1,
            search_mode=True,
        )
        return True

    if mode == "unreviewed_manual_entry":
        lead_id = int(state.get("kommo_lead_id") or 0)
        return_page = int(state.get("return_page") or 1)
        lead_number = text.strip()
        if not lead_number.isdigit():
            await telegram_service.send_message(
                chat_id, "Введите числовой внутренний номер лида."
            )
            return True
        try:
            preview = await unreviewed_leads_service.build_preview_from_manual_number(
                lead_number
            )
            details = await kommo_service.get_lead_details(lead_id)
        except ValueError as exc:
            await telegram_service.send_message(chat_id, f"❌ {html.escape(str(exc))}")
            return True
        except google_sheets_service.GoogleSheetsError as exc:
            await telegram_service.send_message(
                chat_id,
                f"❌ <b>Ошибка Google Sheets</b>\n\n{html.escape(str(exc)[:500])}",
            )
            return True
        await _show_unreviewed_preview(
            chat_id,
            user_id,
            lead_id=lead_id,
            return_page=return_page,
            current_name=str(details.get("name") or ""),
            preview=preview,
        )
        return True

    if mode == "unreviewed_edit_name":
        lead_id = int(state.get("kommo_lead_id") or 0)
        return_page = int(state.get("return_page") or 1)
        preview = dict(state.get("preview") or {})
        new_name = text.strip()
        if not new_name:
            await telegram_service.send_message(chat_id, "Название не может быть пустым.")
            return True
        preview["proposed_name"] = new_name[:255]
        current_name = str(state.get("lead_name") or "")
        await _show_unreviewed_preview(
            chat_id,
            user_id,
            lead_id=lead_id,
            return_page=return_page,
            current_name=current_name,
            preview=preview,
        )
        return True

    if mode == "kommo_lead_edit_field":
        field = str(state.get("edit_field") or "")
        lead_id = int(state.get("kommo_lead_id") or 0)
        return_page = int(state.get("return_page") or 1)
        draft = dict(state.get("draft") or {})
        original = dict(state.get("original") or {})
        value = text.strip()
        if field == "name":
            if not value:
                await telegram_service.send_message(
                    chat_id, "Название не может быть пустым."
                )
                return True
            draft["name"] = value[:255]
        elif field == "price":
            digits = re.sub(r"\D", "", value)
            if not digits:
                await telegram_service.send_message(
                    chat_id,
                    "Введите бюджет числом. Например: <code>15000</code>.",
                )
                return True
            draft["price"] = int(digits)
        else:
            await telegram_service.send_message(chat_id, "❌ Неизвестное поле.")
            return True
        await _show_lead_edit_preview(
            chat_id,
            user_id,
            lead_id,
            return_page=return_page,
            draft=draft,
            original=original,
        )
        return True

    if mode == "kommo_create_edit":
        field = str(state.get("edit_field") or "")
        value = text.strip()
        draft = dict(state.get("draft") or {})
        if value == "-":
            value = ""
        mapping = {
            "number": "lead_number",
            "name": "lead_name",
            "client": "client_name",
            "product": "product_requested",
            "budget": "budget",
            "city": "city",
        }
        target = mapping.get(field)
        if not target:
            await telegram_service.send_message(chat_id, "❌ Неизвестное поле.")
            return True
        draft[target] = value or None
        if field == "number":
            base = str(
                draft.get("lead_name")
                or draft.get("product_requested")
                or "Новый запрос"
            )
            base_without_number = base
            if base.split(maxsplit=1)[0].isdigit():
                base_without_number = (
                    base.split(maxsplit=1)[1]
                    if len(base.split(maxsplit=1)) > 1
                    else "Новый запрос"
                )
            draft["lead_name"] = (
                f"{value} {base_without_number}".strip()
                if value
                else base_without_number
            )
        elif field == "product" and not draft.get("lead_name"):
            draft["lead_name"] = value
        await _show_creation_preview(
            chat_id,
            user_id,
            db=None,
            lead_id=int(state["local_lead_id"]),
            voice_note_id=int(state["voice_note_id"]),
            draft=draft,
        )
        return True

    if mode == "awaiting_task_text":
        task_text = text.strip()
        if not task_text:
            await telegram_service.send_message(chat_id, "Текст задачи пустой.")
            return True
        await telegram_state_service.set_state(
            user_id,
            {**state, "mode": "awaiting_task_date", "task_text": task_text},
            ttl_seconds=settings.telegram_state_ttl_minutes * 60,
        )
        await telegram_service.send_message(
            chat_id,
            "🕒 <b>Шаг 2 из 2 · Срок задачи</b>\n\nВыберите время или введите своё:",
            reply_markup={
                "inline_keyboard": [
                    [
                        {"text": "Сегодня 17:00", "callback_data": "taskdate:today17"},
                        {
                            "text": "Завтра 10:00",
                            "callback_data": "taskdate:tomorrow10",
                        },
                    ],
                    [
                        {
                            "text": "Завтра 15:00",
                            "callback_data": "taskdate:tomorrow15",
                        },
                        {"text": "Другая дата", "callback_data": "taskdate:custom"},
                    ],
                    [{"text": "❌ Отмена", "callback_data": "state:cancel"}],
                ]
            },
        )
        return True

    if mode in {"awaiting_task_date", "awaiting_task_custom_date"}:
        try:
            due_dt = _parse_manager_datetime(text)
        except ValueError as exc:
            await telegram_service.send_message(chat_id, f"❌ {html.escape(str(exc))}")
            return True
        due_display = _format_manager_datetime(due_dt)
        await telegram_state_service.set_state(
            user_id,
            {
                **state,
                "mode": "pending_task_confirmation",
                "complete_till": int(due_dt.astimezone(timezone.utc).timestamp()),
                "due_display": due_display,
            },
            ttl_seconds=settings.telegram_state_ttl_minutes * 60,
        )
        await telegram_service.send_task_confirmation(
            chat_id,
            lead_id=int(state["kommo_lead_id"]),
            lead_name=str(state.get("lead_name") or "—"),
            task_text=str(state.get("task_text") or ""),
            due_display=due_display,
            return_page=int(state.get("return_page") or 1),
        )
        return True

    if mode in {
        "awaiting_calendar_custom_date",
        "awaiting_calendar_custom_time",
        "awaiting_calendar_date",
    }:
        state = state or {}
        try:
            if mode == "awaiting_calendar_custom_time":
                selected_day = str(state.get("selected_day") or "today")
                base = calendar_event_builder.quick_datetime(selected_day)
                time_part = text.strip().replace("в ", "")
                parsed_time = calendar_event_builder._parse_time_fragment(time_part)
                start_dt = datetime.combine(base.date(), parsed_time, tzinfo=base.tzinfo)
            else:
                start_dt, parsed_duration = calendar_event_builder.parse_natural_datetime(
                    text,
                    duration_minutes=int(state.get("duration_minutes") or 30),
                )
                if mode == "awaiting_calendar_custom_date":
                    state = {**state, "duration_minutes": parsed_duration}
        except ValueError as exc:
            await telegram_service.send_message(chat_id, f"❌ {html.escape(str(exc))}")
            return True
        lead_id = int(state.get("kommo_lead_id") or 0)
        return_page = int(state.get("return_page") or 1)
        event_type = str(state.get("event_type") or "call")
        updated = {
            **state,
            "start_iso": start_dt.isoformat(),
            "start_display": _format_manager_datetime(start_dt),
        }
        if event_type in calendar_event_builder.TASK_ONLY_EVENT_TYPES:
            updated["mode"] = "awaiting_calendar_reminder"
            updated.setdefault("duration_minutes", 30)
            updated.setdefault("reminder_minutes", 30)
            await telegram_state_service.set_state(
                user_id,
                updated,
                ttl_seconds=settings.telegram_state_ttl_minutes * 60,
            )
            await telegram_service.send_calendar_reminder_picker(
                chat_id, lead_id=lead_id, return_page=return_page
            )
            return True
        updated["mode"] = "awaiting_calendar_duration"
        await telegram_state_service.set_state(
            user_id,
            updated,
            ttl_seconds=settings.telegram_state_ttl_minutes * 60,
        )
        await telegram_service.send_calendar_duration_picker(
            chat_id, lead_id=lead_id, return_page=return_page
        )
        return True

    if mode == "awaiting_calendar_custom_duration":
        digits = re.sub(r"\D", "", text)
        if not digits:
            await telegram_service.send_message(chat_id, "Введите длительность числом.")
            return True
        lead_id = int(state.get("kommo_lead_id") or 0)
        return_page = int(state.get("return_page") or 1)
        updated = {**state, "duration_minutes": int(digits), "mode": "awaiting_calendar_reminder"}
        await telegram_state_service.set_state(
            user_id,
            updated,
            ttl_seconds=settings.telegram_state_ttl_minutes * 60,
        )
        await telegram_service.send_calendar_reminder_picker(
            chat_id, lead_id=lead_id, return_page=return_page
        )
        return True

    if mode == "awaiting_text_note":
        note_text = text.strip()
        if not note_text:
            await telegram_service.send_message(
                chat_id, "Примечание пустое. Отправьте текст."
            )
            return True
        lead_id = int(state["kommo_lead_id"])
        return_page = int(state.get("return_page") or 1)
        lead_name = str(state.get("lead_name") or f"Сделка {lead_id}")
        await telegram_state_service.set_state(
            user_id,
            {
                **state,
                "mode": "pending_note_confirmation",
                "note_text": note_text,
            },
            ttl_seconds=settings.telegram_state_ttl_minutes * 60,
        )
        await telegram_service.send_note_confirmation(
            chat_id,
            lead_id=lead_id,
            lead_name=lead_name,
            note_text=note_text,
            return_page=return_page,
        )
        return True

    if mode == "awaiting_audio_for_lead":
        await telegram_service.send_message(
            chat_id,
            "Сейчас ожидается голосовое сообщение или аудиофайл. Для отмены нажмите кнопку «Отмена».",
        )
        return True

    if mode == "awaiting_audio_for_new_lead":
        await telegram_service.send_message(
            chat_id,
            (
                "Сейчас ожидается запись <b>разговора с клиентом</b>.\n"
                "Отправьте голосовое или нажмите «Отмена»."
            ),
        )
        return True

    if mode == "pending_note_confirmation":
        await telegram_service.send_message(
            chat_id,
            "Сначала подтвердите или отмените примечание кнопкой под предыдущим сообщением.",
        )
        return True

    if mode == "pending_task_confirmation":
        await telegram_service.send_message(
            chat_id,
            "Сначала подтвердите или отмените задачу кнопкой под предыдущим сообщением.",
        )
        return True

    if mode == "pending_calendar_confirmation":
        await telegram_service.send_message(
            chat_id,
            "Сначала подтвердите или отмените событие кнопкой под предыдущим сообщением.",
        )
        return True

    return False


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
                handled = await _handle_manager_callback(
                    callback_data=callback_data,
                    chat_id=chat_id,
                    user_id=user_id,
                    db=db,
                )
                if handled:
                    return {"ok": True}

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
                    f"❌ Ошибка выполнения действия: {html.escape(str(exc)[:500])}",
                )
            return {"ok": True}

        message = body.get("message", {})
        if not message:
            return {"ok": True}

        chat_id = message["chat"]["id"]
        user_id = message["from"]["id"]
        message_id = message["message_id"]
        text = (message.get("text") or "").strip()

        if not _is_allowed_user(user_id):
            await telegram_service.send_message(chat_id, "Доступ запрещён.")
            return {"ok": True}

        if text.startswith(("/start", "/menu")):
            await telegram_state_service.clear_state(user_id)
            await telegram_service.send_main_menu(chat_id)
            return {"ok": True}

        if text.startswith("/kommo_test"):
            await _handle_kommo_test(chat_id, user_id, db)
            return {"ok": True}

        if text.startswith("/calendar_test_write"):
            await _handle_calendar_test(chat_id, user_id, include_write_probe=True)
            return
        if text.startswith("/calendar_test"):
            await _handle_calendar_test(chat_id, user_id)
            return {"ok": True}

        if text.startswith(("/digest", "/morning")):
            await _handle_morning_digest(chat_id)
            return {"ok": True}

        if text.startswith("/jobs"):
            await _show_audio_jobs(chat_id, user_id, db)
            return {"ok": True}

        if text.startswith(("/kommo_leads", "/open_deals", "/deals")):
            await _show_lead_page(chat_id, user_id, 1)
            return {"ok": True}

        if text.startswith("/lead"):
            parts = text.split(maxsplit=1)
            if len(parts) != 2 or not parts[1].strip().isdigit():
                await telegram_service.send_message(
                    chat_id, "Использование: <code>/lead 123456</code>"
                )
            else:
                await _show_lead_details(chat_id, int(parts[1].strip()), return_page=1)
            return {"ok": True}

        if text and await _handle_text_state(
            chat_id=chat_id, user_id=user_id, text=text
        ):
            return {"ok": True}

        if text and await _handle_text_command(
            chat_id=chat_id, user_id=user_id, text=text, db=db
        ):
            return {"ok": True}

        attachment = _extract_audio_attachment(message)
        if attachment:
            state = await telegram_state_service.get_state(user_id)
            if state and state.get("mode") in {
                "awaiting_text_note",
                "pending_note_confirmation",
                "awaiting_task_text",
                "awaiting_task_date",
                "awaiting_task_custom_date",
                "pending_task_confirmation",
                "awaiting_calendar_event_type",
                "awaiting_calendar_date",
                "awaiting_calendar_time",
                "awaiting_calendar_custom_date",
                "awaiting_calendar_custom_time",
                "awaiting_calendar_duration",
                "awaiting_calendar_custom_duration",
                "awaiting_calendar_reminder",
                "pending_calendar_confirmation",
                "kommo_create_edit",
                "kommo_create_preview",
            }:
                await telegram_service.send_message(
                    chat_id,
                    "Сейчас ожидается другое действие. Нажмите «Отмена» или откройте /menu, чтобы обработать аудио.",
                )
                return {"ok": True}

            target_kommo_lead_id: int | None = None
            target_lead_name: str | None = None
            audio_intent = "command"
            if state and state.get("mode") == "awaiting_audio_for_lead":
                target_kommo_lead_id = int(state.get("kommo_lead_id") or 0) or None
                target_lead_name = str(state.get("lead_name") or "") or None
                audio_intent = "lead_followup"
            elif state and state.get("mode") == "awaiting_audio_for_new_lead":
                audio_intent = "new_lead"

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

            if not await _claim_audio_message(user_id, message_id):
                logger.info(
                    "Duplicate Telegram audio update ignored: user_id=%s message_id=%s",
                    user_id,
                    message_id,
                )
                return {"ok": True}

            process_kwargs = {
                "chat_id": chat_id,
                "telegram_user_id": user_id,
                "telegram_message_id": message_id,
                "file_id": attachment["file_id"],
                "file_extension": attachment["file_extension"],
                "target_kommo_lead_id": target_kommo_lead_id,
                "audio_intent": audio_intent,
            }
            processing_mode = (
                (settings.audio_processing_mode or "direct").strip().lower()
            )

            if processing_mode != "celery":
                logger.info(
                    "Telegram audio started in direct mode: user_id=%s message_id=%s "
                    "target_kommo_lead_id=%s audio_intent=%s",
                    user_id,
                    message_id,
                    target_kommo_lead_id,
                    audio_intent,
                )
                _spawn_background(process_voice_note_async(**process_kwargs))
                if audio_intent == "lead_followup" and target_kommo_lead_id:
                    await telegram_state_service.clear_state(user_id)
                    await telegram_service.send_message(
                        chat_id,
                        (
                            "🎙 Аудио получено. Начинаю анализ разговора по сделке.\n"
                            f"Сделка: <b>{html.escape(target_lead_name or str(target_kommo_lead_id))}</b>\n"
                            f"ID: <code>{target_kommo_lead_id}</code>"
                        ),
                    )
                elif audio_intent == "new_lead":
                    await telegram_state_service.clear_state(user_id)
                    await telegram_service.send_message(
                        chat_id,
                        "🎙 Записываю разговор с клиентом. Начинаю анализ…",
                    )
                else:
                    await telegram_service.send_message(
                        chat_id,
                        "🎙 Слушаю вашу команду…",
                    )
                return {"ok": True}

            try:
                task = process_voice_note.apply_async(
                    kwargs=process_kwargs,
                    queue="voice_notes",
                )
                logger.info(
                    "Telegram audio queued: task_id=%s user_id=%s message_id=%s target_kommo_lead_id=%s",
                    task.id,
                    user_id,
                    message_id,
                    target_kommo_lead_id,
                )
                _spawn_background(
                    _audio_queue_watchdog(
                        task_id=task.id,
                        process_kwargs=process_kwargs,
                        chat_id=chat_id,
                    )
                )
                if audio_intent == "lead_followup" and target_kommo_lead_id:
                    await telegram_state_service.clear_state(user_id)
                    await telegram_service.send_message(
                        chat_id,
                        (
                            "🎙 Аудио поставлено в очередь для анализа по сделке.\n"
                            f"Сделка: <b>{html.escape(target_lead_name or str(target_kommo_lead_id))}</b>\n"
                            f"ID: <code>{target_kommo_lead_id}</code>\n\n"
                            "Если Celery worker не заберёт задачу, бот автоматически запустит резервную обработку."
                        ),
                    )
                elif audio_intent == "new_lead":
                    await telegram_state_service.clear_state(user_id)
                    await telegram_service.send_message(
                        chat_id,
                        (
                            "🎙 Разговор с клиентом поставлен в очередь на анализ.\n\n"
                            "Если Celery worker не заберёт задачу, бот автоматически запустит резервную обработку."
                        ),
                    )
                else:
                    await telegram_service.send_message(
                        chat_id,
                        (
                            "🎙 Команда поставлена в очередь.\n\n"
                            "Если Celery worker не заберёт задачу, бот автоматически запустит резервную обработку."
                        ),
                    )
            except Exception:
                logger.exception(
                    "Could not queue voice/audio processing task; starting direct processing"
                )
                _spawn_background(process_voice_note_async(**process_kwargs))
                await telegram_service.send_message(
                    chat_id,
                    "⚠️ Очередь Redis/Celery недоступна. Начинаю обработку аудио на основном сервере.",
                )
            return {"ok": True}

        if message.get("document"):
            await telegram_service.send_message(
                chat_id,
                "Формат файла не поддерживается. Отправьте .m4a, .mp3, .mp4, .wav, .ogg или .webm размером до 20 МБ.",
            )
            return {"ok": True}

        await telegram_service.send_main_menu(chat_id)

    except Exception:
        logger.exception("Telegram webhook handler error")

    return {"ok": True}
