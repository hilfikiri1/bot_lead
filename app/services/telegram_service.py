"""Telegram Bot API transport and polished Russian-language CRM interface."""

from __future__ import annotations

import asyncio
import html
from datetime import datetime, timezone
from typing import Any

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from app.config import get_settings

settings = get_settings()
TELEGRAM_API = f"https://api.telegram.org/bot{settings.telegram_bot_token}"
TELEGRAM_FILE_API = f"https://api.telegram.org/file/bot{settings.telegram_bot_token}"
TELEGRAM_MESSAGE_LIMIT = 4096
SAFE_MESSAGE_CHUNK = 3600


def _telegram_error(response: httpx.Response, action: str) -> RuntimeError:
    try:
        payload = response.json()
        description = payload.get("description") or str(payload)
    except Exception:
        description = response.text[:300]
    return RuntimeError(
        f"Telegram {action} failed (HTTP {response.status_code}): {description}"
    )


def _ensure_success(response: httpx.Response, action: str) -> None:
    if not 200 <= response.status_code < 300:
        raise _telegram_error(response, action)


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=10))
async def send_message(
    chat_id: int,
    text: str,
    parse_mode: str = "HTML",
    reply_markup: dict[str, Any] | None = None,
    disable_web_page_preview: bool = True,
) -> dict:
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": disable_web_page_preview,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(f"{TELEGRAM_API}/sendMessage", json=payload)
        _ensure_success(response, "sendMessage")
        return response.json()


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=10))
async def send_document(
    chat_id: int,
    *,
    filename: str,
    content: bytes,
    caption: str | None = None,
) -> dict:
    data = {"chat_id": str(chat_id)}
    if caption:
        data["caption"] = caption
        data["parse_mode"] = "HTML"
    files = {"document": (filename, content, "text/calendar")}
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(f"{TELEGRAM_API}/sendDocument", data=data, files=files)
        _ensure_success(response, "sendDocument")
        return response.json()


async def send_calendar_result(
    chat_id: int,
    *,
    title: str,
    start_iso: str,
    duration_minutes: int = 30,
    description: str = "",
    start_display: str | None = None,
    lead_name: str | None = None,
    reminder_minutes: int | None = None,
) -> str:
    """Create a calendar event (or send .ics) and return a short status line."""
    from app.services import calendar_service

    result = await asyncio.to_thread(
        calendar_service.create_event_with_fallback,
        title,
        description,
        start_iso,
        duration_minutes,
        None,
        reminder_minutes,
    )
    if result["success"]:
        lines = [
            f"✅ <b>Событие создано в {html.escape(result['provider'])}</b>",
            "",
        ]
        if lead_name:
            lines.append(f"Сделка: {html.escape(lead_name)}")
        lines.extend(
            [
                f"Название: {html.escape(title)}",
                f"Начало: {html.escape(start_display or start_iso)}",
            ]
        )
        if result.get("event_url"):
            lines.append(
                f"<a href=\"{html.escape(str(result['event_url']), quote=True)}\">Открыть Google Calendar</a>"
            )
        elif result.get("event_id"):
            lines.append(f"Event ID: <code>{html.escape(str(result['event_id']))}</code>")
        await send_message(chat_id, "\n".join(lines))
        return "Событие добавлено в календарь."

    await send_message(
        chat_id,
        (
            "⚠️ <b>Автоматически добавить событие не удалось.</b>\n\n"
            f"{html.escape(str(result.get('error') or 'Неизвестная ошибка'))}\n\n"
            "Я подготовил файл календаря. Откройте его и подтвердите добавление "
            "события вручную в Google Calendar или Apple Calendar."
        ),
    )
    ics_bytes = str(result.get("ics_content") or "").encode("utf-8")
    if ics_bytes:
        await send_document(
            chat_id,
            filename="reminder.ics",
            content=ics_bytes,
            caption=f"📅 {html.escape(title)}",
        )
        return "Отправлен файл .ics для ручного добавления."
    return "Не удалось создать событие в календаре."


async def send_calendar_event_type_picker(
    chat_id: int,
    *,
    lead_id: int,
    lead_name: str,
    return_page: int = 1,
) -> dict:
    return await send_message(
        chat_id,
        (
            "📅 <b>Запланировать</b>\n\n"
            f"Сделка: <b>{_esc(lead_name)}</b>\n\n"
            "Выберите тип действия:"
        ),
        reply_markup={
            "inline_keyboard": [
                [{"text": "📞 Созвон", "callback_data": f"calevt:call:{lead_id}:{return_page}"}],
                [{"text": "🤝 Встреча", "callback_data": f"calevt:meeting:{lead_id}:{return_page}"}],
                [
                    {
                        "text": "💬 Написать клиенту",
                        "callback_data": f"calevt:message:{lead_id}:{return_page}",
                    }
                ],
                [
                    {
                        "text": "📦 Отправить предложение",
                        "callback_data": f"calevt:proposal:{lead_id}:{return_page}",
                    }
                ],
                [{"text": "📝 Другое", "callback_data": f"calevt:other:{lead_id}:{return_page}"}],
                [
                    {
                        "text": "⬅️ Назад",
                        "callback_data": f"lead:view:{lead_id}:{return_page}",
                    }
                ],
            ]
        },
    )


async def send_calendar_date_picker(
    chat_id: int,
    *,
    lead_id: int,
    event_type_label: str,
    return_page: int = 1,
) -> dict:
    return await send_message(
        chat_id,
        (
            "📅 <b>Дата</b>\n\n"
            f"Событие: {_esc(event_type_label)}\n\n"
            "Выберите дату:"
        ),
        reply_markup={
            "inline_keyboard": [
                [
                    {"text": "Сегодня", "callback_data": f"calday:today:{lead_id}:{return_page}"},
                    {"text": "Завтра", "callback_data": f"calday:tomorrow:{lead_id}:{return_page}"},
                ],
                [
                    {
                        "text": "Послезавтра",
                        "callback_data": f"calday:dayafter:{lead_id}:{return_page}",
                    }
                ],
                [
                    {
                        "text": "Выбрать дату",
                        "callback_data": f"calday:custom:{lead_id}:{return_page}",
                    }
                ],
                [
                    {
                        "text": "⬅️ Назад",
                        "callback_data": f"lead:calendar:{lead_id}:{return_page}",
                    }
                ],
            ]
        },
    )


