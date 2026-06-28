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
async def send_message(chat_id: int, text: str, parse_mode: str = "HTML") -> dict:
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            f"{TELEGRAM_API}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": parse_mode},
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
) -> dict:
    """Send the structured AI report with explicit human-approval buttons."""
    inline_keyboard = [
        [
            {
                "text": "➕ Добавить лид в Kommo",
                "callback_data": f"action:kommo_create:{lead_id}:{voice_note_id}",
            }
        ],
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
