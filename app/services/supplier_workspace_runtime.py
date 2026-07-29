"""Telegram runtime for project suppliers, inquiries and offer comparison."""
from __future__ import annotations

import html
from typing import Any

from app.agent import tools as agent_tools
from app.agent.lead_refs import extract_internal_lead_number
from app.services import (
    identity_service,
    supplier_workspace_service,
    telegram_service,
    telegram_state_service,
)

_INSTALLED = False


def _assert_writer() -> None:
    if not identity_service.can_write(identity_service.current_user()):
        raise PermissionError("Роль Viewer позволяет только просматривать данные.")


def _add_supplier_button(markup: dict[str, Any], lead_id: int) -> dict[str, Any]:
    rows = [list(row) for row in (markup.get("inline_keyboard") or [])]
    button = {
        "text": "🏭 Фабрики и предложения",
        "callback_data": f"supplier:workspace:{int(lead_id)}",
    }
    insert_at = (
        max(0, len(rows) - 1)
        if rows and any("url" in item for item in rows[-1])
        else len(rows)
    )
    rows.insert(insert_at, [button])
    return {"inline_keyboard": rows}


def _workspace_markup(
    *,
    lead_id: int,
    suppliers: list[Any],
    lead_url: str | None = None,
) -> dict[str, Any]:
    rows: list[list[dict[str, str]]] = [
        [
            {
                "text": "➕ Добавить фабрику",
                "callback_data": f"supplier:add:{int(lead_id)}",
            },
            {
                "text": "📊 Сравнить предложения",
                "callback_data": f"supplier:compare:{int(lead_id)}",
            },
        ]
    ]
    for supplier in suppliers[:16]:
        rows.append(
            [
                {
                    "text": f"🏭 {str(supplier.name)[:48]}",
                    "callback_data": f"supplier:view:{int(supplier.id)}",
                }
            ]
        )
    if lead_url:
        rows.append([{"text": "🔗 Открыть Kommo", "url": str(lead_url)}])
    return {"inline_keyboard": rows}


def _supplier_markup(supplier: Any) -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {
                    "text": "📤 Запрос отправлен",
                    "callback_data": f"supplier:inquiry:{int(supplier.id)}",
                },
                {
                    "text": "➕ Добавить предложение",
                    "callback_data": f"supplier:offer:{int(supplier.id)}",
                },
            ],
            [
                {
                    "text": "⬅️ К списку фабрик",
                    "callback_data": f"supplier:workspace:{int(supplier.kommo_lead_id)}",
                }
            ],
        ]
    }


def _format_supplier(supplier: Any) -> str:
    status = {
        "candidate": "кандидат",
        "inquiry_sent": "запрос отправлен, ждём ответ",
        "offer_received": "предложение получено",
        "rejected": "отклонён",
    }.get(str(supplier.status), str(supplier.status))
    verification = {
        "not_checked": "не проверена",
        "in_progress": "проверка идёт",
        "verified": "проверена",
        "risk": "обнаружены риски",
    }.get(str(supplier.verification_status), str(supplier.verification_status))
    lines = [
        f"<b>🏭 {html.escape(str(supplier.name))}</b>",
        "",
        f"Статус: <b>{html.escape(status)}</b>",
        f"Проверка: {html.escape(verification)}",
        f"Платформа: {html.escape(str(supplier.platform or '—'))}",
        f"Контакт: {html.escape(str(supplier.contact_value or '—'))}",
    ]
    if supplier.last_contact_at:
        lines.append(
            f"Последний запрос: {supplier.last_contact_at.astimezone().strftime('%d.%m.%Y %H:%M')}"
        )
    if supplier.next_followup_at:
        lines.append(
            f"Проверить ответ: <b>{supplier.next_followup_at.astimezone().strftime('%d.%m.%Y %H:%M')}</b>"
        )
    if supplier.notes:
        lines.extend(["", "<b>Заметки</b>", html.escape(str(supplier.notes)[:1500])])
    if supplier.source_url:
        lines.extend(
            [
                "",
                f'<a href="{html.escape(str(supplier.source_url), quote=True)}">Открыть страницу фабрики</a>',
            ]
        )
    return "\n".join(lines)[:4000]


