"""Telegram runtime for one-by-one Facebook lead onboarding.

This replaces only the manual sync button flow.  Existing lead/project callbacks and
legacy sync functions remain available as fallbacks for an already-open old session.
"""
from __future__ import annotations

import html
import logging
from typing import Any

from app.config import get_settings
from app.services import (
    identity_service,
    sequential_lead_onboarding_service as onboarding,
    telegram_service,
    telegram_state_service,
)

logger = logging.getLogger(__name__)
settings = get_settings()
_INSTALLED = False
_MODE = "sequential_facebook_onboarding"


def _ttl() -> int:
    return max(int(settings.telegram_state_ttl_minutes or 60) * 60, 6 * 60 * 60)


async def _save_state(user_id: int, state: dict[str, Any]) -> None:
    await telegram_state_service.set_state(user_id, state, ttl_seconds=_ttl())


def _summary_markup(report: dict[str, Any]) -> dict[str, Any]:
    rows: list[list[dict[str, Any]]] = []
    if int(report.get("matched_count") or 0) > 0:
        rows.append(
            [
                {
                    "text": f"▶️ Начать обработку ({int(report.get('matched_count') or 0)})",
                    "callback_data": "sync:prepare",
                }
            ]
        )
    rows.extend(
        [
            [{"text": "🔄 Проверить заново", "callback_data": "sync:run"}],
            [{"text": "🏠 Главное меню", "callback_data": "menu:home"}],
        ]
    )
    return {"inline_keyboard": rows}


async def _show_current(chat_id: int, user_id: int) -> None:
    state = await telegram_state_service.get_state(user_id)
    if not state or state.get("mode") != _MODE:
        await telegram_service.send_message(
            chat_id,
            "⚠️ Сессия обработки устарела. Нажмите «Проверить новые лиды» ещё раз.",
            reply_markup={
                "inline_keyboard": [
                    [{"text": "🔄 Проверить новые лиды", "callback_data": "sync:run"}]
                ]
            },
        )
        return
    queue = list(state.get("queue") or [])
    index = int(state.get("index") or 0)
    if index >= len(queue):
        processed = int(state.get("processed_count") or 0)
        skipped = int(state.get("skipped_count") or 0)
        await telegram_state_service.clear_state(user_id)
        await telegram_service.send_message(
            chat_id,
            (
                "✅ <b>ОБРАБОТКА ЗАВЕРШЕНА</b>\n\n"
                f"Оформлено лидов: <b>{processed}</b>\n"
                f"Пропущено: <b>{skipped}</b>\n\n"
                "По оформленным лидам заполнен Y, установлен «Первый контакт», "
                "добавлены анализы и задачи. Колонки W и X не изменялись."
            ),
            reply_markup={
                "inline_keyboard": [
                    [{"text": "🔄 Проверить ещё", "callback_data": "sync:run"}],
                    [{"text": "🏠 Главное меню", "callback_data": "menu:home"}],
                ]
            },
        )
        return
    item = dict(queue[index])
    await telegram_service.send_message(
        chat_id,
        onboarding.format_item_card(item, index, len(queue)),
        reply_markup=onboarding.item_markup(item),
    )


async def _run(chat_id: int, user_id: int) -> None:
    actor = identity_service.current_user()
    if actor is not None and actor.lead_access_scope == "assigned":
        await telegram_service.send_message(
            chat_id,
            "🔒 Обработка общей очереди доступна Owner/Admin.",
        )
        return
    await telegram_service.send_message(
        chat_id,
        "🔄 Читаю Facebook-лиды из «Неразобранного» и строки Google Sheets. Ничего не изменяю…",
    )
    try:
        report = await onboarding.build_onboarding_queue()
    except Exception as exc:
        logger.exception("Sequential onboarding preview failed")
        await telegram_service.send_message(
            chat_id,
            (
                "❌ <b>Не удалось подготовить очередь</b>\n\n"
                f"<code>{html.escape(type(exc).__name__)}</code>: "
                f"{html.escape(str(exc)[:500])}"
            ),
        )
        return
    state = {
        "mode": _MODE,
        "chat_id": chat_id,
        "digest": report.get("digest"),
        "generated_at": report.get("generated_at"),
        "queue": list(report.get("items") or []),
        "index": 0,
        "processed_count": 0,
        "skipped_count": 0,
    }
    await _save_state(user_id, state)
    await telegram_service.send_message(
        chat_id,
        onboarding.format_queue_summary(report),
        reply_markup=_summary_markup(report),
    )


