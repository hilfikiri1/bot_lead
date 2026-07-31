"""Kommo-first, confirmation-first onboarding of new Facebook leads."""
from __future__ import annotations

import asyncio
import hashlib
import html
import json
import logging
import re
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

from app.api import telegram as telegram_api
from app.config import get_settings
from app.services import (
    ai_analysis_service,
    google_sheets_service,
    identity_service,
    kommo_service,
    lead_status_sync_service,
    product_title_service,
    telegram_service,
    telegram_state_service,
)
from app.services.google_sheets_service import SpreadsheetRow
from app.services.lead_matching_service import lead_contact_snapshot, match_lead_to_rows
from app.services.unreviewed_leads_service import build_proposed_name

logger = logging.getLogger(__name__)
settings = get_settings()
_INSTALLED = False
_GENERIC = {"narzedzia", "narzędzia", "tools", "tool", "sprzet", "sprzęt", "equipment", "produkty", "towar"}


def _clean(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()


def _fold(value: Any) -> str:
    return re.sub(r"[^\wąćęłńóśźż]+", " ", _clean(value).casefold().replace("ё", "е")).strip()


def _esc(value: Any) -> str:
    return html.escape(_clean(value) or "—")


def _tz() -> ZoneInfo:
    try:
        return ZoneInfo(settings.manager_timezone or "Europe/Warsaw")
    except Exception:
        return ZoneInfo("Europe/Warsaw")


def _is_facebook(lead: dict[str, Any]) -> bool:
    metadata = lead.get("metadata") or {}
    text = " ".join(
        [_clean(lead.get(key)) for key in ("name", "source_name", "category", "unsorted_uid")]
        + [_clean(value) for value in metadata.values()]
    ).casefold()
    return "facebook" in text or "fb" in text.split()


def _fingerprint(row: SpreadsheetRow) -> list[str]:
    return [_clean(row.phone).casefold(), _clean(row.email).casefold(), _clean(row.client_name).casefold(), _clean(row.product).casefold()]


def _lead_number(row: SpreadsheetRow) -> str:
    return _clean(row.lead_number) or str(int(row.row_number))


def _product_ru_source(product: str | None) -> str | None:
    folded = _fold(product)
    if folded in {"narzedzia", "narzędzia"}:
        return "Инструменты"
    return None


async def _product_ru(product: str | None) -> str:
    return _product_ru_source(product) or await product_title_service.short_product_title(product)


def _priority(row: SpreadsheetRow, analysis: dict[str, Any]) -> dict[str, str]:
    product = _fold(row.product)
    generic = not product or product in _GENERIC
    lead = analysis.get("lead") or {}
    missing = list(analysis.get("missing_questions") or [])
    budget = _fold(row.budget)
    high_budget = any(token in budget for token in ("20000", "20 000", "20_000", "powyzej", "powyżej"))
    if not generic and _clean(lead.get("quantity")) and (_clean(lead.get("timeline")) or not missing):
        return {"grade": "A", "potential": "высокий", "readiness": "высокая"}
    if not generic or high_budget:
        return {"grade": "B", "potential": "средний или высокий", "readiness": "средняя"}
    return {"grade": "C", "potential": "средний", "readiness": "низкая — требуется квалификация"}


def _channel(row: SpreadsheetRow) -> str:
    value = _fold(row.contact_channel)
    if any(token in value for token in ("telefon", "phone", "call", "polaczenie", "połączenie")):
        return "call"
    if any(token in value for token in ("whatsapp", "whats app", "whats_app")):
        return "whatsapp"
    return "whatsapp" if _clean(row.phone) else "email" if _clean(row.email) else "manual"


def _due_at(channel: str) -> datetime:
    now = datetime.now(_tz())
    target = now + timedelta(hours=1 if channel == "call" else 2)
    if target.hour >= 18:
        target = (target + timedelta(days=1)).replace(hour=10, minute=0, second=0, microsecond=0)
    while target.weekday() >= 5:
        target = (target + timedelta(days=1)).replace(hour=10, minute=0, second=0, microsecond=0)
    return target


def _missing(row: SpreadsheetRow) -> list[str]:
    product = _clean(row.product) or "товар"
    first = f"конкретный перечень или модели по категории «{product}»"
    if "narz" in _fold(product) or "инструмент" in _fold(product):
        first = "виды инструментов: ручные, аккумуляторные, строительные, садовые или промышленные"
    return [first, "фотографии, ссылки или характеристики", "количество по каждой позиции", "назначение закупки и информация о компании", "город доставки и срок закупки"]


def _whatsapp(row: SpreadsheetRow) -> str:
    first = (_clean(row.client_name).split() or [""])[0]
    greeting = f"Panie {first}" if first else ""
    product = _clean(row.product) or "produktów"
    budget = f" z budżetem {_clean(row.budget)}" if _clean(row.budget) else ""
    return (
        f"Dzień dobry {greeting},\nz tej strony Kirill z firmy Buy & Bring Solutions. "
        f"Otrzymaliśmy Pana zapytanie dotyczące zakupu {product}{budget}.\n\n"
        "Pomagamy w zakupach bezpośrednio od producentów w Chinach: wyszukiwaniu i weryfikacji fabryk, negocjacjach, kontroli jakości, logistyce oraz dokumentacji importowej.\n\n"
        "Żeby dobrze ocenić projekt, proszę o krótką informację:\n"
        "1. Jakiego rodzaju produkty lub modele Pana interesują?\n"
        "2. Czy ma Pan listę, zdjęcia albo linki do podobnych modeli?\n"
        "3. Jakie ilości planuje Pan zamówić?\n"
        "4. Czy zakup jest dla firmy, sklepu lub dalszej odsprzedaży?\n"
        "5. Do jakiego miasta ma być dostarczony towar?\n\n"
        "Po otrzymaniu tych informacji ocenimy możliwości zakupu i zaproponujemy kolejny etap."
    ).replace("Dzień dobry ,", "Dzień dobry,")


def _call_script(row: SpreadsheetRow) -> str:
    first = (_clean(row.client_name).split() or [""])[0]
    greeting = f"Panie {first}" if first else ""
    return (
        f"Dzień dobry {greeting}, z tej strony Kirill z Buy & Bring Solutions. Otrzymaliśmy Pana zapytanie z Facebooka i chciałbym krótko doprecyzować projekt.\n\n"
        "1. Jakie dokładnie produkty lub modele są potrzebne?\n"
        "2. Jakie ilości planuje Pan zamówić?\n"
        "3. Czy zakup jest dla firmy, sklepu czy dalszej odsprzedaży?\n"
        "4. Czy potrzebna jest własna marka/OEM?\n"
        "5. Do jakiego miasta i w jakim terminie ma być dostawa?\n\n"
        "Na końcu poprosić o przesłanie listy, zdjęć lub linków przez WhatsApp."
    ).replace("Dzień dobry ,", "Dzień dobry,")


def _fallback_analysis(row: SpreadsheetRow, product_ru: str) -> dict[str, Any]:
    return {
        "lead": {"product_requested": product_ru, "quantity": None, "timeline": None},
        "conversation_summary": f"Клиент указал запрос «{_clean(row.product) or product_ru}». Для оценки поставщиков, стоимости и маржинальности данных пока недостаточно.",
        "confirmed_facts": [item for item in (f"Категория: {product_ru}", f"Бюджет: {_clean(row.budget)}" if _clean(row.budget) else "", f"Канал: {_clean(row.contact_channel)}" if _clean(row.contact_channel) else "") if item],
        "missing_questions": _missing(row),
        "risks": ["категория товара указана слишком широко", "неизвестны количество, назначение, сроки и город доставки"],
        "recommended_next_step": "Получить перечень, фотографии или ссылки и только после этого определять производителей.",
        "whatsapp": {"message": _whatsapp(row)},
    }


async def _analysis(row: SpreadsheetRow, details: dict[str, Any], product_ru: str) -> dict[str, Any]:
    contact = ((details.get("contacts") or [{}])[0]) or {}
    prompt = "\n".join([
        "Новая рекламная B2B-заявка из Facebook. Это данные формы, не разговор.",
        f"Клиент: {_clean(row.client_name) or _clean(contact.get('name')) or 'не указан'}",
        f"Телефон: {_clean(row.phone) or _clean((contact.get('phones') or [''])[0]) or 'не указан'}",
        f"Email: {_clean(row.email) or _clean((contact.get('emails') or [''])[0]) or 'не указан'}",
        f"Компания: {_clean(row.company) or 'не указана'}",
        f"Товар: {_clean(row.product) or 'не указан'}; перевод: {product_ru}",
        f"Бюджет: {_clean(row.budget) or 'не указан'}",
        f"Канал: {_clean(row.contact_channel) or 'не указан'}",
        f"Регион: {_clean(row.region) or 'не указан'}",
        "Сделай первичную квалификацию без выдумывания фактов, перечисли недостающие данные и подготовь польское WhatsApp-сообщение.",
    ])
    try:
        result = await ai_analysis_service.analyse_transcript(prompt)
    except Exception as exc:
        logger.warning("Initial lead analysis fallback: %s", exc)
        result = _fallback_analysis(row, product_ru)
    result["missing_questions"] = list(result.get("missing_questions") or _missing(row))
    result["risks"] = list(result.get("risks") or [])
    result["confirmed_facts"] = list(result.get("confirmed_facts") or [])
    result.setdefault("whatsapp", {})["message"] = _clean((result.get("whatsapp") or {}).get("message")) or _whatsapp(row)
    return result


def _digest(row: SpreadsheetRow, details: dict[str, Any], number: str, product_ru: str) -> str:
    payload = {"lead": details.get("id"), "row": row.row_number, "fingerprint": _fingerprint(row), "number": number, "product": product_ru}
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:24]


