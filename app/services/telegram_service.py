"""
Telegram Bot API helpers.

The bot token is embedded in Telegram API URLs, so this module never raises
httpx errors containing the full request URL and therefore the token.
"""
from __future__ import annotations

import html
import logging
from datetime import datetime, timezone
from typing import Any

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from app.config import get_settings

logger = logging.getLogger(__name__)
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
        response = await client.post(
            f"{TELEGRAM_API}/sendMessage",
            json=payload,
        )
        _ensure_success(response, "sendMessage")
        return response.json()


async def send_message_chunks(
    chat_id: int,
    chunks: list[str],
    parse_mode: str = "HTML",
) -> None:
    """Send preformatted chunks sequentially, keeping below Telegram limits."""
    for chunk in chunks:
        if not chunk:
            continue
        # Defensive fallback in case a formatter generated an oversized chunk.
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
    """Send the structured AI report with explicit human-approval buttons."""
    if target_kommo_lead_id:
        primary_button = {
            "text": "📝 Добавить разговор в выбранную сделку",
            "callback_data": (
                f"action:kommo_update:{lead_id}:{voice_note_id}:{target_kommo_lead_id}"
            ),
        }
    else:
        primary_button = {
            "text": "➕ Добавить лид в Kommo",
            "callback_data": f"action:kommo_create:{lead_id}:{voice_note_id}",
        }

    inline_keyboard = [
        [primary_button],
        [
            {
                "text": "✉️ Создать черновик Gmail",
                "callback_data": f"action:gmail:{lead_id}:{voice_note_id}",
            },
            {
                "text": "📅 Добавить в календарь",
                "callback_data": f"action:calendar:{lead_id}:{voice_note_id}",
            },
        ],
        [
            {
                "text": "💬 Показать WhatsApp-текст",
                "callback_data": f"action:whatsapp:{lead_id}:{voice_note_id}",
            },
            {
                "text": "❌ Ничего не делать",
                "callback_data": f"action:cancel:{lead_id}:{voice_note_id}",
            },
        ],
    ]
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            f"{TELEGRAM_API}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": report_text,
                "parse_mode": "HTML",
                "reply_markup": {"inline_keyboard": inline_keyboard},
            },
        )
        _ensure_success(response, "sendMessage(report)")
        return response.json()


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
            f"{TELEGRAM_API}/getFile",
            params={"file_id": file_id},
        )
        _ensure_success(response, "getFile")
        data = response.json()
        return data["result"]["file_path"]


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=10))
async def download_file(file_path: str) -> bytes:
    url = f"{TELEGRAM_FILE_API}/{file_path}"
    async with httpx.AsyncClient(timeout=90) as client:
        response = await client.get(url)
        _ensure_success(response, "downloadFile")
        return response.content


async def download_voice(file_id: str) -> bytes:
    file_path = await get_file_path(file_id)
    return await download_file(file_path)


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
    """Expose one main /menu command plus diagnostics in Telegram UI."""
    commands = [
        {"command": "menu", "description": "Открыть меню Kommo"},
    ]
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.post(
            f"{TELEGRAM_API}/setMyCommands",
            json={"commands": commands},
        )
        _ensure_success(response, "setMyCommands")
        return response.json()


def _esc(value: Any) -> str:
    return html.escape(str(value if value not in (None, "") else "—"))


def _format_unix_timestamp(value: Any) -> str:
    if not value:
        return "—"
    try:
        return datetime.fromtimestamp(int(value), tz=timezone.utc).strftime("%d.%m.%Y %H:%M UTC")
    except (TypeError, ValueError, OSError):
        return str(value)