async def _start(chat_id: int, user_id: int, original_start: Any) -> None:
    state = await telegram_state_service.get_state(user_id)
    if not state or state.get("mode") != _MODE:
        await original_start(chat_id, user_id)
        return
    if not list(state.get("queue") or []):
        await telegram_service.send_message(chat_id, "Надёжно сопоставленных Facebook-лидов нет.")
        return
    if not settings.google_sheets_write_enabled:
        await telegram_service.send_message(
            chat_id,
            (
                "🔒 <b>Запись Y отключена</b>\n\n"
                "Для подтверждаемой обработки установите "
                "<code>GOOGLE_SHEETS_WRITE_ENABLED=true</code> и дайте service account право Editor."
            ),
        )
        return
    await _show_current(chat_id, user_id)


async def _apply_current(chat_id: int, user_id: int, original_confirm: Any) -> None:
    state = await telegram_state_service.get_state(user_id)
    if not state or state.get("mode") != _MODE:
        await original_confirm(chat_id, user_id)
        return
    queue = list(state.get("queue") or [])
    index = int(state.get("index") or 0)
    if index >= len(queue):
        await _show_current(chat_id, user_id)
        return
    item = dict(queue[index])
    await telegram_service.send_message(
        chat_id,
        (
            "⏳ <b>Оформляю лид</b>\n\n"
            "1/4 — записываю номер Y\n"
            "2/4 — перевожу на «Первый контакт»\n"
            "3/4 — добавляю анализ и задачу\n"
            "4/4 — устанавливаю итоговое название"
        ),
    )
    result = await onboarding.apply_item(item)
    if not result.get("success"):
        partial = bool(result.get("partial"))
        steps = ", ".join(str(value) for value in result.get("steps") or []) or "нет"
        await telegram_service.send_message(
            chat_id,
            (
                f"{'⚠️' if partial else '❌'} <b>ЛИД НЕ ЗАВЕРШЁН</b>\n\n"
                f"Ошибка: {html.escape(str(result.get('error') or 'неизвестная ошибка'))}\n"
                f"Успешные шаги: <code>{html.escape(steps)}</code>\n\n"
                "Лид остаётся текущим. Повторное подтверждение безопасно: уже выполненные шаги не дублируются."
            ),
            reply_markup={
                "inline_keyboard": [
                    [{"text": "🔄 Повторить", "callback_data": "sync:confirm"}],
                    [{"text": "⏭ Пропустить", "callback_data": "onboard:skip"}],
                    [{"text": "❌ Завершить", "callback_data": "onboard:cancel"}],
                ]
            },
        )
        return

    state["processed_count"] = int(state.get("processed_count") or 0) + 1
    state["index"] = index + 1
    await _save_state(user_id, state)
    rows: list[list[dict[str, Any]]] = []
    if result.get("whatsapp_url"):
        rows.append(
            [
                {
                    "text": "💬 Открыть WhatsApp с готовым текстом",
                    "url": str(result["whatsapp_url"]),
                }
            ]
        )
    if result.get("kommo_url"):
        rows.append([{"text": "Открыть оформленную сделку", "url": str(result["kommo_url"])}])
    await telegram_service.send_message(
        chat_id,
        (
            "✅ <b>ЛИД ОФОРМЛЕН</b>\n\n"
            f"Название: <b>{html.escape(str(result.get('lead_name') or '—'))}</b>\n"
            "Y заполнен → Первый контакт установлен → анализ и задача добавлены → название проверено."
        ),
        reply_markup={"inline_keyboard": rows} if rows else None,
    )
    await _show_current(chat_id, user_id)