def _note(preview: dict[str, Any]) -> str:
    analysis = preview["analysis"]
    priority = preview["priority"]
    lines = [
        f"[BBS-SMART-ONBOARD-{preview['lead_number']}-{preview['lead_id']}]",
        f"{datetime.now(_tz()).strftime('%d.%m.%Y')} — заявка / {preview['contact_channel'] or 'Facebook'}", "",
        f"Клиент: {preview['client_name'] or 'не указан'}", f"Телефон: {preview['phone'] or 'не указан'}", f"Email: {preview['email'] or 'не указан'}", "",
        f"Клиент указал интерес к закупке: {preview['product_ru']}.", f"Исходный запрос: {preview['product_original'] or 'не указан'}.", f"Заявленный бюджет: {preview['budget'] or 'не указан'}.", "",
        "Личный анализ:", _clean(analysis.get("conversation_summary")) or "Требуется квалификация.",
        f"Потенциал: {priority['potential']}.", f"Готовность: {priority['readiness']}.", f"Приоритет: {priority['grade']} — {'квалификация' if priority['grade'] == 'C' else 'развитие проекта'}.",
    ]
    if analysis.get("risks"):
        lines += ["", "Основные риски:"] + [f"– {_clean(item)}" for item in analysis["risks"][:6]]
    if analysis.get("confirmed_facts"):
        lines += ["", "Что уже получено:"] + [f"– {_clean(item)}" for item in analysis["confirmed_facts"][:8]]
    lines += ["", "Что запросить у клиента:"] + [f"– {_clean(item)}" for item in analysis["missing_questions"][:8]]
    lines += ["", "Что должны сделать мы:", _clean(analysis.get("recommended_next_step")) or "Квалифицировать запрос.", "",
              f"Рекомендуемый канал: {preview['recommended_channel']}.", f"Следующее действие: {preview['task_due_display']} — {preview['task_text']}", f"Контроль ответа: {preview['followup_display']}.", "",
              "Готовое сообщение клиенту:", preview["whatsapp_message"], "", "Название следующей задачи в Kommo:", preview["task_text"]]
    return "\n".join(lines)[:13500]