async def _show_workspace(db: Any, *, chat_id: int, lead_id: int) -> None:
    lead = await agent_tools.kommo_service.get_lead_details(int(lead_id))
    suppliers = await supplier_workspace_service.list_suppliers(
        db, kommo_lead_id=int(lead_id)
    )
    records = await supplier_workspace_service.list_offers(
        db, kommo_lead_id=int(lead_id)
    )
    await telegram_service.send_message(
        chat_id,
        supplier_workspace_service.format_workspace(
            lead_name=str(lead.get("name") or lead_id),
            suppliers=suppliers,
            offer_count=len(records),
        ),
        reply_markup=_workspace_markup(
            lead_id=int(lead_id), suppliers=suppliers, lead_url=lead.get("url")
        ),
    )


async def _show_comparison(db: Any, *, chat_id: int, lead_id: int) -> None:
    lead = await agent_tools.kommo_service.get_lead_details(int(lead_id))
    comparison = await supplier_workspace_service.compare_offers(
        db, kommo_lead_id=int(lead_id)
    )
    await telegram_service.send_message(
        chat_id,
        supplier_workspace_service.format_comparison(
            comparison, lead_name=str(lead.get("name") or lead_id)
        ),
        reply_markup={
            "inline_keyboard": [
                [
                    {
                        "text": "➕ Добавить фабрику",
                        "callback_data": f"supplier:add:{int(lead_id)}",
                    },
                    {
                        "text": "⬅️ К фабрикам",
                        "callback_data": f"supplier:workspace:{int(lead_id)}",
                    },
                ]
            ]
        },
    )


async def _handle_supplier_callback(
    *,
    callback_data: str,
    chat_id: int,
    user_id: int,
    db: Any,
) -> bool:
    if not callback_data.startswith("supplier:"):
        return False
    parts = callback_data.split(":")
    command = parts[1] if len(parts) > 1 else ""

    if command == "workspace" and len(parts) == 3:
        await _show_workspace(db, chat_id=chat_id, lead_id=int(parts[2]))
        return True

    if command == "view" and len(parts) == 3:
        supplier = await supplier_workspace_service.get_supplier(db, int(parts[2]))
        if supplier is None:
            raise ValueError("Фабрика не найдена.")
        await telegram_service.send_message(
            chat_id, _format_supplier(supplier), reply_markup=_supplier_markup(supplier)
        )
        return True

    _assert_writer()

    if command == "add" and len(parts) == 3:
        lead_id = int(parts[2])
        lead = await agent_tools.kommo_service.get_lead_details(lead_id)
        internal = extract_internal_lead_number(lead)
        await telegram_state_service.set_state(
            user_id,
            {
                "mode": "supplier_add",
                "chat_id": chat_id,
                "kommo_lead_id": lead_id,
                "internal_lead_number": internal,
            },
            ttl_seconds=60 * 30,
        )
        await telegram_service.send_message(
            chat_id,
            (
                "🏭 <b>Добавление фабрики</b>\n\n"
                "Отправьте одной строкой:\n"
                "<code>Название | ссылка | контакт | заметка</code>\n\n"
                "Обязательное поле — только название."
            ),
        )
        return True

    if command == "inquiry" and len(parts) == 3:
        supplier_id = int(parts[2])
        inquiry = await supplier_workspace_service.record_inquiry_sent(
            db,
            supplier_id=supplier_id,
            telegram_user_id=user_id,
            followup_days=3,
        )
        supplier = await supplier_workspace_service.get_supplier(db, supplier_id)
        await telegram_service.send_message(
            chat_id,
            (
                "✅ Запрос фабрике отмечен как отправленный.\n"
                f"Проверить ответ: <b>{inquiry.due_at.astimezone().strftime('%d.%m.%Y %H:%M')}</b>"
            ),
            reply_markup=_supplier_markup(supplier),
        )
        return True

    if command == "offer" and len(parts) == 3:
        supplier_id = int(parts[2])
        supplier = await supplier_workspace_service.get_supplier(db, supplier_id)
        if supplier is None:
            raise ValueError("Фабрика не найдена.")
        await telegram_state_service.set_state(
            user_id,
            {
                "mode": "supplier_offer_add",
                "chat_id": chat_id,
                "supplier_id": supplier_id,
                "kommo_lead_id": int(supplier.kommo_lead_id),
            },
            ttl_seconds=60 * 45,
        )
        await telegram_service.send_message(
            chat_id,
            (
                f"📥 <b>Предложение · {html.escape(str(supplier.name))}</b>\n\n"
                "Отправьте данные через <code>|</code>:\n"
                "<code>валюта | Incoterm и место | цена/ед. | total | MOQ | срок дней | гарантия мес. | оплата | сертификаты | заметка</code>\n\n"
                "Пример:\n"
                "<code>USD | FOB Qingdao | 17900 | 17900 | 1 set | 50 | 18 | 30/70 | CE, ISO | двигатель уточняется</code>"
            ),
        )
        return True

    if command == "compare" and len(parts) == 3:
        await _show_comparison(db, chat_id=chat_id, lead_id=int(parts[2]))
        return True

    raise ValueError("Неизвестная команда фабрик.")