async def send_calendar_time_picker(
    chat_id: int,
    *,
    lead_id: int,
    return_page: int = 1,
) -> dict:
    return await send_message(
        chat_id,
        "🕒 <b>Время</b>\n\nВыберите время:",
        reply_markup={
            "inline_keyboard": [
                [
                    {"text": "09:00", "callback_data": f"caltime:time09:{lead_id}:{return_page}"},
                    {"text": "10:00", "callback_data": f"caltime:time10:{lead_id}:{return_page}"},
                    {"text": "11:00", "callback_data": f"caltime:time11:{lead_id}:{return_page}"},
                ],
                [
                    {"text": "12:00", "callback_data": f"caltime:time12:{lead_id}:{return_page}"},
                    {"text": "14:00", "callback_data": f"caltime:time14:{lead_id}:{return_page}"},
                    {"text": "15:00", "callback_data": f"caltime:time15:{lead_id}:{return_page}"},
                ],
                [{"text": "16:00", "callback_data": f"caltime:time16:{lead_id}:{return_page}"}],
                [
                    {
                        "text": "Другое время",
                        "callback_data": f"caltime:custom:{lead_id}:{return_page}",
                    }
                ],
                [
                    {
                        "text": "⬅️ Назад",
                        "callback_data": f"lead:calendar:{lead_id}:{return_page}",
                    }
                ],
            ]
        },
    )


async def send_calendar_duration_picker(
    chat_id: int,
    *,
    lead_id: int,
    return_page: int = 1,
) -> dict:
    return await send_message(
        chat_id,
        "⏱ <b>Продолжительность</b>\n\nВыберите длительность:",
        reply_markup={
            "inline_keyboard": [
                [
                    {"text": "15 минут", "callback_data": f"caldur:15:{lead_id}:{return_page}"},
                    {"text": "30 минут", "callback_data": f"caldur:30:{lead_id}:{return_page}"},
                ],
                [
                    {"text": "45 минут", "callback_data": f"caldur:45:{lead_id}:{return_page}"},
                    {"text": "60 минут", "callback_data": f"caldur:60:{lead_id}:{return_page}"},
                ],
                [
                    {
                        "text": "Другая",
                        "callback_data": f"caldur:custom:{lead_id}:{return_page}",
                    }
                ],
                [
                    {
                        "text": "⬅️ Назад",
                        "callback_data": f"lead:calendar:{lead_id}:{return_page}",
                    }
                ],
            ]
        },
    )


async def send_calendar_reminder_picker(
    chat_id: int,
    *,
    lead_id: int,
    return_page: int = 1,
) -> dict:
    return await send_message(
        chat_id,
        "🔔 <b>Напоминание</b>\n\nЗа сколько напомнить?",
        reply_markup={
            "inline_keyboard": [
                [
                    {"text": "За 10 минут", "callback_data": f"calrem:10:{lead_id}:{return_page}"},
                    {"text": "За 30 минут", "callback_data": f"calrem:30:{lead_id}:{return_page}"},
                ],
                [
                    {"text": "За 1 час", "callback_data": f"calrem:60:{lead_id}:{return_page}"},
                    {"text": "За 1 день", "callback_data": f"calrem:1440:{lead_id}:{return_page}"},
                ],
                [{"text": "Без напоминания", "callback_data": f"calrem:0:{lead_id}:{return_page}"}],
                [
                    {
                        "text": "⬅️ Назад",
                        "callback_data": f"lead:calendar:{lead_id}:{return_page}",
                    }
                ],
            ]
        },
    )


async def send_calendar_preview(
    chat_id: int,
    *,
    lead_id: int,
    return_page: int,
    preview: dict[str, Any],
) -> dict:
    will_calendar = "✅ событие в Google Calendar" if preview.get("needs_calendar") else "— без события в календаре"
    will_task = "✅ задача в Kommo" if preview.get("needs_kommo_task") else "— без задачи Kommo"
    text = (
        "📅 <b>ПРОВЕРКА СОБЫТИЯ</b>\n\n"
        f"Сделка: {_esc(preview.get('lead_name'))}\n"
        f"Событие: {_esc(preview.get('event_label'))}\n"
        f"Дата: {_esc(preview.get('date_display'))}\n"
        f"Время: {_esc(preview.get('time_display'))}\n"
        f"Часовой пояс: {_esc(preview.get('timezone'))}\n"
        f"Продолжительность: {_esc(preview.get('duration_label'))}\n"
        f"Напоминание: {_esc(preview.get('reminder_label'))}\n\n"
        f"Будет создано:\n{will_calendar}\n{will_task}"
    )
    return await send_message(
        chat_id,
        text,
        reply_markup={
            "inline_keyboard": [
                [
                    {
                        "text": "✅ Создать",
                        "callback_data": f"calendar:confirm:{lead_id}:{return_page}",
                    }
                ],
                [
                    {
                        "text": "✏️ Изменить",
                        "callback_data": f"calendar:edit:{lead_id}:{return_page}",
                    },
                    {
                        "text": "❌ Отмена",
                        "callback_data": f"calendar:cancel:{lead_id}:{return_page}",
                    },
                ],
            ]
        },
    )