def format_open_leads_messages(result: dict[str, Any]) -> list[str]:
    """Format all open Kommo leads into Telegram-safe message chunks."""
    leads = result.get("leads") or []
    count = result.get("open_count", len(leads))

    if not leads:
        return ["✅ <b>Открытых сделок в Kommo нет.</b>"]

    header = (
        f"📋 <b>Открытые сделки Kommo: {count}</b>\n"
        "Сначала показаны недавно обновлённые сделки.\n"
    )
    if result.get("truncated"):
        header += (
            f"⚠️ Проверены только первые {result.get('page_cap')} страниц. "
            "Увеличьте KOMMO_OPEN_LEADS_MAX_PAGES для полной выборки.\n"
        )

    chunks: list[str] = []
    current = header

    for index, lead in enumerate(leads, start=1):
        price = lead.get("price")
        price_line = f"\n💰 Бюджет сделки: {_esc(price)}" if price else ""
        task_line = (
            f"\n⏰ Ближайшая задача: {_esc(_format_unix_timestamp(lead.get('closest_task_at')))}"
            if lead.get("closest_task_at")
            else ""
        )
        entry = (
            f"\n<b>{index}. {_esc(lead.get('name'))}</b>\n"
            f"ID: <code>{_esc(lead.get('id'))}</code>\n"
            f"Воронка: {_esc(lead.get('pipeline_name'))}\n"
            f"Этап: {_esc(lead.get('status_name'))}"
            f"{price_line}{task_line}\n"
            f"🔗 <a href=\"{html.escape(lead.get('url') or '', quote=True)}\">Открыть в Kommo</a>\n"
        )
        if len(current) + len(entry) > SAFE_MESSAGE_CHUNK:
            chunks.append(current)
            current = entry.lstrip("\n")
        else:
            current += entry

    if current:
        chunks.append(current)
    return chunks


def format_report(report: dict, transcript: str) -> str:
    client = report.get("client", {})
    lead = report.get("lead", {})
    email = report.get("email", {})
    whatsapp = report.get("whatsapp", {})
    calendar = report.get("calendar", {})

    def _list(items: list) -> str:
        if not items:
            return "  <i>Не указано</i>"
        return "\n".join(f"  • {_esc(item)}" for item in items)

    confidence = report.get("confidence_score", 0) or 0
    review_flag = "⚠️ <b>Требуется проверка менеджера</b>\n\n" if report.get("needs_human_review") else ""

    return (
        f"{review_flag}"
        f"<b>🎙 Анализ голосового сообщения</b>\n"
        f"{'─' * 30}\n\n"
        f"<b>👤 Клиент</b>\n"
        f"Имя: {_esc(client.get('name'))}\n"
        f"Компания: {_esc(client.get('company'))}\n"
        f"Телефон: {_esc(client.get('phone'))}\n"
        f"Email: {_esc(client.get('email'))}\n"
        f"Язык: {_esc(client.get('language'))}\n\n"
        f"<b>📦 Запрос</b>\n"
        f"Товар: {_esc(lead.get('product_requested'))}\n"
        f"Бюджет: {_esc(lead.get('budget'))}\n"
        f"Страна/город: {_esc(lead.get('country'))} / {_esc(lead.get('city'))}\n"
        f"Срочность: {_esc(lead.get('urgency'))}\n"
        f"Статус: {_esc(lead.get('status'))}\n\n"
        f"<b>📝 Резюме</b>\n{_esc(report.get('conversation_summary'))}\n\n"
        f"<b>✅ Что было сказано</b>\n{_list(report.get('what_manager_said', []))}\n\n"
        f"<b>⚠️ Слабые места</b>\n{_list(report.get('mistakes_or_weak_points', []))}\n\n"
        f"<b>❓ Что уточнить</b>\n{_list(report.get('missing_questions', []))}\n\n"
        f"<b>➡️ Следующий шаг</b>\n{_esc(report.get('recommended_next_step'))}\n\n"
        f"<b>✉️ Черновик письма</b>\n"
        f"Тема: {_esc(email.get('subject'))}\n"
        f"<pre>{_esc(email.get('body'))}</pre>\n\n"
        f"<b>💬 Черновик WhatsApp</b>\n<pre>{_esc(whatsapp.get('message'))}</pre>\n\n"
        f"<b>📅 Следующий контакт</b>\n"
        f"Название: {_esc(calendar.get('title'))}\n"
        f"Когда: {_esc(calendar.get('start_time'))}\n"
        f"<pre>{_esc(calendar.get('description'))}</pre>\n\n"
        f"<i>Уверенность анализа: {confidence:.0%}</i>"
    )