async def discover() -> dict[str, Any]:
    rows, unsorted = await asyncio.gather(
        asyncio.to_thread(google_sheets_service.get_rows, force_refresh=True),
        kommo_service.get_all_unsorted_leads(pipeline_id=settings.kommo_unreviewed_pipeline_id or settings.lead_status_sync_pipeline_id or None),
    )
    leads = await kommo_service.enrich_leads_with_contacts(list(unsorted.get("leads") or []))
    used_rows: set[int] = set()
    queue, unmatched = [], []
    for lead in sorted(leads, key=lambda item: item.get("created_at") or 0):
        if not _is_facebook(lead):
            continue
        match = match_lead_to_rows(phones=lead.get("phones"), emails=lead.get("emails"), contact_name=lead.get("contact_name"), company=lead.get("company"), product_hint=lead.get("name"), rows=rows, require_lead_number=False)
        candidate = match.single
        if candidate is None or candidate.score < 60 or candidate.row.row_number in used_rows:
            unmatched.append({"lead_id": lead.get("id"), "name": lead.get("name"), "candidate_rows": [item.row.row_number for item in match.candidates]})
            continue
        used_rows.add(candidate.row.row_number)
        queue.append({"lead_id": int(lead["id"]), "row_number": candidate.row.row_number, "matched_by": candidate.matched_by})
    return {"items": queue, "unmatched": unmatched}