async def send_calendar_success(
    chat_id: int,
    *,
    lead_id: int,
    return_page: int,
    result: dict[str, Any],
) -> dict:
    from app.services import calendar_event_builder

    start = result.get("start_at")
    end = result.get("end_at")
    if start and end:
        when = calendar_event_builder.format_time_range(start, end)
    else:
        when = "—"
    lines = [
        "✅ <b>СОБЫТИЕ СОЗДАНО</b>",
        "",
        f"<b>{_esc(result.get('title'))}</b>",
        when,
        _esc(result.get("timezone")),
        "",
    ]
    if result.get("calendar_success"):
        lines.append("✅ Google Calendar")
    elif result.get("ics_fallback"):
        lines.append("⚠️ Google Calendar — отправлен файл .ics")
    else:
        lines.append(f"❌ Google Calendar — {_esc(result.get('calendar_error') or 'ошибка')}")
    if result.get("kommo_task_success"):
        lines.append("✅ Задача Kommo")
    elif result.get("kommo_task_error"):
        lines.append(f"❌ Задача Kommo — {_esc(result.get('kommo_task_error'))}")
    keyboard: list[list[dict[str, Any]]] = []
    if result.get("calendar_event_url"):
        keyboard.append(
            [{"text": "📅 Открыть календарь", "url": result["calendar_event_url"]}]
        )
    if result.get("lead_url"):
        keyboard.append([{"text": "🔗 Открыть Kommo", "url": result["lead_url"]}])
    keyboard.extend(
        [
            [
                {
                    "text": "📋 Карточка сделки",
                    "callback_data": f"lead:view:{lead_id}:{return_page}",
                }
            ],
            [{"text": "🏠 Главное меню", "callback_data": "menu:home"}],
        ]
    )
    if result.get("calendar_success") and result.get("kommo_task_error"):
        keyboard.insert(
            0,
            [
                {
                    "text": "🔄 Повторить задачу Kommo",
                    "callback_data": f"calendar:retry_kommo:{lead_id}:{return_page}",
                }
            ],
        )
    message = await send_message(
        chat_id, "\n".join(lines), reply_markup={"inline_keyboard": keyboard}
    )
    if result.get("ics_fallback") and result.get("ics_content"):
        await send_document(
            chat_id,
            filename="event.ics",
            content=str(result["ics_content"]).encode("utf-8"),
            caption=f"📅 {_esc(result.get('title'))}",
        )
    return message


async def send_message_chunks(
    chat_id: int,
    chunks: list[str],
    parse_mode: str = "HTML",
) -> None:
    for chunk in chunks:
        if not chunk:
            continue
        if len(chunk) <= TELEGRAM_MESSAGE_LIMIT:
            await send_message(chat_id, chunk, parse_mode=parse_mode)
            continue
        for offset in range(0, len(chunk), SAFE_MESSAGE_CHUNK):
            await send_message(
                chat_id,
                chunk[offset : offset + SAFE_MESSAGE_CHUNK],
                parse_mode=parse_mode,
            )


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=10))
async def send_report(
    chat_id: int,
    report_text: str,
    lead_id: int,
    voice_note_id: int,
    target_kommo_lead_id: int | None = None,
) -> dict:
    if target_kommo_lead_id:
        primary = {
            "text": "✅ Добавить к сделке",
            "callback_data": (
                f"action:kommo_update:{lead_id}:{voice_note_id}:{target_kommo_lead_id}"
            ),
        }
    else:
        primary = {
            "text": "📥 Подготовить новый лид",
            "callback_data": f"leadcreate:preview:{lead_id}:{voice_note_id}",
        }

    keyboard = {
        "inline_keyboard": [
            [primary],
            [
                {
                    "text": "💬 Текст клиенту",
                    "callback_data": f"action:whatsapp:{lead_id}:{voice_note_id}",
                },
                {
                    "text": "📅 Следующий контакт",
                    "callback_data": f"action:calendar:{lead_id}:{voice_note_id}",
                },
            ],
            [
                {
                    "text": "✉️ Черновик email",
                    "callback_data": f"action:gmail:{lead_id}:{voice_note_id}",
                },
                {
                    "text": "❌ Закрыть",
                    "callback_data": f"action:cancel:{lead_id}:{voice_note_id}",
                },
            ],
        ]
    }
    return await send_message(chat_id, report_text, reply_markup=keyboard)


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=10))
async def answer_callback_query(callback_query_id: str, text: str = "") -> dict:
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.post(
            f"{TELEGRAM_API}/answerCallbackQuery",
            json={
                "callback_query_id": callback_query_id,
                "text": text,
                "show_alert": bool(text),
            },
        )
        _ensure_success(response, "answerCallbackQuery")
        return response.json()


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=10))
async def get_file_path(file_id: str) -> str:
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.get(
            f"{TELEGRAM_API}/getFile", params={"file_id": file_id}
        )
        _ensure_success(response, "getFile")
        return response.json()["result"]["file_path"]


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=10))
async def download_file(file_path: str) -> bytes:
    async with httpx.AsyncClient(timeout=90) as client:
        response = await client.get(f"{TELEGRAM_FILE_API}/{file_path}")
        _ensure_success(response, "downloadFile")
        return response.content


async def download_voice(file_id: str) -> bytes:
    return await download_file(await get_file_path(file_id))


async def delete_webhook() -> dict:
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.post(
            f"{TELEGRAM_API}/deleteWebhook",
            json={"drop_pending_updates": False},
        )
        _ensure_success(response, "deleteWebhook")
        return response.json()


async def register_webhook(url: str) -> dict:
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.post(
            f"{TELEGRAM_API}/setWebhook",
            json={
                "url": url,
                "secret_token": settings.telegram_webhook_secret,
                "allowed_updates": ["message", "callback_query"],
            },
        )
        _ensure_success(response, "setWebhook")
        return response.json()


async def set_bot_commands() -> dict:
    commands = [
        {"command": "menu", "description": "Главное меню"},
        {"command": "jobs", "description": "Статус обработки аудио"},
        {"command": "digest", "description": "Приоритеты Kommo и задачи дня"},
        {"command": "notion_test", "description": "Проверить новую структуру Notion"},
        {"command": "sync_leads", "description": "Синхронизировать Kommo → Notion"},
        {"command": "errors", "description": "Последние ошибки интеграций"},
        {"command": "kommo_test", "description": "Проверить Kommo"},
        {"command": "calendar_test", "description": "Проверить Google Calendar"},
        {"command": "calendar_test_write", "description": "Тест записи в Google Calendar"},
    ]
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.post(
            f"{TELEGRAM_API}/setMyCommands", json={"commands": commands}
        )
        _ensure_success(response, "setMyCommands")
        return response.json()


def _esc(value: Any) -> str:
    return html.escape(str(value if value not in (None, "") else "—"))


def _format_unix_timestamp(value: Any) -> str:
    if not value:
        return "—"
    try:
        return datetime.fromtimestamp(int(value), tz=timezone.utc).strftime(
            "%d.%m.%Y %H:%M UTC"
        )
    except (TypeError, ValueError, OSError):
        return str(value)


def _list(items: list[Any] | None, *, empty: str = "Не указано", limit: int = 8) -> str:
    values = [str(item).strip() for item in (items or []) if str(item).strip()]
    if not values:
        return f"<i>{_esc(empty)}</i>"
    return "\n".join(f"• {_esc(item)}" for item in values[:limit])


