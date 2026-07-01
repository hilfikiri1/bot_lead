"""FastAPI router for the Telegram webhook and Kommo manager menu."""

from __future__ import annotations

import asyncio
import hmac
import html
import logging
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
    calendar_service,
    crm_service,
    kommo_service,
    telegram_service,
    telegram_state_service,
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


async def _handle_calendar_test(chat_id: int, user_id: int) -> None:
    if not _is_allowed_user(user_id):
        await telegram_service.send_message(chat_id, "Доступ запрещён.")
        return

    provider = calendar_service.provider_label()
    await telegram_service.send_message(
        chat_id, f"🔄 Проверяю подключение к {html.escape(provider)}..."
    )
    try:
        if (settings.calendar_provider or "icloud").strip().lower() == "icloud":
            details = await asyncio.to_thread(calendar_service.test_icloud_connection)
            await telegram_service.send_message(
                chat_id,
                (
                    f"✅ <b>{html.escape(provider)} доступен</b>\n\n"
                    f"{html.escape(details)}\n\n"
                    "Можно создавать напоминания из карточки сделки или кнопки "
                    "«Следующий контакт»."
                ),
            )
            return

        test_result = await asyncio.to_thread(
            calendar_service.create_event_with_fallback,
            "Тест Telegram Assistant",
            "Проверка интеграции календаря",
            None,
            15,
        )
        if test_result["success"]:
            await telegram_service.send_message(
                chat_id,
                (
                    f"✅ <b>{html.escape(provider)} работает</b>\n\n"
                    f"Тестовое событие создано. ID: "
                    f"<code>{html.escape(str(test_result['event_id']))}</code>"
                ),
            )
        else:
            await telegram_service.send_message(
                chat_id,
                f"❌ {html.escape(str(test_result.get('error') or 'Ошибка календаря'))}",
            )
    except calendar_service.CalendarIntegrationError as exc:
        await telegram_service.send_message(
            chat_id,
            (
                "❌ <b>Календарь не настроен</b>\n\n"
                f"{html.escape(str(exc))}\n\n"
                "Проверьте Railway Variables:\n"
                "• <code>ICLOUD_USERNAME</code>\n"
                "• <code>ICLOUD_APP_SPECIFIC_PASSWORD</code>\n"
                "• <code>ICLOUD_CALENDAR_NAME</code>\n"
                "• <code>ICLOUD_CALDAV_URL</code>"
            ),
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
    result = await asyncio.to_thread(
        calendar_service.create_event_with_fallback,
        title,
        description,
        start_iso,
        duration_minutes,
    )
    if result["success"]:
        await telegram_service.send_message(
            chat_id,
            (
                f"✅ <b>Событие создано в {html.escape(result['provider'])}</b>\n\n"
                + (
                    f"Сделка: {html.escape(lead_name)}\n"
                    if lead_name
                    else ""
                )
                + f"Название: {html.escape(title)}\n"
                + (
                    f"Начало: {html.escape(start_display)}\n"
                    if start_display
                    else ""
                )
                + f"Event ID: <code>{html.escape(str(result['event_id']))}</code>\n"
                "Напоминание: за 10 минут до начала."
            ),
        )
        return

    await telegram_service.send_message(
        chat_id,
        (
            "⚠️ <b>Не удалось записать событие напрямую в календарь</b>\n\n"
            f"{html.escape(str(result.get('error') or 'Неизвестная ошибка'))}\n\n"
            "Отправляю файл <code>.ics</code>. Откройте его на iPhone или Mac и "
            "нажмите «Добавить в календарь»."
        ),
    )
    ics_bytes = str(result.get("ics_content") or "").encode("utf-8")
    if ics_bytes:
        await telegram_service.send_document(
            chat_id,
            filename="reminder.ics",
            content=ics_bytes,
            caption=f"📅 {html.escape(title)}",
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
            "mode": "awaiting_calendar_title",
            "chat_id": chat_id,
            "kommo_lead_id": lead_id,
            "lead_name": details.get("name"),
            "lead_url": details.get("url"),
            "return_page": return_page,
        },
        ttl_seconds=settings.telegram_state_ttl_minutes * 60,
    )
    await telegram_service.send_message(
        chat_id,
        (
            "📅 <b>Шаг 1 из 3 · Событие</b>\n\n"
            f"Сделка: <b>{html.escape(str(details.get('name') or '—'))}</b>\n\n"
            "Напишите название события. Например: <i>Созвон с клиентом</i>."
        ),
        reply_markup={
            "inline_keyboard": [
                [{"text": "❌ Отмена", "callback_data": "state:cancel"}]
            ]
        },
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
    if callback_data == "menu:jobs":
        await _show_audio_jobs(chat_id, user_id, db)
        return True
    if callback_data == "menu:search":
        await _prompt_search(chat_id, user_id)
        return True
    if callback_data == "menu:new":
        await telegram_state_service.clear_state(user_id)
        await telegram_service.send_message(
            chat_id,
            (
                "🎙 <b>Новый разговор</b>\n\n"
                "Отправьте голосовое сообщение или аудиофайл. Бот покажет прогресс, "
                "сформирует анализ на русском и подготовит карточку нового лида."
            ),
            reply_markup={
                "inline_keyboard": [[{"text": "🏠 Меню", "callback_data": "menu:home"}]]
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

    if callback_data.startswith("caldate:"):
        choice = callback_data.split(":", 1)[1]
        state = await telegram_state_service.get_state(user_id)
        if not state or state.get("mode") != "awaiting_calendar_date":
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
                "🕒 Введите дату и время. Например: <code>завтра 10:00</code> или <code>30.06.2026 15:30</code>.",
            )
            return True
        start_dt = _quick_manager_datetime(choice)
        await telegram_state_service.set_state(
            user_id,
            {
                **state,
                "mode": "awaiting_calendar_duration",
                "start_iso": start_dt.isoformat(),
                "start_display": _format_manager_datetime(start_dt),
            },
            ttl_seconds=settings.telegram_state_ttl_minutes * 60,
        )
        await telegram_service.send_message(
            chat_id,
            "⏱ <b>Шаг 3 из 3 · Длительность</b>\n\nВыберите продолжительность:",
            reply_markup={
                "inline_keyboard": [
                    [
                        {"text": "15 мин", "callback_data": "caldur:15"},
                        {"text": "30 мин", "callback_data": "caldur:30"},
                    ],
                    [
                        {"text": "45 мин", "callback_data": "caldur:45"},
                        {"text": "60 мин", "callback_data": "caldur:60"},
                    ],
                    [{"text": "❌ Отмена", "callback_data": "state:cancel"}],
                ]
            },
        )
        return True

    if callback_data.startswith("caldur:"):
        duration = int(callback_data.split(":", 1)[1])
        state = await telegram_state_service.get_state(user_id)
        if not state or state.get("mode") != "awaiting_calendar_duration":
            await telegram_service.send_message(chat_id, "⚠️ Мастер календаря устарел.")
            return True
        await telegram_state_service.set_state(
            user_id,
            {
                **state,
                "mode": "pending_calendar_confirmation",
                "duration_minutes": duration,
            },
            ttl_seconds=settings.telegram_state_ttl_minutes * 60,
        )
        await telegram_service.send_calendar_confirmation(
            chat_id,
            lead_id=int(state["kommo_lead_id"]),
            lead_name=str(state.get("lead_name") or "—"),
            title=str(state.get("calendar_title") or "Созвон с клиентом"),
            start_display=str(state.get("start_display") or "—"),
            duration_minutes=duration,
            return_page=int(state.get("return_page") or 1),
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
        description = (
            f"Kommo: {state.get('lead_name') or lead_id}\n"
            f"Lead ID: {lead_id}\n"
            f"{state.get('lead_url') or ''}"
        )
        try:
            await _deliver_calendar_result(
                chat_id,
                title=str(state.get("calendar_title") or "Созвон с клиентом"),
                description=description,
                start_iso=str(state.get("start_iso") or ""),
                duration_minutes=int(state.get("duration_minutes") or 30),
                lead_name=str(state.get("lead_name") or "—"),
                start_display=str(state.get("start_display") or "—"),
            )
        except Exception as exc:
            await telegram_service.send_message(
                chat_id,
                (
                    "❌ <b>Не удалось создать напоминание</b>\n\n"
                    f"{html.escape(str(exc)[:500])}"
                ),
            )
            return True
        await telegram_state_service.clear_state(user_id)
        await _show_lead_details(chat_id, lead_id, return_page=int(page_raw))
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

    if mode == "awaiting_calendar_title":
        title = text.strip()
        if not title:
            await telegram_service.send_message(chat_id, "Название события пустое.")
            return True
        await telegram_state_service.set_state(
            user_id,
            {**state, "mode": "awaiting_calendar_date", "calendar_title": title},
            ttl_seconds=settings.telegram_state_ttl_minutes * 60,
        )
        await telegram_service.send_message(
            chat_id,
            "🕒 <b>Шаг 2 из 3 · Дата и время</b>\n\nВыберите вариант или введите свою дату:",
            reply_markup={
                "inline_keyboard": [
                    [
                        {"text": "Сегодня 17:00", "callback_data": "caldate:today17"},
                        {"text": "Завтра 10:00", "callback_data": "caldate:tomorrow10"},
                    ],
                    [
                        {"text": "Завтра 15:00", "callback_data": "caldate:tomorrow15"},
                        {"text": "Другая дата", "callback_data": "caldate:custom"},
                    ],
                    [{"text": "❌ Отмена", "callback_data": "state:cancel"}],
                ]
            },
        )
        return True

    if mode in {"awaiting_calendar_date", "awaiting_calendar_custom_date"}:
        try:
            start_dt = _parse_manager_datetime(text)
        except ValueError as exc:
            await telegram_service.send_message(chat_id, f"❌ {html.escape(str(exc))}")
            return True
        await telegram_state_service.set_state(
            user_id,
            {
                **state,
                "mode": "awaiting_calendar_duration",
                "start_iso": start_dt.isoformat(),
                "start_display": _format_manager_datetime(start_dt),
            },
            ttl_seconds=settings.telegram_state_ttl_minutes * 60,
        )
        await telegram_service.send_message(
            chat_id,
            "⏱ <b>Шаг 3 из 3 · Длительность</b>\n\nВыберите продолжительность:",
            reply_markup={
                "inline_keyboard": [
                    [
                        {"text": "15 мин", "callback_data": "caldur:15"},
                        {"text": "30 мин", "callback_data": "caldur:30"},
                    ],
                    [
                        {"text": "45 мин", "callback_data": "caldur:45"},
                        {"text": "60 мин", "callback_data": "caldur:60"},
                    ],
                    [{"text": "❌ Отмена", "callback_data": "state:cancel"}],
                ]
            },
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

        if text.startswith("/calendar_test"):
            await _handle_calendar_test(chat_id, user_id)
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
                "awaiting_calendar_title",
                "awaiting_calendar_date",
                "awaiting_calendar_custom_date",
                "awaiting_calendar_duration",
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
            if state and state.get("mode") == "awaiting_audio_for_lead":
                target_kommo_lead_id = int(state.get("kommo_lead_id") or 0) or None
                target_lead_name = str(state.get("lead_name") or "") or None

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
            }
            processing_mode = (
                (settings.audio_processing_mode or "direct").strip().lower()
            )

            if processing_mode != "celery":
                logger.info(
                    "Telegram audio started in direct mode: user_id=%s message_id=%s target_kommo_lead_id=%s",
                    user_id,
                    message_id,
                    target_kommo_lead_id,
                )
                _spawn_background(process_voice_note_async(**process_kwargs))
                if target_kommo_lead_id:
                    await telegram_state_service.clear_state(user_id)
                    await telegram_service.send_message(
                        chat_id,
                        (
                            "🎙 Аудио получено. Начинаю обработку для существующей сделки.\n"
                            f"Сделка: <b>{html.escape(target_lead_name or str(target_kommo_lead_id))}</b>\n"
                            f"ID: <code>{target_kommo_lead_id}</code>"
                        ),
                    )
                else:
                    await telegram_service.send_message(
                        chat_id,
                        "🎙 Аудио получено. Начинаю расшифровку и анализ нового лида.",
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
                if target_kommo_lead_id:
                    await telegram_state_service.clear_state(user_id)
                    await telegram_service.send_message(
                        chat_id,
                        (
                            "🎙 Аудио поставлено в очередь для обновления существующей сделки.\n"
                            f"Сделка: <b>{html.escape(target_lead_name or str(target_kommo_lead_id))}</b>\n"
                            f"ID: <code>{target_kommo_lead_id}</code>\n\n"
                            "Если Celery worker не заберёт задачу, бот автоматически запустит резервную обработку."
                        ),
                    )
                else:
                    await telegram_service.send_message(
                        chat_id,
                        (
                            "🎙 Аудио получено и поставлено в очередь на обработку нового лида.\n\n"
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