async def build_preview(item: dict[str, Any]) -> dict[str, Any]:
    rows, details = await asyncio.gather(asyncio.to_thread(google_sheets_service.get_rows, force_refresh=True), kommo_service.get_lead_details(int(item["lead_id"])))
    row = next((row for row in rows if row.row_number == int(item["row_number"])), None)
    if row is None:
        raise ValueError("Строка Google Sheets больше не найдена.")
    snap = lead_contact_snapshot(details)
    match = match_lead_to_rows(phones=snap.get("phones"), emails=snap.get("emails"), contact_name=snap.get("contact_name"), company=snap.get("company"), product_hint=details.get("name"), rows=[row], require_lead_number=False)
    if match.single is None or match.single.score < 60:
        raise ValueError("Контактные данные Kommo и Google Sheets больше не совпадают.")
    number = _lead_number(row)
    product_ru = await _product_ru(row.product)
    analysis = await _analysis(row, details, product_ru)
    channel = _channel(row)
    due = _due_at(channel)
    followup = due + timedelta(days=2)
    missing = ", ".join(_clean(value).rstrip(".") for value in analysis["missing_questions"][:4])
    action = "Позвонить" if channel == "call" else "Написать в WhatsApp" if channel == "whatsapp" else "Связаться"
    task = f"{action} {_clean(row.client_name) or 'клиенту'} и получить: {missing} · №{number}"[:500]
    pipeline = details.get("pipeline_id") if isinstance(details.get("pipeline_id"), int) else None
    status = await lead_status_sync_service._first_contact_status_id(pipeline)
    contact = ((details.get("contacts") or [{}])[0]) or {}
    preview = {
        "lead_id": int(details["id"]), "row_number": row.row_number, "lead_number": number, "old_y": _clean(row.lead_number),
        "old_name": _clean(details.get("name")), "new_name": build_proposed_name(number, product_ru), "target_status_id": status,
        "matched_by": item.get("matched_by") or match.single.matched_by, "match_score": match.single.score,
        "product_original": _clean(row.product), "product_ru": product_ru, "client_name": _clean(row.client_name) or _clean(contact.get("name")),
        "phone": _clean(row.phone) or _clean((contact.get("phones") or [""])[0]), "email": _clean(row.email) or _clean((contact.get("emails") or [""])[0]),
        "budget": _clean(row.budget), "contact_channel": _clean(row.contact_channel), "analysis": analysis, "priority": _priority(row, analysis),
        "recommended_channel": channel, "whatsapp_message": _clean((analysis.get("whatsapp") or {}).get("message")) or _whatsapp(row), "call_script": _call_script(row),
        "task_text": task, "task_due_at": int(due.timestamp()), "task_due_display": due.strftime("%d.%m.%Y %H:%M"), "followup_display": followup.strftime("%d.%m.%Y %H:%M"),
        "row_fingerprint": _fingerprint(row), "digest": _digest(row, details, number, product_ru), "kommo_url": details.get("url"),
    }
    preview["analysis_note"] = _note(preview)
    return preview