async def send_main_menu(chat_id: int) -> dict:
    keyboard = {
        "inline_keyboard": [
            [
                {"text": "📋 Открытые сделки", "callback_data": "menu:leads:1"},
                {"text": "🔎 Найти сделку", "callback_data": "menu:search"},
            ],
            [
                {"text": "🎙 Новый лид из аудио", "callback_data": "menu:new"},
                {"text": "📝 Обновить существующий лид", "callback_data": "menu:update"},
            ],
            [
                {"text": "🔌 Проверить Kommo", "callback_data": "menu:test"},
            ],
        ]
    }
    return await send_message(
        chat_id,
        (
            "🏠 <b>Меню Buy & Bring Assistant</b>\n\n"
            "Выберите действие. Для второго разговора откройте сделку и нажмите "
            "<b>«Добавить разговор»</b>."
        ),
        reply_markup=keyboard,
    )


def _short_button_title(value: Any, limit: int = 42) -> str:
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
        keyboard = {"inline_keyboard": [[{"text": "↩️ В меню", "callback_data": "menu:home"}]]}
        title = "По вашему запросу открытые сделки не найдены." if search_mode else "Открытых сделок нет."
        return await send_message(chat_id, f"✅ {title}", reply_markup=keyboard)

    rows: list[list[dict[str, Any]]] = []
    for lead in leads:
        lead_id = lead.get("id")
        if not isinstance(lead_id, int):
            continue
        label = f"{lead_id} · {_short_button_title(lead.get('name'))}"
        rows.append(
            [
                {
                    "text": label,
                    "callback_data": f"lead:view:{lead_id}:{page}",
                }
            ]
        )

    if not search_mode and total_pages > 1:
        nav: list[dict[str, Any]] = []
        if page > 1:
            nav.append({"text": "⬅️", "callback_data": f"menu:leads:{page - 1}"})
        nav.append({"text": f"{page}/{total_pages}", "callback_data": "noop"})
        if page < total_pages:
            nav.append({"text": "➡️", "callback_data": f"menu:leads:{page + 1}"})
        rows.append(nav)

    rows.append(
        [
            {"text": "🔎 Новый поиск", "callback_data": "menu:search"},
            {"text": "🏠 Меню", "callback_data": "menu:home"},
        ]
    )

    if search_mode:
        heading = f"🔎 <b>Результаты поиска: {total}</b>"
    else:
        heading = f"📋 <b>Открытые сделки: {total}</b>\nСтраница {page} из {total_pages}"
    return await send_message(chat_id, heading, reply_markup={"inline_keyboard": rows})


