"""Runtime extensions for manual onboarding, iPhone contacts and chat context."""
from __future__ import annotations

import html
import logging
import os
from typing import Any

from app.agent import tools as agent_tools
from app.services import (
    client_message_service,
    kommo_chat_service,
    kommo_service,
    telegram_service,
)

logger = logging.getLogger(__name__)
_INSTALLED = False


def _enabled(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().casefold() in {"1", "true", "yes", "on"}


def _esc(value: Any) -> str:
    return html.escape(str(value if value is not None else "—"))


def _chat_section(chat: dict[str, Any]) -> str:
    if not chat or not chat.get("enabled"):
        return ""
    if not chat.get("available"):
        if chat.get("reason") == "external_chat_history_scope_required":
            return (
                "<b>Переписка:</b> требуется разрешение Kommo "
                "<code>External chat history</code>."
            )
        return "<b>Переписка:</b> временно недоступна."

    messages = list(chat.get("messages") or [])
    if not messages:
        return "<b>Переписка:</b> беседы для этой сделки не найдены."

    lines = [
        f"<b>Последняя переписка · {_esc(chat.get('origin') or 'чат')}</b>"
    ]
    for item in messages[-6:]:
        direction = "Клиент" if item.get("direction") == "incoming" else "Мы"
        body = " ".join(str(item.get("text") or "[вложение]").split())
        if len(body) > 320:
            body = body[:317] + "…"
        lines.append(f"<b>{direction}:</b> {_esc(body)}")
    analysis = chat.get("analysis") or {}
    if analysis:
        lines.extend(
            [
                "",
                f"<b>Анализ:</b> {_esc(analysis.get('summary') or '—')}",
                f"<b>Следующий шаг:</b> "
                f"{_esc(analysis.get('recommended_action') or '—')}",
            ]
        )
    return "\n".join(lines)


def _format_onboarding_report(report: dict[str, Any]) -> str:
    actions = list(report.get("onboarding_actions") or [])
    unmatched = list(report.get("unmatched_table_rows") or [])
    duplicate_count = len(report.get("table_duplicates") or []) + len(
        report.get("kommo_duplicates") or []
    )
    move_count = sum(1 for item in actions if item.get("target_status_id"))
    lines = [
        "🆕 <b>ОБРАБОТКА НОВЫХ ЛИДОВ</b>",
        "",
        f"Воронка: <b>{_esc(report.get('pipeline_name') or '—')}</b>",
        f"Новых строк без номера Y: <b>{int(report.get('new_rows_count') or 0)}</b>",
        f"Надёжно найдено в Kommo: <b>{len(actions)}</b>",
        f"Будет присвоено номеров Y: <b>{int(report.get('number_assignments_count') or 0)}</b>",
        f"Будет переименовано сделок: <b>{int(report.get('kommo_renames_count') or 0)}</b>",
        f"Переход на «Первый контакт»: <b>{move_count}</b>",
        f"Не удалось однозначно найти: <b>{len(unmatched)}</b>",
        f"Дубли номеров: <b>{duplicate_count}</b>",
        "",
        "🔒 <b>Сделки в Kommo не создаются.</b>",
        "Колонки W и X не изменяются.",
    ]
    if actions:
        lines.extend(["", "<b>Будут обработаны:</b>"])
        for item in actions[:12]:
            lines.append(
                f"• строка {_esc(item.get('row_number'))}: "
                f"<b>№{_esc(item.get('lead_number'))}</b> · "
                f"{_esc(item.get('old_name'))} → <b>{_esc(item.get('new_name'))}</b>"
            )
        if len(actions) > 12:
            lines.append(f"<i>…и ещё {len(actions) - 12}</i>")
    if unmatched:
        lines.extend(["", "<b>Требуют ручного сопоставления:</b>"])
        for item in unmatched[:8]:
            lines.append(
                f"• строка {_esc(item.get('row_number'))}: "
                f"{_esc(item.get('client_name') or '—')} · "
                f"{_esc(item.get('product') or '—')}"
            )
        if len(unmatched) > 8:
            lines.append(f"<i>…и ещё {len(unmatched) - 8}</i>")
    if not actions and not unmatched:
        lines.extend(["", "✅ Новых лидов для обработки нет."])
    return "\n".join(lines)[:4000]


async def _send_onboarding_report(
    chat_id: int, report: dict[str, Any]
) -> dict[str, Any]:
    rows: list[list[dict[str, Any]]] = []
    if report.get("updates_count"):
        if telegram_service.settings.google_sheets_write_enabled:
            rows.append(
                [
                    {
                        "text": f"✅ Обработать новые лиды ({report['updates_count']})",
                        "callback_data": "sync:prepare",
                    }
                ]
            )
        else:
            rows.append(
                [
                    {
                        "text": "🔒 Запись в Y выключена",
                        "callback_data": "sync:write_help",
                    }
                ]
            )
    rows.extend(
        [
            [{"text": "🔄 Проверить новые лиды", "callback_data": "sync:run"}],
            [{"text": "🏠 Главное меню", "callback_data": "menu:home"}],
        ]
    )
    return await telegram_service.send_message(
        chat_id,
        _format_onboarding_report(report),
        reply_markup={"inline_keyboard": rows},
    )


async def _send_onboarding_confirmation(
    chat_id: int, report: dict[str, Any]
) -> dict[str, Any]:
    count = int(report.get("updates_count") or 0)
    return await telegram_service.send_message(
        chat_id,
        (
            "⚠️ <b>ПОДТВЕРЖДЕНИЕ ОБРАБОТКИ НОВЫХ ЛИДОВ</b>\n\n"
            f"Надёжно сопоставлено: <b>{count}</b>.\n\n"
            "После подтверждения бот:\n"
            "• заполнит только пустые номера Y;\n"
            "• переименует найденные сделки Kommo;\n"
            "• при необходимости переведёт их на «Первый контакт»;\n"
            "• добавит первичный анализ и задачу;\n"
            "• пришлёт готовые контакты .vcf для iPhone.\n\n"
            "Новые сделки не создаются. Колонки W и X не изменяются."
        ),
        reply_markup={
            "inline_keyboard": [
                [
                    {
                        "text": f"✅ Да, обработать {count}",
                        "callback_data": "sync:confirm",
                    }
                ],
                [{"text": "❌ Отмена", "callback_data": "sync:cancel"}],
            ]
        },
    )


async def _send_vcards(chat_id: int, result: dict[str, Any]) -> None:
    for card in result.get("contact_cards") or []:
        phone = str(card.get("phone") or "").strip()
        if not phone:
            continue
        try:
            client_name = str(card.get("name") or "Клиент").strip()
            product = str(card.get("product") or "Новый запрос").strip()
            lead_number = str(card.get("lead_number") or "").strip()
            display_name = f"{client_name} — {product}"[:120]
            content = client_message_service.build_vcard(
                name=display_name,
                company=(
                    f"B&BS · лид №{lead_number}" if lead_number else "B&BS"
                ),
                phone=phone,
                email=str(card.get("email") or "").strip() or None,
                language="pl",
            )
            await telegram_service.send_document(
                chat_id,
                filename=client_message_service.vcard_filename(display_name),
                content=content,
                caption=(
                    f"👤 <b>Контакт для iPhone · №{_esc(lead_number)}</b>\n"
                    f"{_esc(client_name)} — {_esc(product)}\n"
                    "Нажмите на файл и выберите «Создать новый контакт»."
                ),
                mime_type="text/vcard",
            )
        except Exception as exc:
            logger.warning("Could not send onboarding vCard: %s", exc)


async def _send_onboarding_result(
    chat_id: int, result: dict[str, Any]
) -> dict[str, Any]:
    skipped = list(result.get("skipped") or []) + list(
        result.get("rename_skipped") or []
    )
    response = await telegram_service.send_message(
        chat_id,
        (
            "✅ <b>НОВЫЕ ЛИДЫ ОБРАБОТАНЫ</b>\n\n"
            f"Присвоено номеров Y: <b>{int(result.get('updated_count') or 0)}</b>\n"
            f"Переименовано сделок: <b>{int(result.get('renamed_count') or 0)}</b>\n"
            f"Переведено на «Первый контакт»: "
            f"<b>{int(result.get('status_moved_count') or 0)}</b>\n"
            f"Добавлено анализов: <b>{int(result.get('note_count') or 0)}</b>\n"
            f"Создано задач: <b>{int(result.get('task_count') or 0)}</b>\n"
            f"Контактов для iPhone: <b>{len(result.get('contact_cards') or [])}</b>\n"
            f"Пропущено после повторной проверки: <b>{len(skipped)}</b>\n\n"
            "Колонки W и X не изменялись."
        ),
        reply_markup={
            "inline_keyboard": [
                [{"text": "🔄 Проверить ещё", "callback_data": "sync:run"}],
                [{"text": "🏠 Главное меню", "callback_data": "menu:home"}],
            ]
        },
    )
    await _send_vcards(chat_id, result)
    return response


def install_runtime_extensions() -> None:
    """Install wrappers exactly once during application startup."""
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True

    original_get_lead_details = kommo_service.get_lead_details

    async def get_lead_details_with_chat(lead_id: int) -> dict[str, Any]:
        details = await original_get_lead_details(lead_id)
        if _enabled("KOMMO_CHAT_CONTEXT_ENABLED"):
            details["chat_context"] = await kommo_chat_service.get_lead_chat_context(
                lead_id
            )
        return details

    kommo_service.get_lead_details = get_lead_details_with_chat

    original_format_lead_summary = agent_tools.format_lead_summary

    def format_lead_summary_with_chat(lead: dict[str, Any]) -> str:
        base = original_format_lead_summary(lead)
        section = _chat_section(dict(lead.get("chat_context") or {}))
        if not section:
            return base
        link_marker = "\n\n<a href="
        if link_marker in base:
            body, link = base.rsplit(link_marker, 1)
            return (body + "\n\n" + section + link_marker + link)[:4000]
        return (base + "\n\n" + section)[:4000]

    agent_tools.format_lead_summary = format_lead_summary_with_chat
    telegram_service.format_status_sync_report = _format_onboarding_report
    telegram_service.send_status_sync_report = _send_onboarding_report
    telegram_service.send_status_sync_confirmation = _send_onboarding_confirmation
    telegram_service.send_status_sync_result = _send_onboarding_result