async def apply(preview: dict[str, Any]) -> dict[str, Any]:
    rows, details = await asyncio.gather(asyncio.to_thread(google_sheets_service.get_rows, force_refresh=True), kommo_service.get_lead_details(int(preview["lead_id"])))
    row = next((row for row in rows if row.row_number == int(preview["row_number"])), None)
    if row is None or _digest(row, details, str(preview["lead_number"]), str(preview["product_ru"])) != preview.get("digest"):
        return {"stale": True, "reason": "source_changed"}
    snap = lead_contact_snapshot(details)
    match = match_lead_to_rows(phones=snap.get("phones"), emails=snap.get("emails"), contact_name=snap.get("contact_name"), company=snap.get("company"), product_hint=details.get("name"), rows=[row], require_lead_number=False)
    if match.single is None or match.single.score < 60:
        return {"stale": True, "reason": "contact_match_changed"}
    desired = str(preview["lead_number"])
    current_y = _clean(row.lead_number)
    if current_y not in {"", desired}:
        return {"stale": True, "reason": "lead_number_changed"}
    sheet = {"updated_count": 0}
    if current_y != desired:
        sheet = await asyncio.to_thread(google_sheets_service.apply_lead_registry_updates, [{"row_number": row.row_number, "row_fingerprint": preview["row_fingerprint"], "old_lead_number": current_y, "new_lead_number": desired, "old_comment": _clean(row.marketing_comment), "new_comment": _clean(row.marketing_comment)}])
        if int(sheet.get("updated_count") or 0) != 1:
            return {"stale": True, "reason": "sheet_write_not_applied"}
    existing_number = lead_status_sync_service.parse_internal_number(_clean(details.get("name")))
    if existing_number and existing_number != desired:
        return {"stale": True, "reason": "kommo_number_conflict"}
    changes: dict[str, Any] = {}
    if _clean(details.get("name")) != preview["new_name"]:
        changes["name"] = preview["new_name"]
    if isinstance(preview.get("target_status_id"), int) and details.get("status_id") != preview["target_status_id"]:
        changes["status_id"] = preview["target_status_id"]
    if changes:
        await kommo_service.update_kommo_lead(int(preview["lead_id"]), **changes)
    marker = f"[BBS-SMART-ONBOARD-{desired}-{preview['lead_id']}]"
    notes = await kommo_service.get_recent_common_notes(int(preview["lead_id"]), limit=50)
    note_added = not any(marker in _clean(note.get("text")) for note in notes)
    if note_added:
        await kommo_service.add_common_note(int(preview["lead_id"]), preview["analysis_note"])
    tasks = await kommo_service.get_open_lead_tasks(int(preview["lead_id"]), limit=50)
    task_added = not any(f"№{desired}" in _clean(task.get("text")) for task in tasks)
    if task_added:
        await kommo_service.create_lead_task(lead_id=int(preview["lead_id"]), text=preview["task_text"], complete_till=int(preview["task_due_at"]), responsible_user_id=details.get("responsible_user_id"))
    return {"stale": False, "lead_number": desired, "new_name": preview["new_name"], "sheet_updated": bool(sheet.get("updated_count")), "kommo_updated": bool(changes), "note_added": note_added, "task_added": task_added, "kommo_url": preview.get("kommo_url")}


def _bullets(values: list[Any]) -> str:
    return "\n".join(f"• {_esc(value)}" for value in values[:6] if _clean(value)) or "• не определено"


def _card(preview: dict[str, Any], position: int, total: int) -> str:
    analysis, priority = preview["analysis"], preview["priority"]
    channel = {"whatsapp": "сначала написать в WhatsApp", "call": "сначала позвонить", "email": "сначала написать email"}.get(preview["recommended_channel"], "уточнить канал")
    lines = [f"🆕 <b>НОВЫЙ FACEBOOK-ЛИД · {position}/{total}</b>", "", f"Kommo: <b>{_esc(preview['old_name'])}</b>", f"Google Sheets: строка <b>{preview['row_number']}</b>", f"Сопоставление: <b>{_esc(preview['matched_by'])}</b> · {preview['match_score']}%", "", f"ID и название: <b>{_esc(preview['new_name'])}</b>", f"Клиент: {_esc(preview['client_name'])}", f"Телефон: <code>{_esc(preview['phone'])}</code>", f"Email: {_esc(preview['email'])}", f"Бюджет: {_esc(preview['budget'])}", f"Товар: {_esc(preview['product_original'])} → <b>{_esc(preview['product_ru'])}</b>", "", "<b>Личный анализ</b>", _esc(analysis.get("conversation_summary")), f"Потенциал: <b>{_esc(priority['potential'])}</b>", f"Готовность: <b>{_esc(priority['readiness'])}</b>", f"Приоритет: <b>{priority['grade']}</b>"]
    if analysis.get("risks"):
        lines += ["", "<b>Основные риски</b>", _bullets(analysis["risks"])]
    lines += ["", "<b>Что запросить</b>", _bullets(analysis["missing_questions"]), "", f"<b>Рекомендация:</b> {channel}", f"<b>Одна задача:</b> {_esc(preview['task_text'])}", f"<b>Срок:</b> {preview['task_due_display']}", "", "<b>После подтверждения</b>", "Y → новое название → Первый контакт → подробное примечание → одна задача."]
    if preview["recommended_channel"] == "whatsapp":
        lines += ["", "<b>Готовое сообщение</b>", _esc(preview["whatsapp_message"])]
    return "\n".join(lines)[:4000]