def format_lead_details(details: dict[str, Any]) -> str:
    contacts = details.get("contacts") or []
    contact_lines: list[str] = []
    for contact in contacts[:5]:
        line = f"• <b>{_esc(contact.get('name'))}</b>"
        phones = contact.get("phones") or []
        emails = contact.get("emails") or []
        if phones:
            line += f"\n  Телефон: {_esc(', '.join(phones))}"
        if emails:
            line += f"\n  Email: {_esc(', '.join(emails))}"
        contact_lines.append(line)
    contacts_text = "\n".join(contact_lines) if contact_lines else "• Контакты не привязаны"

    custom_fields = details.get("custom_fields") or []
    field_lines = [
        f"• {_esc(field.get('name'))}: {_esc(field.get('value'))}"
        for field in custom_fields[:12]
    ]
    fields_text = "\n".join(field_lines) if field_lines else "• Дополнительные поля не заполнены"

    notes = details.get("notes") or []
    note_lines: list[str] = []
    for note in notes[:3]:
        text = str(note.get("text") or "").strip().replace("\n", " ")
        if len(text) > 350:
            text = text[:349] + "…"
        note_lines.append(
            f"• {_format_unix_timestamp(note.get('created_at') or note.get('updated_at'))}\n  {_esc(text)}"
        )
    notes_text = "\n".join(note_lines) if note_lines else "• Текстовых примечаний пока нет"

    price = details.get("price")
    price_text = _esc(price) if price not in (None, 0, "") else "—"
    return (
        f"📌 <b>{_esc(details.get('name'))}</b>\n"
        f"ID: <code>{_esc(details.get('id'))}</code>\n"
        f"Воронка: {_esc(details.get('pipeline_name'))}\n"
        f"Этап: {_esc(details.get('status_name'))}\n"
        f"Бюджет сделки: {price_text}\n"
        f"Ответственный ID: <code>{_esc(details.get('responsible_user_id'))}</code>\n"
        f"Обновлено: {_esc(_format_unix_timestamp(details.get('updated_at')))}\n"
        f"Ближайшая задача: {_esc(_format_unix_timestamp(details.get('closest_task_at')))}\n\n"
        f"<b>👤 Контакты</b>\n{contacts_text}\n\n"
        f"<b>📄 Поля сделки</b>\n{fields_text}\n\n"
        f"<b>🗒 Последние примечания</b>\n{notes_text}"
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
                    "text": "📝 Добавить текстовое примечание",
                    "callback_data": f"lead:text:{lead_id}:{return_page}",
                }
            ],
            [
                {
                    "text": "🎙 Добавить второй разговор",
                    "callback_data": f"lead:audio:{lead_id}:{return_page}",
                }
            ],
            [
                {
                    "text": "✅ Задача в Kommo",
                    "callback_data": f"lead:task:{lead_id}:{return_page}",
                },
                {
                    "text": "📅 В календарь",
                    "callback_data": f"lead:calendar:{lead_id}:{return_page}",
                },
            ],
            [
                {"text": "🔗 Открыть в Kommo", "url": details.get("url")},
                {"text": "↩️ К сделкам", "callback_data": f"menu:leads:{return_page}"},
            ],
            [{"text": "🏠 Главное меню", "callback_data": "menu:home"}],
        ]
    }
    return await send_message(
        chat_id,
        format_lead_details(details),
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
    preview = note_text.strip()
    if len(preview) > 2500:
        preview = preview[:2499] + "…"
    keyboard = {
        "inline_keyboard": [
            [
                {
                    "text": "✅ Добавить в Kommo",
                    "callback_data": f"note:confirm:{lead_id}:{return_page}",
                },
                {"text": "❌ Отмена", "callback_data": f"note:cancel:{lead_id}:{return_page}"},
            ]
        ]
    }
    return await send_message(
        chat_id,
        (
            f"📝 <b>Проверка примечания</b>\n\n"
            f"Сделка: <b>{_esc(lead_name)}</b>\n"
            f"ID: <code>{lead_id}</code>\n\n"
            f"<pre>{_esc(preview)}</pre>\n\n"
            "Примечание будет добавлено только после подтверждения."
        ),
        reply_markup=keyboard,
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
    preview = task_text.strip()
    if len(preview) > 1500:
        preview = preview[:1499] + "…"
    keyboard = {
        "inline_keyboard": [
            [
                {
                    "text": "✅ Создать задачу",
                    "callback_data": f"task:confirm:{lead_id}:{return_page}",
                },
                {
                    "text": "❌ Отмена",
                    "callback_data": f"task:cancel:{lead_id}:{return_page}",
                },
            ]
        ]
    }
    return await send_message(
        chat_id,
        (
            "✅ <b>Проверка задачи Kommo</b>\n\n"
            f"Сделка: <b>{_esc(lead_name)}</b>\n"
            f"ID: <code>{lead_id}</code>\n"
            f"Срок: <b>{_esc(due_display)}</b>\n\n"
            f"<pre>{_esc(preview)}</pre>\n\n"
            "Задача будет создана только после подтверждения."
        ),
        reply_markup=keyboard,
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
) -> dict:
    keyboard = {
        "inline_keyboard": [
            [
                {
                    "text": "✅ Создать событие",
                    "callback_data": f"calendar:confirm:{lead_id}:{return_page}",
                },
                {
                    "text": "❌ Отмена",
                    "callback_data": f"calendar:cancel:{lead_id}:{return_page}",
                },
            ]
        ]
    }
    return await send_message(
        chat_id,
        (
            "📅 <b>Проверка события календаря</b>\n\n"
            f"Сделка: <b>{_esc(lead_name)}</b>\n"
            f"ID: <code>{lead_id}</code>\n"
            f"Название: {_esc(title)}\n"
            f"Начало: <b>{_esc(start_display)}</b>\n"
            f"Длительность: {duration_minutes} мин.\n\n"
            "Событие будет создано только после подтверждения."
        ),
        reply_markup=keyboard,
    )