def format_report(report: dict, transcript: str) -> str:
    """Compact professional manager card; manager content is Russian."""
    client = report.get("client") or {}
    lead = report.get("lead") or {}
    whatsapp = report.get("whatsapp") or {}
    task = report.get("manager_task") or {}
    facts = report.get("confirmed_facts") or report.get("what_manager_said") or []
    risks = report.get("risks") or report.get("mistakes_or_weak_points") or []
    confidence = float(report.get("confidence_score") or 0)
    confidence_badge = (
        "🟢" if confidence >= 0.8 else "🟡" if confidence >= 0.6 else "🔴"
    )
    review = (
        "Нужна проверка"
        if report.get("needs_human_review")
        else "Данные достаточно полные"
    )

    client_label = client.get("name") or client.get("company") or "Не определён"
    location = (
        " / ".join(
            str(value) for value in (lead.get("country"), lead.get("city")) if value
        )
        or "—"
    )
    task_text = task.get("title") or report.get("recommended_next_step") or "—"
    due = task.get("due_at") or "дата не определена"
    polish_text = whatsapp.get("message") or "Черновик не сформирован"

    return (
        "✨ <b>BBS • Анализ разговора</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"{confidence_badge} Уверенность: <b>{confidence:.0%}</b> · {_esc(review)}\n\n"
        f"👤 <b>{_esc(client_label)}</b>\n"
        f"Компания: {_esc(client.get('company'))}\n"
        f"Телефон: {_esc(client.get('phone'))}\n"
        f"Email: {_esc(client.get('email'))}\n\n"
        f"📦 <b>{_esc(lead.get('product_requested'))}</b>\n"
        f"Количество: {_esc(lead.get('quantity'))}\n"
        f"Бюджет: {_esc(lead.get('budget'))}\n"
        f"Локация: {_esc(location)}\n"
        f"Сроки: {_esc(lead.get('timeline'))}\n\n"
        f"🧾 <b>Кратко</b>\n{_esc(report.get('conversation_summary'))}\n\n"
        f"✅ <b>Что подтверждено</b>\n{_list(facts)}\n\n"
        f"❓ <b>Что ещё выяснить</b>\n{_list(report.get('missing_questions'))}\n\n"
        f"⚠️ <b>Риски</b>\n{_list(risks, empty='Явных рисков не выявлено')}\n\n"
        f"🎯 <b>Следующий шаг</b>\n{_esc(report.get('recommended_next_step'))}\n\n"
        f"✅ <b>Задача менеджеру</b>\n{_esc(task_text)}\n"
        f"Срок: {_esc(due)}\n\n"
        f"🇵🇱 <b>Сообщение клиенту</b>\n<pre>{_esc(polish_text[:1300])}</pre>"
    )


async def send_processing_step(
    chat_id: int,
    step: str,
    *,
    target_kommo_lead_id: int | None = None,
    command_mode: bool = False,
) -> dict:
    if command_mode:
        steps = {
            "download": "📥 <b>Аудио получено</b>\nСкачиваю файл…",
            "transcribe": "🎧 Расшифровываю команду…",
        }
    else:
        steps = {
            "download": "📥 <b>Аудио получено</b>\nСкачиваю файл и проверяю формат…",
            "transcribe": "🎧 Расшифровываю сообщение…",
            "analyze": "🧠 <b>Шаг 2 из 3</b>\nВыделяю факты, вопросы и следующий шаг…",
            "save": "💾 <b>Шаг 3 из 3</b>\nСохраняю результат и готовлю карточку…",
        }
    suffix = (
        f"\n\nСделка Kommo: <code>{target_kommo_lead_id}</code>"
        if target_kommo_lead_id
        else ""
    )
    return await send_message(chat_id, steps.get(step, "⏳ Обрабатываю…") + suffix)


async def send_main_menu(chat_id: int) -> dict:
    keyboard = {
        "inline_keyboard": [
            [
                {"text": "🎙 Новый разговор", "callback_data": "menu:new"},
                {"text": "🔎 Найти сделку", "callback_data": "menu:search"},
            ],
            [
                {"text": "📋 Открытые сделки", "callback_data": "menu:leads:1"},
                {"text": "🧭 Статус аудио", "callback_data": "menu:jobs"},
            ],
            [
                {"text": "📝 Дополнить сделку", "callback_data": "menu:update"},
                {"text": "🔌 Проверить Kommo", "callback_data": "menu:test"},
            ],
            [
                {"text": "📅 Проверить календарь", "callback_data": "menu:calendar"},
                {"text": "☀️ Дайджест", "callback_data": "menu:digest"},
                {"text": "📓 Notion", "callback_data": "menu:notion"},
            ],
            [
                {
                    "text": "📥 Неразобранные сделки",
                    "callback_data": "menu:unrev:1",
                },
            ],
        ]
    }
    return await send_message(
        chat_id,
        (
            "✨ <b>BBS • CRM Assistant</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "Голосовые сообщения — ваши команды боту: календарь, напоминания, поиск.\n\n"
            "Чтобы записать <b>разговор с клиентом</b> и создать лид — "
            "нажмите <b>🎙 Новый разговор</b>.\n"
            "Чтобы дополнить сделку — найдите её и выберите действие в карточке."
        ),
        reply_markup=keyboard,
    )


def _short_button_title(value: Any, limit: int = 38) -> str:
    text = str(value or "Без названия").replace("\n", " ").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


