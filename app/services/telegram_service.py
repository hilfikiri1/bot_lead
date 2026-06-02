"""
telegram_service.py
Handles all Telegram bot interactions: receiving voice notes,
sending structured AI reports, and managing inline button callbacks.
"""
from __future__ import annotations

import logging
import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

TELEGRAM_API = f"https://api.telegram.org/bot{settings.telegram_bot_token}"
TELEGRAM_FILE_API = f"https://api.telegram.org/file/bot{settings.telegram_bot_token}"


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=10))
async def send_message(chat_id: int, text: str, parse_mode: str = "HTML") -> dict:
    """Send a plain text message to a Telegram chat."""
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{TELEGRAM_API}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": parse_mode},
        )
        resp.raise_for_status()
        return resp.json()


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=10))
async def send_report(
    chat_id: int,
    report_text: str,
    lead_id: int,
    voice_note_id: int,
) -> dict:
    """Send the structured AI report with inline action buttons."""
    inline_keyboard = [
        [
            {"text": "✉️ Create Gmail draft", "callback_data": f"action:gmail:{lead_id}:{voice_note_id}"},
            {"text": "📅 Add to Calendar", "callback_data": f"action:calendar:{lead_id}:{voice_note_id}"},
        ],
        [
            {"text": "💬 Send WhatsApp draft to me", "callback_data": f"action:whatsapp:{lead_id}:{voice_note_id}"},
            {"text": "💾 Save to CRM", "callback_data": f"action:crm:{lead_id}:{voice_note_id}"},
        ],
        [
            {"text": "✏️ Edit data", "callback_data": f"action:edit:{lead_id}:{voice_note_id}"},
            {"text": "❌ Cancel", "callback_data": f"action:cancel:{lead_id}:{voice_note_id}"},
        ],
    ]
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{TELEGRAM_API}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": report_text,
                "parse_mode": "HTML",
                "reply_markup": {"inline_keyboard": inline_keyboard},
            },
        )
        resp.raise_for_status()
        return resp.json()


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=10))
async def answer_callback_query(callback_query_id: str, text: str = "") -> dict:
    """Acknowledge a callback query (removes loading spinner)."""
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            f"{TELEGRAM_API}/answerCallbackQuery",
            json={"callback_query_id": callback_query_id, "text": text, "show_alert": bool(text)},
        )
        resp.raise_for_status()
        return resp.json()


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=10))
async def get_file_path(file_id: str) -> str:
    """Resolve a Telegram file_id to a downloadable path."""
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(f"{TELEGRAM_API}/getFile", params={"file_id": file_id})
        resp.raise_for_status()
        data = resp.json()
        return data["result"]["file_path"]


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=10))
async def download_file(file_path: str) -> bytes:
    """Download raw bytes of a Telegram file."""
    url = f"{TELEGRAM_FILE_API}/{file_path}"
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.content


async def download_voice(file_id: str) -> bytes:
    """High-level helper: resolve file_id → download bytes."""
    file_path = await get_file_path(file_id)
    return await download_file(file_path)


async def delete_webhook() -> dict:
    """Delete any existing Telegram webhook."""
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            f"{TELEGRAM_API}/deleteWebhook",
            json={"drop_pending_updates": False},
        )
        resp.raise_for_status()
        return resp.json()


async def register_webhook(url: str) -> dict:
    """Register the bot webhook with Telegram."""
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            f"{TELEGRAM_API}/setWebhook",
            json={
                "url": url,
                "secret_token": settings.telegram_webhook_secret,
                "allowed_updates": ["message", "callback_query"],
            },
        )
        resp.raise_for_status()
        return resp.json()


def format_report(report: dict, transcript: str) -> str:
    """Format the AI analysis result into a readable Telegram HTML message."""
    client = report.get("client", {})
    lead = report.get("lead", {})
    email = report.get("email", {})
    whatsapp = report.get("whatsapp", {})
    calendar = report.get("calendar", {})

    def _list(items: list) -> str:
        if not items:
            return "  <i>None identified</i>"
        return "\n".join(f"  • {item}" for item in items)

    confidence = report.get("confidence_score", 0)
    review_flag = "⚠️ <b>Needs human review</b>\n\n" if report.get("needs_human_review") else ""

    text = (
        f"{review_flag}"
        f"<b>🎙 Voice Note Analysis</b>\n"
        f"{'─' * 35}\n\n"
        f"<b>👤 Client</b>\n"
        f"  Name: {client.get('name') or '—'}\n"
        f"  Company: {client.get('company') or '—'}\n"
        f"  Phone: {client.get('phone') or '—'}\n"
        f"  Email: {client.get('email') or '—'}\n"
        f"  Language: {client.get('language') or '—'}\n\n"
        f"<b>📦 Lead</b>\n"
        f"  Product: {lead.get('product_requested') or '—'}\n"
        f"  Budget: {lead.get('budget') or '—'}\n"
        f"  Country: {lead.get('country') or '—'} / {lead.get('city') or '—'}\n"
        f"  Urgency: {lead.get('urgency') or '—'}\n"
        f"  Status: {lead.get('status') or '—'}\n\n"
        f"<b>📝 Summary</b>\n{report.get('conversation_summary') or '—'}\n\n"
        f"<b>✅ What was said</b>\n{_list(report.get('what_manager_said', []))}\n\n"
        f"<b>⚠️ Mistakes / Weak points</b>\n{_list(report.get('mistakes_or_weak_points', []))}\n\n"
        f"<b>❓ Missing questions</b>\n{_list(report.get('missing_questions', []))}\n\n"
        f"<b>➡️ Recommended next step</b>\n  {report.get('recommended_next_step') or '—'}\n\n"
        f"<b>✉️ Email draft</b>\n"
        f"  <b>Subject:</b> {email.get('subject') or '—'}\n"
        f"  <b>Body:</b>\n<pre>{email.get('body') or '—'}</pre>\n\n"
        f"<b>💬 WhatsApp draft</b>\n<pre>{whatsapp.get('message') or '—'}</pre>\n\n"
        f"<b>📅 Calendar event</b>\n"
        f"  <b>Title:</b> {calendar.get('title') or '—'}\n"
        f"  <b>When:</b> {calendar.get('start_time') or '—'}\n"
        f"  <b>Talking points:</b>\n<pre>{calendar.get('description') or '—'}</pre>\n\n"
        f"<i>Confidence: {confidence:.0%}</i>"
    )
    return text
