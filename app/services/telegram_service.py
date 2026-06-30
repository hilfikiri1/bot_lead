"""Telegram Bot API transport and polished Russian-language CRM interface."""

from __future__ import annotations

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
        {"command": "kommo_test", "description": "Проверить Kommo"},
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
) -> dict:
    steps = {
        "download": "📥 <b>Аудио получено</b>\nСкачиваю файл и проверяю формат…",
        "transcribe": "🎧 <b>Шаг 1 из 3</b>\nРасшифровываю разговор…",
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
        ]
    }
    return await send_message(
        chat_id,
        (
            "✨ <b>BBS • CRM Assistant</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "Работа со сделками Kommo прямо в Telegram.\n\n"
            "Отправьте аудио для нового лида или выберите существующую сделку, "
            "чтобы добавить разговор, примечание, задачу или событие."
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
    subtitle = (
        f"Найдено: <b>{total}</b>"
        if search_mode
        else f"Всего: <b>{total}</b> · страница {page}/{total_pages}"
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
                    "text": "📅 Календарь",
                    "callback_data": f"lead:calendar:{lead_id}:{return_page}",
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
) -> dict:
    return await send_message(
        chat_id,
        (
            "📅 <b>Проверка события</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"Сделка: <b>{_esc(lead_name)}</b>\n"
            f"Событие: {_esc(title)}\n"
            f"Начало: <b>{_esc(start_display)}</b>\n"
            f"Длительность: {duration_minutes} мин."
        ),
        reply_markup={
            "inline_keyboard": [
                [
                    {
                        "text": "✅ Создать",
                        "callback_data": f"calendar:confirm:{lead_id}:{return_page}",
                    },
                    {
                        "text": "❌ Отмена",
                        "callback_data": f"calendar:cancel:{lead_id}:{return_page}",
                    },
                ]
            ]
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
        lines.append(
            f"{labels.get(job.processing_status, job.processing_status)} · {created}\n"
            f"ID сообщения: <code>{job.telegram_message_id or '—'}</code>"
        )
    return "\n\n".join(lines)