async def send_lead_selection_menu(
    chat_id: int,
    result: dict[str, Any],
    *,
    page: int = 1,
    search_mode: bool = False,
) -> dict:
    leads = result.get("leads") or []
    total = result.get("open_count", len(leads))
    total_pages = result.get("total_pages", 1)
    page = result.get("page", page)
    if not leads:
        return await send_message(
            chat_id,
            "🔎 <b>Ничего не найдено</b>\n\nПопробуйте номер или часть названия товара.",
            reply_markup={
                "inline_keyboard": [
                    [{"text": "🔎 Повторить поиск", "callback_data": "menu:search"}],
                    [{"text": "🏠 Главное меню", "callback_data": "menu:home"}],
                ]
            },
        )

    rows: list[list[dict[str, Any]]] = []
    for lead in leads:
        lead_id = lead.get("id")
        if isinstance(lead_id, int):
            rows.append(
                [
                    {
                        "text": f"{_short_button_title(lead.get('name'))}",
                        "callback_data": f"lead:view:{lead_id}:{page}",
                    }
                ]
            )
    if not search_mode and total_pages > 1:
        nav: list[dict[str, Any]] = []
        if page > 1:
            nav.append({"text": "←", "callback_data": f"menu:leads:{page - 1}"})
        nav.append({"text": f"{page} / {total_pages}", "callback_data": "noop"})
        if page < total_pages:
            nav.append({"text": "→", "callback_data": f"menu:leads:{page + 1}"})
        rows.append(nav)
    rows.append(
        [
            {"text": "🔎 Поиск", "callback_data": "menu:search"},
            {"text": "🏠 Меню", "callback_data": "menu:home"},
        ]
    )
    title = "Результаты поиска" if search_mode else "Открытые сделки"
    pipeline_name = result.get("pipeline_name")
    pipeline_suffix = (
        f" · воронка <b>{_esc(pipeline_name)}</b>" if pipeline_name else ""
    )
    subtitle = (
        f"Найдено: <b>{total}</b>{pipeline_suffix}"
        if search_mode
        else f"Всего: <b>{total}</b>{pipeline_suffix} · страница {page}/{total_pages}"
    )
    return await send_message(
        chat_id,
        f"📋 <b>{title}</b>\n{subtitle}\n\nВыберите сделку:",
        reply_markup={"inline_keyboard": rows},
    )


def format_lead_details(details: dict[str, Any]) -> str:
    contacts = details.get("contacts") or []
    contact = contacts[0] if contacts else {}
    phones = ", ".join(contact.get("phones") or []) or "—"
    emails = ", ".join(contact.get("emails") or []) or "—"

    fields = details.get("custom_fields") or []
    field_lines = [
        f"• {_esc(item.get('name'))}: {_esc(item.get('value'))}" for item in fields[:8]
    ]
    fields_text = (
        "\n".join(field_lines)
        if field_lines
        else "<i>Дополнительные поля не заполнены</i>"
    )

    notes = details.get("notes") or []
    note_lines: list[str] = []
    for note in notes[:2]:
        text = " ".join(str(note.get("text") or "").split())
        note_lines.append(f"• {_esc(text[:420])}")
    notes_text = "\n".join(note_lines) if note_lines else "<i>Примечаний пока нет</i>"

    return (
        "📌 <b>Карточка сделки</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"<b>{_esc(details.get('name'))}</b>\n"
        f"ID: <code>{_esc(details.get('id'))}</code>\n"
        f"Этап: <b>{_esc(details.get('status_name'))}</b>\n"
        f"Воронка: {_esc(details.get('pipeline_name'))}\n"
        f"Бюджет: {_esc(details.get('price'))}\n"
        f"Ближайшая задача: {_esc(_format_unix_timestamp(details.get('closest_task_at')))}\n\n"
        f"👤 <b>{_esc(contact.get('name') or 'Контакт не указан')}</b>\n"
        f"Телефон: {_esc(phones)}\n"
        f"Email: {_esc(emails)}\n\n"
        f"📄 <b>Данные сделки</b>\n{fields_text}\n\n"
        f"🗒 <b>Последние примечания</b>\n{notes_text}"
    )


async def send_lead_details(
    chat_id: int,
    details: dict[str, Any],
    *,
    return_page: int = 1,
) -> dict:
    lead_id = int(details["id"])
    keyboard = {
        "inline_keyboard": [
            [
                {
                    "text": "🎙 Новый разговор",
                    "callback_data": f"lead:audio:{lead_id}:{return_page}",
                },
                {
                    "text": "📝 Примечание",
                    "callback_data": f"lead:text:{lead_id}:{return_page}",
                },
            ],
            [
                {
                    "text": "✅ Задача",
                    "callback_data": f"lead:task:{lead_id}:{return_page}",
                },
                {
                    "text": "📅 Запланировать",
                    "callback_data": f"lead:calendar:{lead_id}:{return_page}",
                },
            ],
            [
                {
                    "text": "✏️ Редактировать",
                    "callback_data": f"lead:edit:{lead_id}:{return_page}",
                },
            ],
            [
                {"text": "🔗 Открыть Kommo", "url": details.get("url")},
                {"text": "↩️ К списку", "callback_data": f"menu:leads:{return_page}"},
            ],
            [{"text": "🏠 Главное меню", "callback_data": "menu:home"}],
        ]
    }
    return await send_message(
        chat_id, format_lead_details(details), reply_markup=keyboard
    )


async def send_lead_edit_preview(
    chat_id: int,
    *,
    lead_id: int,
    draft: dict[str, Any],
    original: dict[str, Any],
    return_page: int = 1,
) -> dict:
    changes: list[str] = []
    if draft.get("name") != original.get("name"):
        changes.append(
            f"• Название: {_esc(original.get('name'))} → <b>{_esc(draft.get('name'))}</b>"
        )
    if draft.get("price") != original.get("price"):
        changes.append(
            f"• Бюджет: {_esc(original.get('price'))} → <b>{_esc(draft.get('price'))}</b>"
        )
    if draft.get("status_id") != original.get("status_id"):
        changes.append(
            "• Этап: "
            f"{_esc(original.get('status_name'))} → "
            f"<b>{_esc(draft.get('status_name'))}</b>"
        )
    changes_text = (
        "\n".join(changes) if changes else "<i>Изменений пока нет</i>"
    )

    keyboard_rows: list[list[dict[str, Any]]] = [
        [
            {
                "text": "✏️ Название",
                "callback_data": f"leadedit:edit:name:{lead_id}:{return_page}",
            },
            {
                "text": "💰 Бюджет",
                "callback_data": f"leadedit:edit:price:{lead_id}:{return_page}",
            },
        ],
        [
            {
                "text": "📍 Этап",
                "callback_data": f"leadedit:edit:status:{lead_id}:{return_page}",
            }
        ],
    ]
    if changes:
        keyboard_rows.append(
            [
                {
                    "text": "✅ Сохранить в Kommo",
                    "callback_data": f"leadedit:confirm:{lead_id}:{return_page}",
                }
            ]
        )
    keyboard_rows.extend(
        [
            [
                {
                    "text": "↩️ К карточке",
                    "callback_data": f"lead:view:{lead_id}:{return_page}",
                }
            ],
            [{"text": "❌ Отмена", "callback_data": f"leadedit:cancel:{lead_id}:{return_page}"}],
        ]
    )

    return await send_message(
        chat_id,
        (
            "✏️ <b>Редактирование сделки</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"<b>{_esc(draft.get('name'))}</b>\n"
            f"ID: <code>{lead_id}</code>\n"
            f"Этап: <b>{_esc(draft.get('status_name'))}</b>\n"
            f"Бюджет: {_esc(draft.get('price'))}\n\n"
            f"Изменения:\n{changes_text}\n\n"
            "Выберите поле или сохраните изменения."
        ),
        reply_markup={"inline_keyboard": keyboard_rows},
    )