def install_supplier_workspace_runtime() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True

    original_actions_markup = agent_tools.lead_card_actions_markup

    def lead_card_actions_with_suppliers(lead: dict[str, Any]) -> dict[str, Any]:
        markup = original_actions_markup(lead)
        lead_id = int(lead.get("id") or lead.get("kommo_lead_id") or 0)
        return _add_supplier_button(markup, lead_id) if lead_id else markup

    agent_tools.lead_card_actions_markup = lead_card_actions_with_suppliers

    from app.api import telegram as telegram_api

    original_manager_callback = telegram_api._handle_manager_callback

    async def manager_callback_with_suppliers(**kwargs: Any) -> bool:
        callback_data = str(kwargs.get("callback_data") or "")
        if callback_data.startswith("supplier:"):
            return await _handle_supplier_callback(**kwargs)
        return await original_manager_callback(**kwargs)

    telegram_api._handle_manager_callback = manager_callback_with_suppliers

    original_text_state = telegram_api._handle_text_state

    async def text_state_with_suppliers(**kwargs: Any) -> bool:
        user_id = int(kwargs["user_id"])
        state = await telegram_state_service.get_state(user_id)
        if not state or state.get("mode") not in {"supplier_add", "supplier_offer_add"}:
            return await original_text_state(**kwargs)
        _assert_writer()
        db = kwargs["db"]
        chat_id = int(kwargs["chat_id"])
        text = str(kwargs.get("text") or "")

        if state.get("mode") == "supplier_add":
            values = supplier_workspace_service.parse_supplier_line(text)
            supplier = await supplier_workspace_service.create_supplier(
                db,
                kommo_lead_id=int(state["kommo_lead_id"]),
                internal_lead_number=state.get("internal_lead_number"),
                telegram_user_id=user_id,
                **values,
            )
            await telegram_state_service.clear_state(user_id)
            await telegram_service.send_message(
                chat_id,
                f"✅ Фабрика <b>{html.escape(str(supplier.name))}</b> добавлена.",
            )
            await _show_workspace(
                db, chat_id=chat_id, lead_id=int(state["kommo_lead_id"])
            )
            return True

        values = supplier_workspace_service.parse_offer_line(text)
        offer = await supplier_workspace_service.create_offer(
            db,
            supplier_id=int(state["supplier_id"]),
            telegram_user_id=user_id,
            **values,
        )
        await telegram_state_service.clear_state(user_id)
        await telegram_service.send_message(
            chat_id,
            f"✅ Предложение <code>{offer.id}</code> сохранено и нормализовано.",
        )
        await _show_comparison(
            db, chat_id=chat_id, lead_id=int(state["kommo_lead_id"])
        )
        return True

    telegram_api._handle_text_state = text_state_with_suppliers