def _markup(preview: dict[str, Any]) -> dict[str, Any]:
    rows = [[{"text": "✅ Обработать и следующий", "callback_data": "sync:confirm"}]]
    digits = re.sub(r"\D", "", preview.get("phone") or "")
    if digits and preview["recommended_channel"] == "whatsapp":
        rows.append([{"text": "💬 Открыть готовый WhatsApp", "url": f"https://wa.me/{digits}?text={quote(preview['whatsapp_message'][:1800])}"}])
    if digits:
        rows.append([{"text": "📞 Сценарий звонка", "callback_data": "sync:call"}])
    rows += [[{"text": "⏭ Пропустить", "callback_data": "sync:skip"}], [{"text": "❌ Завершить", "callback_data": "sync:cancel"}]]
    return {"inline_keyboard": rows}


async def _show_current(chat_id: int, user_id: int, state: dict[str, Any]) -> None:
    queue, index = list(state.get("queue") or []), int(state.get("index") or 0)
    if index >= len(queue):
        await telegram_state_service.clear_state(user_id)
        await telegram_service.send_message(chat_id, f"🏁 <b>ОБРАБОТКА ЗАВЕРШЕНА</b>\n\nОбработано: <b>{len(state.get('results') or [])}</b>\nПропущено: <b>{len(state.get('skipped') or [])}</b>\nНе сопоставлено: <b>{len(state.get('unmatched') or [])}</b>\n\nНовые сделки не создавались. W и X не изменялись.")
        return
    await telegram_service.send_message(chat_id, f"🧠 Анализирую лид {index + 1} из {len(queue)}…")
    try:
        preview = await build_preview(queue[index])
    except Exception as exc:
        state["current_preview"] = None
        await telegram_state_service.set_state(user_id, state, ttl_seconds=settings.telegram_state_ttl_minutes * 60)
        await telegram_service.send_message(chat_id, f"❌ Не удалось подготовить карточку: {_esc(exc)}", reply_markup={"inline_keyboard": [[{"text": "🔄 Повторить", "callback_data": "sync:current"}], [{"text": "⏭ Пропустить", "callback_data": "sync:skip"}]]})
        return
    state["current_preview"] = preview
    await telegram_state_service.set_state(user_id, state, ttl_seconds=settings.telegram_state_ttl_minutes * 60)
    await telegram_service.send_message(chat_id, _card(preview, index + 1, len(queue)), reply_markup=_markup(preview))


async def _run(chat_id: int, user_id: int) -> None:
    if not telegram_api._is_allowed_user(user_id):
        await telegram_service.send_message(chat_id, "Доступ запрещён.")
        return
    actor = identity_service.current_user()
    if actor is not None and actor.lead_access_scope == "assigned":
        await telegram_service.send_message(chat_id, "🔒 Обработка входящих лидов доступна Owner/Admin.")
        return
    await telegram_service.send_message(chat_id, "🔄 Читаю Неразобранное Kommo и Google Sheets. Ничего не изменяю…")
    try:
        found = await discover()
    except Exception as exc:
        await telegram_service.send_message(chat_id, f"❌ Не удалось прочитать лиды: {_esc(exc)}")
        return
    queue = list(found["items"])
    if not queue:
        await telegram_service.send_message(chat_id, f"✅ Новых Facebook-лидов с точным совпадением нет.\nНе сопоставлено: <b>{len(found['unmatched'])}</b>.")
        return
    state = {"mode": "smart_lead_onboarding", "queue": queue, "index": 0, "results": [], "skipped": [], "unmatched": found["unmatched"], "current_preview": None}
    await telegram_state_service.set_state(user_id, state, ttl_seconds=settings.telegram_state_ttl_minutes * 60)
    await _show_current(chat_id, user_id, state)