async def send_lead_status_picker(
    chat_id: int,
    *,
    lead_id: int,
    statuses: list[dict[str, Any]],
    return_page: int = 1,
) -> dict:
    rows: list[list[dict[str, Any]]] = []
    current_row: list[dict[str, Any]] = []
    for status in statuses:
        status_id = status.get("id")
        if not isinstance(status_id, int):
            continue
        current_row.append(
            {
                "text": _short_button_title(status.get("name"), limit=24),
                "callback_data": f"leadedit:status:{status_id}:{lead_id}:{return_page}",
            }
        )
        if len(current_row) == 2:
            rows.append(current_row)
            current_row = []
    if current_row:
        rows.append(current_row)
    rows.append(
        [
            {
                "text": "↩️ Назад",
                "callback_data": f"leadedit:back:{lead_id}:{return_page}",
            }
        ]
    )
    return await send_message(
        chat_id,
        "📍 <b>Выберите этап воронки</b>",
        reply_markup={"inline_keyboard": rows},
    )


async def send_kommo_creation_preview(chat_id: int, draft: dict[str, Any]) -> dict:
    keyboard = {
        "inline_keyboard": [
            [{"text": "✅ Создать лид", "callback_data": "leadcreate:confirm"}],
            [
                {"text": "🔢 Номер", "callback_data": "leadcreate:edit:number"},
                {"text": "✏️ Название", "callback_data": "leadcreate:edit:name"},
            ],
            [
                {"text": "👤 Клиент", "callback_data": "leadcreate:edit:client"},
                {"text": "📦 Товар", "callback_data": "leadcreate:edit:product"},
            ],
            [
                {"text": "💰 Бюджет", "callback_data": "leadcreate:edit:budget"},
                {"text": "📍 Город", "callback_data": "leadcreate:edit:city"},
            ],
            [{"text": "❌ Отмена", "callback_data": "leadcreate:cancel"}],
        ]
    }
    location = (
        " / ".join(
            str(value) for value in (draft.get("country"), draft.get("city")) if value
        )
        or "—"
    )
    return await send_message(
        chat_id,
        (
            "📥 <b>Проверка нового лида</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"Название: <b>{_esc(draft.get('lead_name'))}</b>\n"
            f"Номер: {_esc(draft.get('lead_number'))}\n"
            f"Клиент: {_esc(draft.get('client_name'))}\n"
            f"Компания: {_esc(draft.get('company'))}\n"
            f"Товар: {_esc(draft.get('product_requested'))}\n"
            f"Бюджет: {_esc(draft.get('budget'))}\n"
            f"Локация: {_esc(location)}\n\n"
            f"🎯 Следующий шаг: {_esc(draft.get('next_step'))}\n\n"
            f"Воронка: {_esc(draft.get('pipeline_name'))}\n"
            f"Этап: {_esc(draft.get('status_name'))}\n\n"
            "Данные попадут в Kommo только после подтверждения."
        ),
        reply_markup=keyboard,
    )


async def send_note_confirmation(
    chat_id: int,
    *,
    lead_id: int,
    lead_name: str,
    note_text: str,
    return_page: int = 1,
) -> dict:
    preview = note_text.strip()[:2500]
    return await send_message(
        chat_id,
        (
            "📝 <b>Проверка примечания</b>\n\n"
            f"Сделка: <b>{_esc(lead_name)}</b>\n\n"
            f"<pre>{_esc(preview)}</pre>"
        ),
        reply_markup={
            "inline_keyboard": [
                [
                    {
                        "text": "✅ Добавить",
                        "callback_data": f"note:confirm:{lead_id}:{return_page}",
                    },
                    {
                        "text": "❌ Отмена",
                        "callback_data": f"note:cancel:{lead_id}:{return_page}",
                    },
                ]
            ]
        },
    )


async def send_task_confirmation(
    chat_id: int,
    *,
    lead_id: int,
    lead_name: str,
    task_text: str,
    due_display: str,
    return_page: int = 1,
) -> dict:
    return await send_message(
        chat_id,
        (
            "✅ <b>Проверка задачи</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"Сделка: <b>{_esc(lead_name)}</b>\n"
            f"Срок: <b>{_esc(due_display)}</b>\n\n"
            f"{_esc(task_text)}"
        ),
        reply_markup={
            "inline_keyboard": [
                [
                    {
                        "text": "✅ Создать",
                        "callback_data": f"task:confirm:{lead_id}:{return_page}",
                    },
                    {
                        "text": "✏️ Изменить",
                        "callback_data": f"lead:task:{lead_id}:{return_page}",
                    },
                ],
                [
                    {
                        "text": "❌ Отмена",
                        "callback_data": f"task:cancel:{lead_id}:{return_page}",
                    }
                ],
            ]
        },
    )


async def send_calendar_confirmation(
    chat_id: int,
    *,
    lead_id: int,
    lead_name: str,
    title: str,
    start_display: str,
    duration_minutes: int,
    return_page: int = 1,
    reminder_minutes: int = 30,
    event_type_label: str = "Созвон с клиентом",
    timezone_label: str = "Europe/Warsaw",
    needs_calendar: bool = True,
    needs_kommo_task: bool = True,
) -> dict:
    return await send_calendar_preview(
        chat_id,
        lead_id=lead_id,
        return_page=return_page,
        preview={
            "lead_name": lead_name,
            "event_label": event_type_label or title,
            "date_display": start_display.split(",")[0] if "," in start_display else start_display,
            "time_display": start_display,
            "timezone": timezone_label,
            "duration_label": f"{duration_minutes} минут",
            "reminder_label": (
                "без напоминания"
                if reminder_minutes <= 0
                else f"за {reminder_minutes} минут"
            ),
            "needs_calendar": needs_calendar,
            "needs_kommo_task": needs_kommo_task,
        },
    )