async def _skip(chat_id: int, user_id: int) -> None:
    state = await telegram_state_service.get_state(user_id)
    if not state or state.get("mode") != _MODE:
        await _show_current(chat_id, user_id)
        return
    state["index"] = int(state.get("index") or 0) + 1
    state["skipped_count"] = int(state.get("skipped_count") or 0) + 1
    await _save_state(user_id, state)
    await telegram_service.send_message(chat_id, "⏭ Лид пропущен. Данные не изменены.")
    await _show_current(chat_id, user_id)


async def _refresh(chat_id: int, user_id: int) -> None:
    state = await telegram_state_service.get_state(user_id)
    if not state or state.get("mode") != _MODE:
        await _show_current(chat_id, user_id)
        return
    queue = list(state.get("queue") or [])
    index = int(state.get("index") or 0)
    if index >= len(queue):
        await _show_current(chat_id, user_id)
        return
    await telegram_service.send_message(chat_id, "🔄 Пересчитываю квалификацию и рекомендацию…")
    queue[index] = await onboarding.refresh_item_analysis(dict(queue[index]))
    state["queue"] = queue
    await _save_state(user_id, state)
    await _show_current(chat_id, user_id)


async def _cancel(chat_id: int, user_id: int) -> None:
    state = await telegram_state_service.get_state(user_id) or {}
    processed = int(state.get("processed_count") or 0)
    skipped = int(state.get("skipped_count") or 0)
    await telegram_state_service.clear_state(user_id)
    await telegram_service.send_message(
        chat_id,
        (
            "Обработка остановлена.\n"
            f"Уже оформлено: <b>{processed}</b>; пропущено: <b>{skipped}</b>. "
            "Оставшиеся лиды не изменены."
        ),
        reply_markup={
            "inline_keyboard": [
                [{"text": "🔄 Начать заново", "callback_data": "sync:run"}],
                [{"text": "🏠 Главное меню", "callback_data": "menu:home"}],
            ]
        },
    )


def install_sequential_lead_onboarding_runtime() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True

    from app.api import telegram as telegram_api

    original_run = telegram_api._run_status_sync
    original_start = telegram_api._prepare_status_sync_confirmation
    original_confirm = telegram_api._confirm_status_sync
    original_callbacks = telegram_api._handle_manager_callback

    async def run_status_sync(chat_id: int, user_id: int) -> None:
        await _run(chat_id, user_id)

    async def prepare_status_sync(chat_id: int, user_id: int) -> None:
        await _start(chat_id, user_id, original_start)

    async def confirm_status_sync(chat_id: int, user_id: int) -> None:
        await _apply_current(chat_id, user_id, original_confirm)

    async def handle_callbacks(
        *, callback_data: str, chat_id: int, user_id: int, db: Any
    ) -> bool:
        if callback_data.startswith("onboard:"):
            actor = identity_service.current_user()
            if actor is not None and actor.lead_access_scope == "assigned":
                await telegram_service.send_message(
                    chat_id, "🔒 Обработка общей очереди доступна Owner/Admin."
                )
                return True
            if callback_data == "onboard:skip":
                await _skip(chat_id, user_id)
                return True
            if callback_data == "onboard:refresh":
                await _refresh(chat_id, user_id)
                return True
            if callback_data == "onboard:cancel":
                await _cancel(chat_id, user_id)
                return True
        return await original_callbacks(
            callback_data=callback_data,
            chat_id=chat_id,
            user_id=user_id,
            db=db,
        )

    telegram_api._run_status_sync = run_status_sync
    telegram_api._prepare_status_sync_confirmation = prepare_status_sync
    telegram_api._confirm_status_sync = confirm_status_sync
    telegram_api._handle_manager_callback = handle_callbacks
    logger.info("Sequential Facebook lead onboarding runtime installed")