async def _confirm(chat_id: int, user_id: int, state: dict[str, Any]) -> None:
    preview = dict(state.get("current_preview") or {})
    if not preview:
        await _show_current(chat_id, user_id, state)
        return
    if not settings.google_sheets_write_enabled:
        await telegram_service.send_message(chat_id, "🔒 Нужны GOOGLE_SHEETS_WRITE_ENABLED=true и право Editor.")
        return
    await telegram_service.send_message(chat_id, "🔐 Повторная проверка. Сначала Y, затем Kommo…")
    result = await apply(preview)
    if result.get("stale"):
        await telegram_service.send_message(chat_id, f"⚠️ Лид не обработан: <code>{_esc(result.get('reason'))}</code>.")
    else:
        await telegram_service.send_message(chat_id, f"✅ <b>ЛИД №{result['lead_number']} ОБРАБОТАН</b>\nНазвание: <b>{_esc(result['new_name'])}</b>\nY: {'обновлён' if result['sheet_updated'] else 'уже был'}\nПримечание: {'добавлено' if result['note_added'] else 'уже было'}\nЗадача: {'добавлена' if result['task_added'] else 'уже была'}" + (f"\n<a href=\"{html.escape(str(result['kommo_url']), quote=True)}\">Открыть Kommo</a>" if result.get("kommo_url") else ""))
    state["results"] = list(state.get("results") or []) + [result]
    state["index"] = int(state.get("index") or 0) + 1
    state["current_preview"] = None
    await telegram_state_service.set_state(user_id, state, ttl_seconds=settings.telegram_state_ttl_minutes * 60)
    await _show_current(chat_id, user_id, state)


async def _call(chat_id: int, preview: dict[str, Any]) -> None:
    await telegram_service.send_message(chat_id, f"📞 <b>ПОДГОТОВКА К ЗВОНКУ</b>\n\nКлиент: <b>{_esc(preview.get('client_name'))}</b>\nТелефон: <code>{_esc(preview.get('phone'))}</code>\n\n{_esc(preview.get('call_script'))}\n\nПосле разговора отправь итог боту — он добавит его в проект.", reply_markup={"inline_keyboard": [[{"text": "⬅️ К карточке", "callback_data": "sync:current"}], [{"text": "✅ Обработать и следующий", "callback_data": "sync:confirm"}]]})


def install_smart_lead_onboarding_runtime() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True
    original = telegram_api._handle_manager_callback

    async def callback(*, callback_data: str, chat_id: int, user_id: int, db: Any) -> bool:
        state = await telegram_state_service.get_state(user_id)
        smart = bool(state and state.get("mode") == "smart_lead_onboarding")
        if callback_data == "sync:run":
            await _run(chat_id, user_id)
            return True
        if smart and callback_data in {"sync:prepare", "sync:confirm"}:
            await _confirm(chat_id, user_id, state)
            return True
        if smart and callback_data == "sync:current":
            await _show_current(chat_id, user_id, state)
            return True
        if smart and callback_data == "sync:call":
            await _call(chat_id, dict(state.get("current_preview") or {}))
            return True
        if smart and callback_data == "sync:skip":
            state["skipped"] = list(state.get("skipped") or []) + [list(state.get("queue") or [])[int(state.get("index") or 0)]]
            state["index"] = int(state.get("index") or 0) + 1
            state["current_preview"] = None
            await telegram_state_service.set_state(user_id, state, ttl_seconds=settings.telegram_state_ttl_minutes * 60)
            await _show_current(chat_id, user_id, state)
            return True
        if smart and callback_data == "sync:cancel":
            await telegram_state_service.clear_state(user_id)
            await telegram_service.send_message(chat_id, "Обработка остановлена. Необработанные лиды не изменены.")
            return True
        return await original(callback_data=callback_data, chat_id=chat_id, user_id=user_id, db=db)

    telegram_api._handle_manager_callback = callback
    telegram_api._run_status_sync = _run