def format_audio_jobs(jobs: list[Any]) -> str:
    labels = {
        "received": "📥 Получено",
        "downloading": "⬇️ Скачивание",
        "transcribing": "🎧 Транскрибация",
        "analyzing": "🧠 Анализ",
        "saving": "💾 Сохранение",
        "ready": "✅ Готово",
        "failed": "❌ Ошибка",
    }
    if not jobs:
        return "🧭 <b>Статус обработки</b>\n\nАудиозаписей пока нет."
    lines = ["🧭 <b>Последние аудиозаписи</b>", "━━━━━━━━━━━━━━━━━━"]
    for job in jobs:
        created = job.created_at.strftime("%d.%m %H:%M") if job.created_at else "—"
        block = (
            f"{labels.get(job.processing_status, job.processing_status)} · {created}\n"
            f"ID сообщения: <code>{job.telegram_message_id or '—'}</code>"
        )
        if job.processing_status == "failed" and getattr(job, "processing_error", None):
            block += f"\nОшибка: <code>{_esc(job.processing_error[:300])}</code>"
        lines.append(block)
    return "\n\n".join(lines)


def _unreviewed_lead_button_label(lead: dict[str, Any]) -> str:
    name = _short_button_title(lead.get("name"), limit=24)
    contact = lead.get("contact_name")
    if contact:
        suffix = _short_button_title(contact, limit=12)
        label = f"{name} · {suffix}"
    else:
        label = name
    return label if len(label) <= 38 else label[:37] + "…"


async def send_unreviewed_lead_selection_menu(
    chat_id: int,
    result: dict[str, Any],
    *,
    page: int = 1,
) -> dict:
    leads = result.get("leads") or []
    total = result.get("open_count", len(leads))
    total_pages = result.get("total_pages", 1)
    page = result.get("page", page)
    if not leads:
        status_label = result.get("status_label") or "Неразобранное"
        pipeline_name = result.get("pipeline_name")
        pipeline_hint = (
            f" воронки <b>{_esc(pipeline_name)}</b>" if pipeline_name else ""
        )
        return await send_message(
            chat_id,
            (
                "📥 <b>Неразобранные сделки</b>\n\n"
                f"В разделе <b>{_esc(status_label)}</b>{pipeline_hint} "
                "сейчас нет открытых заявок."
            ),
            reply_markup={
                "inline_keyboard": [
                    [{"text": "🔄 Обновить", "callback_data": "menu:unrev:1"}],
                    [{"text": "🏠 Главное меню", "callback_data": "menu:home"}],
                ]
            },
        )

    rows: list[list[dict[str, Any]]] = []
    for lead in leads:
        lead_id = lead.get("id")
        if isinstance(lead_id, int):
            rows.append(
                [
                    {
                        "text": _unreviewed_lead_button_label(lead),
                        "callback_data": f"unrev:view:{lead_id}:{page}",
                    }
                ]
            )
    if total_pages > 1:
        nav: list[dict[str, Any]] = []
        if page > 1:
            nav.append({"text": "⬅️ Назад", "callback_data": f"menu:unrev:{page - 1}"})
        nav.append({"text": f"{page}/{total_pages}", "callback_data": "noop"})
        if page < total_pages:
            nav.append({"text": "➡️ Далее", "callback_data": f"menu:unrev:{page + 1}"})
        rows.append(nav)
    rows.append([{"text": "🏠 Главное меню", "callback_data": "menu:home"}])

    pipeline_name = result.get("pipeline_name")
    status_label = result.get("status_label") or "Неразобранное"
    pipeline_suffix = (
        f" · воронка <b>{_esc(pipeline_name)}</b>" if pipeline_name else ""
    )
    lines = [
        "📥 <b>Неразобранные сделки</b>",
        f"Этап: <b>{_esc(status_label)}</b>",
        f"Всего: <b>{total}</b>{pipeline_suffix} · стр. {page}/{total_pages}",
        "",
    ]
    for lead in leads[:5]:
        phones = ", ".join(lead.get("phones") or []) or "—"
        created = _format_unix_timestamp(lead.get("created_at"))
        lines.append(
            f"• <b>{_esc(lead.get('name'))}</b>\n"
            f"  ID <code>{_esc(lead.get('id'))}</code> · "
            f"{_esc(lead.get('contact_name') or '—')} · "
            f"{_esc(phones)} · {created}"
        )
    if len(leads) > 5:
        lines.append(f"<i>…и ещё {len(leads) - 5} на этой странице</i>")
    lines.append("\nВыберите сделку:")
    return await send_message(
        chat_id,
        "\n".join(lines),
        reply_markup={"inline_keyboard": rows},
    )


def format_unreviewed_lead_card(details: dict[str, Any]) -> str:
    contacts = details.get("contacts") or []
    contact = contacts[0] if contacts else {}
    phones = ", ".join(contact.get("phones") or []) or "—"
    emails = ", ".join(contact.get("emails") or []) or "—"
    created = _format_unix_timestamp(details.get("created_at"))
    return (
        "📥 <b>НЕРАЗОБРАННАЯ СДЕЛКА</b>\n\n"
        f"Название: {_esc(details.get('name'))}\n"
        f"Kommo ID: <code>{_esc(details.get('id'))}</code>\n"
        f"Клиент: {_esc(contact.get('name') or '—')}\n"
        f"Телефон: {_esc(phones)}\n"
        f"Email: {_esc(emails)}\n"
        f"Этап: {_esc(details.get('status_name'))}\n"
        f"Создана: {created}"
    )


async def send_unreviewed_lead_card(
    chat_id: int,
    details: dict[str, Any],
    *,
    return_page: int = 1,
) -> dict:
    lead_id = int(details["id"])
    keyboard = {
        "inline_keyboard": [
            [
                {
                    "text": "➕ Добавить ID",
                    "callback_data": f"unrev:add:{lead_id}:{return_page}",
                }
            ],
            [
                {"text": "🔗 Открыть Kommo", "url": details.get("url")},
                {
                    "text": "⬅️ Назад",
                    "callback_data": f"menu:unrev:{return_page}",
                },
            ],
            [{"text": "🏠 Главное меню", "callback_data": "menu:home"}],
        ]
    }
    return await send_message(
        chat_id, format_unreviewed_lead_card(details), reply_markup=keyboard
    )


async def send_unreviewed_match_candidates(
    chat_id: int,
    *,
    lead_id: int,
    return_page: int,
    candidates: list[dict[str, Any]],
) -> dict:
    rows: list[list[dict[str, Any]]] = []
    for item in candidates[:8]:
        row_number = item.get("row_number")
        label = (
            f"{item.get('lead_number')} · "
            f"{_short_button_title(item.get('product'), 18)}"
        )
        rows.append(
            [
                {
                    "text": label[:38],
                    "callback_data": f"unrev:pick:{row_number}:{lead_id}:{return_page}",
                }
            ]
        )
    rows.extend(
        [
            [
                {
                    "text": "⬅️ Назад",
                    "callback_data": f"unrev:view:{lead_id}:{return_page}",
                }
            ],
            [{"text": "🏠 Главное меню", "callback_data": "menu:home"}],
        ]
    )
    lines = ["⚠️ <b>Найдено несколько строк</b>", "", "Выберите правильный вариант:", ""]
    for item in candidates[:8]:
        lines.append(
            f"• <b>{_esc(item.get('lead_number'))}</b> · "
            f"{_esc(item.get('product'))}\n"
            f"  {_esc(item.get('phone') or '—')} · "
            f"{_esc(item.get('client_name') or item.get('company') or '—')}"
        )
    return await send_message(
        chat_id, "\n".join(lines), reply_markup={"inline_keyboard": rows}
    )


async def send_unreviewed_no_match(
    chat_id: int,
    *,
    lead_id: int,
    return_page: int,
) -> dict:
    return await send_message(
        chat_id,
        (
            "❌ <b>Строка в таблице не найдена</b>\n\n"
            "Проверьте номер телефона, email или имя клиента."
        ),
        reply_markup={
            "inline_keyboard": [
                [
                    {
                        "text": "🔎 Ввести номер лида вручную",
                        "callback_data": f"unrev:manual:{lead_id}:{return_page}",
                    }
                ],
                [
                    {
                        "text": "🔄 Обновить таблицу",
                        "callback_data": f"unrev:refresh:{lead_id}:{return_page}",
                    }
                ],
                [
                    {
                        "text": "⬅️ Назад",
                        "callback_data": f"unrev:view:{lead_id}:{return_page}",
                    }
                ],
            ]
        },
    )


async def send_unreviewed_rename_preview(
    chat_id: int,
    *,
    lead_id: int,
    return_page: int,
    current_name: str,
    preview: dict[str, Any],
) -> dict:
    text = (
        "✏️ <b>ПРОВЕРКА НАЗВАНИЯ</b>\n\n"
        f"Текущее название:\n{_esc(current_name)}\n\n"
        "Строка таблицы:\n"
        f"ID: {_esc(preview.get('spreadsheet_lead_number'))}\n"
        f"Товар: {_esc(preview.get('original_product'))}\n\n"
        f"Название по-русски:\n{_esc(preview.get('short_product_ru'))}\n\n"
        f"Новое название:\n<b>{_esc(preview.get('proposed_name'))}</b>"
    )
    keyboard = {
        "inline_keyboard": [
            [
                {
                    "text": "✅ Обновить название",
                    "callback_data": f"unrev:confirm:{lead_id}:{return_page}",
                }
            ],
            [
                {
                    "text": "✏️ Изменить название",
                    "callback_data": f"unrev:editname:{lead_id}:{return_page}",
                },
                {
                    "text": "🔄 Выбрать другую строку",
                    "callback_data": f"unrev:repick:{lead_id}:{return_page}",
                },
            ],
            [
                {
                    "text": "❌ Отмена",
                    "callback_data": f"unrev:view:{lead_id}:{return_page}",
                }
            ],
        ]
    }
    return await send_message(chat_id, text, reply_markup=keyboard)


async def send_unreviewed_replace_warning(
    chat_id: int,
    *,
    lead_id: int,
    return_page: int,
    current_name: str,
    preview: dict[str, Any],
) -> dict:
    return await send_message(
        chat_id,
        (
            "⚠️ <b>У сделки уже есть внутренний номер</b>\n\n"
            f"Текущее название:\n{_esc(current_name)}\n\n"
            f"Новая строка:\n<b>{_esc(preview.get('proposed_name'))}</b>\n\n"
            "Заменить существующий номер?"
        ),
        reply_markup={
            "inline_keyboard": [
                [
                    {
                        "text": "⚠️ Да, заменить",
                        "callback_data": f"unrev:replace:{lead_id}:{return_page}",
                    }
                ],
                [
                    {
                        "text": "❌ Отмена",
                        "callback_data": f"unrev:view:{lead_id}:{return_page}",
                    }
                ],
            ]
        },
    )


async def send_unreviewed_success(
    chat_id: int,
    *,
    lead_id: int,
    return_page: int,
    result: dict[str, Any],
) -> dict:
    return await send_message(
        chat_id,
        (
            "✅ <b>НАЗВАНИЕ ОБНОВЛЕНО</b>\n\n"
            f"<b>{_esc(result.get('lead_name'))}</b>\n\n"
            f"Kommo ID: <code>{lead_id}</code>\n"
            f"Внутренний номер: {_esc(result.get('internal_number'))}\n"
            f"Категория: {_esc(result.get('short_product_ru'))}"
        ),
        reply_markup={
            "inline_keyboard": [
                [{"text": "🔗 Открыть Kommo", "url": result.get("url")}],
                [
                    {
                        "text": "➡️ Следующая сделка",
                        "callback_data": f"unrev:next:{return_page}",
                    }
                ],
                [
                    {
                        "text": "📥 К списку",
                        "callback_data": f"menu:unrev:{return_page}",
                    },
                    {"text": "🏠 Главное меню", "callback_data": "menu:home"},
                ],
            ]
        },
    )
