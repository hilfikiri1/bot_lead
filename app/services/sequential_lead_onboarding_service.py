"""Sequential, manager-reviewed onboarding for new Facebook advertising leads.

The source of truth is a concrete Kommo lead from the unsorted inbox.  A sheet row
is used only after one reliable identity match.  Writes are deliberately ordered:
Google Sheets Y -> First contact -> note/task -> final Kommo title.
"""
from __future__ import annotations

import asyncio
import hashlib
import html
import json
import logging
import os
import re
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

from openai import AsyncOpenAI

from app.config import get_settings
from app.services import (
    google_sheets_service,
    kommo_service,
    lead_status_sync_service,
    product_title_service,
)
from app.services.google_sheets_service import SpreadsheetRow
from app.services.lead_matching_service import match_lead_to_rows
from app.services.unreviewed_leads_service import build_proposed_name

logger = logging.getLogger(__name__)
settings = get_settings()

_FACEBOOK_TITLE_RE = re.compile(r"^\s*facebook\s*[#№]?\s*\d+", re.I)
_GENERIC_PRODUCTS = {
    "narzedzia",
    "narzędzia",
    "tools",
    "tool",
    "инструменты",
    "товар",
    "produkty",
    "product",
}
_PRODUCT_TRANSLATIONS = {
    "narzedzia": "Инструменты",
    "narzędzia": "Инструменты",
    "tools": "Инструменты",
    "fotele autobusowe": "Автобусные сиденья",
    "pokrycia podlogowe": "Напольные покрытия",
    "pokrycia podłogowe": "Напольные покрытия",
    "karmniki i poidla": "Кормушки и поилки",
    "karmniki i poidła": "Кормушки и поилки",
}


def _clean(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()


def _fold(value: Any) -> str:
    return _clean(value).casefold().replace("ł", "l").replace("ę", "e").replace("ą", "a")


def _row_fingerprint(row: SpreadsheetRow) -> list[str]:
    return [
        _clean(row.phone).casefold(),
        _clean(row.email).casefold(),
        _clean(row.client_name).casefold(),
        _clean(row.product).casefold(),
    ]


def is_facebook_lead_name(value: Any) -> bool:
    return bool(_FACEBOOK_TITLE_RE.match(_clean(value)))


def _field_value(details: dict[str, Any], tokens: tuple[str, ...]) -> str:
    for field in details.get("custom_fields") or []:
        name = _fold(field.get("name"))
        code = _fold(field.get("code"))
        if any(token in name or token == code for token in tokens):
            value = _clean(field.get("value"))
            if value:
                return value
    return ""


def extract_lead_identity(details: dict[str, Any]) -> dict[str, Any]:
    """Read identity from contacts and from Facebook form fields on the lead."""
    contacts = list(details.get("contacts") or [])
    primary = contacts[0] if contacts else {}
    phones = [_clean(value) for value in primary.get("phones") or [] if _clean(value)]
    emails = [_clean(value) for value in primary.get("emails") or [] if _clean(value)]

    for field in details.get("custom_fields") or []:
        name = _fold(field.get("name"))
        code = _fold(field.get("code"))
        value = _clean(field.get("value"))
        if not value:
            continue
        if "@" in value and value.casefold() not in {item.casefold() for item in emails}:
            emails.append(value)
        phone_field = code == "phone" or any(
            token in name
            for token in (
                "phone",
                "telefon",
                "numer telefonu",
                "numer kontaktowy",
                "swój numer",
                "swoj numer",
                "телефон",
            )
        )
        if phone_field and any(char.isdigit() for char in value):
            for candidate in re.split(r"[,;/]", value):
                candidate = _clean(candidate)
                if candidate and candidate.casefold() not in {
                    item.casefold() for item in phones
                }:
                    phones.append(candidate)

    product = _field_value(
        details,
        (
            "jakiego produktu",
            "jakiego towaru",
            "jaki produkt",
            "product",
            "towar",
            "товар",
        ),
    )
    budget = _field_value(details, ("wartosc zam", "wartość zam", "budget", "бюджет"))
    channel = _field_value(
        details,
        ("w jaki sposob", "w jaki sposób", "kanal", "kanał", "channel", "канал"),
    )
    region = _field_value(details, ("region", "miasto", "город", "область"))
    contact_name = _clean(primary.get("name")) or _field_value(
        details, ("contact name", "imie", "imię", "клиент", "имя")
    )
    return {
        "phones": phones,
        "emails": emails,
        "contact_name": contact_name,
        "company": _field_value(details, ("company", "firma", "компания")),
        "product": product,
        "budget": budget,
        "channel": channel,
        "region": region,
    }


async def _short_product_ru(value: str) -> str:
    folded = _fold(value)
    for source, target in _PRODUCT_TRANSLATIONS.items():
        if folded == _fold(source):
            return target
    try:
        title = _clean(await product_title_service.short_product_title(value))
        if title and title.casefold() not in {"товар", "новый товар", "новый запрос"}:
            return title[:50]
    except Exception as exc:
        logger.warning("Could not translate onboarding product %r: %s", value[:80], exc)
    return (_clean(value).capitalize() or "Новый запрос")[:50]


def _is_polish(item: dict[str, Any]) -> bool:
    phone = _clean(item.get("phone"))
    region = _fold(item.get("region"))
    return phone.startswith("+48") or phone.startswith("48") or bool(region)


def _fallback_analysis(item: dict[str, Any]) -> dict[str, Any]:
    product_raw = _clean(item.get("product_original"))
    product_ru = _clean(item.get("product_ru")) or "товар"
    generic = _fold(product_raw) in {_fold(value) for value in _GENERIC_PRODUCTS}
    budget = _clean(item.get("budget")) or "не указан"
    channel = _fold(item.get("channel"))
    phone = _clean(item.get("phone"))
    client = _clean(item.get("client_name")) or "клиент"
    method = "whatsapp" if "whats" in channel and phone else ("call" if phone else "email")
    priority = "C" if generic else "B"
    potential = "средний" if generic else "средний/высокий после проверки объёма"
    readiness = "низкая — требуется квалификация" if generic else "средняя"
    missing = [
        "конкретный перечень, фотографии или ссылки",
        "количество по каждой позиции",
        "назначение закупки и информация о компании",
        "город доставки и срок закупки",
    ]
    if generic:
        analysis = (
            f"Клиент указал только общую категорию «{product_raw}», без конкретного товара, "
            f"количества и назначения. Бюджет {budget} может подходить для оптовой или "
            "тестовой закупки, но проект пока нельзя оценить по маржинальности и реальности."
        )
    else:
        analysis = (
            f"Есть первичный запрос на {product_ru.lower()} и бюджет {budget}. "
            "Для оценки фабрик, цены и логистики нужно подтвердить спецификацию, объём и сроки."
        )
    if method == "whatsapp":
        greeting = f"Dzień dobry Panie {client.split()[0]}," if _is_polish(item) else f"Добрый день, {client}!"
        if _is_polish(item):
            message = (
                f"{greeting}\n\nz tej strony {os.getenv('BBS_ONBOARDING_SENDER_NAME', 'Kirill')} z firmy Buy & Bring Solutions. "
                f"Otrzymaliśmy Pana zapytanie dotyczące zakupu {product_raw or product_ru.lower()} "
                f"z budżetem {budget}. Pomagamy w zakupach bezpośrednio od producentów w Chinach: "
                "wyszukiwaniu i weryfikacji fabryk, negocjacjach, kontroli jakości, logistyce oraz dokumentacji importowej.\n\n"
                "Żeby dobrze ocenić projekt, proszę o informację: jaki dokładnie rodzaj produktów Pana interesuje, "
                "czy posiada Pan listę, zdjęcia lub linki, jakie ilości są planowane oraz do jakiego miasta ma być dostarczony towar?"
            )
        else:
            message = (
                f"{greeting}\n\nМы получили запрос на {product_ru.lower()} с бюджетом {budget}. "
                "Пришлите, пожалуйста, перечень, фотографии или ссылки, планируемое количество, "
                "назначение закупки и город доставки. После этого оценим производителей и следующий этап."
            )
    else:
        message = ""
    return {
        "personal_analysis": analysis,
        "potential": potential,
        "readiness": readiness,
        "priority": priority,
        "priority_reason": "Нужна квалификация до поиска фабрик и расчёта экономики.",
        "risks": [
            "слишком широкая категория товара" if generic else "неполная спецификация",
            "неизвестны количество, сроки и назначение закупки",
        ],
        "missing_data": missing,
        "contact_method": method,
        "contact_reason": (
            "Сначала лучше получить конкретику письменно; звонок без перечня пока неэффективен."
            if method == "whatsapp"
            else "По телефону быстрее уточнить назначение, объём и сроки."
        ),
        "recommended_action": (
            "Отправить квалификационное сообщение в WhatsApp."
            if method == "whatsapp"
            else "Позвонить клиенту сегодня и пройти короткую квалификацию."
        ),
        "client_message": message,
        "call_script": (
            f"Представиться от Buy & Bring Solutions. Уточнить, какие именно {product_ru.lower()} нужны, "
            "для какого бизнеса, количество, ориентир по сроку, город доставки и нужны ли OEM/private label."
            if method == "call"
            else ""
        ),
        "task_text": (
            f"Получить от {client} перечень, количество, назначение закупки и город доставки по лиду №{item.get('lead_number')}"
        ),
        "followup_plan": "Проверить ответ на следующий рабочий день; при отсутствии ответа через 3 дня отправить короткий follow-up.",
    }


async def _generate_analysis(item: dict[str, Any]) -> dict[str, Any]:
    fallback = _fallback_analysis(item)
    if not settings.openai_api_key.strip():
        return fallback
    client = AsyncOpenAI(api_key=settings.openai_api_key)
    try:
        response = await client.chat.completions.create(
            model=settings.agent_writer_model or settings.openai_model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are the senior B2B lead qualification manager of Buy & Bring Solutions. "
                        "Analyze only confirmed form facts. Decide whether WhatsApp, phone or email is the best first contact. "
                        "For a broad category and a declared WhatsApp preference, normally request specifics in writing before calling. "
                        "Use priorities A/B/C/D; A is urgent high-value qualified, B promising, C qualification required, D weak/unusable. "
                        "Write analysis in Russian and the client message in the client's language. Never invent company facts, quantities, "
                        "deadlines, product models or purchase readiness. Return JSON only."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "lead": {
                                "client_name": item.get("client_name"),
                                "phone": item.get("phone"),
                                "email": item.get("email"),
                                "product_original": item.get("product_original"),
                                "product_ru": item.get("product_ru"),
                                "budget": item.get("budget"),
                                "preferred_channel": item.get("channel"),
                                "region": item.get("region"),
                            },
                            "required_output": {
                                "personal_analysis": "string",
                                "potential": "string",
                                "readiness": "string",
                                "priority": "A|B|C|D",
                                "priority_reason": "string",
                                "risks": ["string"],
                                "missing_data": ["string"],
                                "contact_method": "whatsapp|call|email|manual",
                                "contact_reason": "string",
                                "recommended_action": "string",
                                "client_message": "string",
                                "call_script": "string",
                                "task_text": "string",
                                "followup_plan": "string",
                            },
                        },
                        ensure_ascii=False,
                        default=str,
                    ),
                },
            ],
            response_format={"type": "json_object"},
            temperature=0.12,
        )
        data = json.loads(response.choices[0].message.content or "{}")
    except Exception as exc:
        logger.warning("AI onboarding analysis failed, using fallback: %s", exc)
        return fallback

    result = dict(fallback)
    for key in (
        "personal_analysis",
        "potential",
        "readiness",
        "priority",
        "priority_reason",
        "contact_method",
        "contact_reason",
        "recommended_action",
        "client_message",
        "call_script",
        "task_text",
        "followup_plan",
    ):
        value = _clean(data.get(key))
        if value:
            result[key] = value
    for key in ("risks", "missing_data"):
        values = [_clean(value) for value in data.get(key) or [] if _clean(value)]
        if values:
            result[key] = values[:10]
    if result.get("contact_method") not in {"whatsapp", "call", "email", "manual"}:
        result["contact_method"] = fallback["contact_method"]
    if result.get("priority") not in {"A", "B", "C", "D"}:
        result["priority"] = fallback["priority"]
    return result


def _task_due_timestamp(method: str) -> int:
    try:
        tz = ZoneInfo(settings.manager_timezone or "Europe/Warsaw")
    except Exception:
        tz = ZoneInfo("Europe/Warsaw")
    now = datetime.now(tz)
    if method == "call":
        due = now + timedelta(hours=2)
        if due.hour >= 18:
            due = (now + timedelta(days=1)).replace(hour=10, minute=0, second=0, microsecond=0)
    else:
        due = (now + timedelta(days=1)).replace(hour=10, minute=0, second=0, microsecond=0)
    while due.weekday() >= 5:
        due = (due + timedelta(days=1)).replace(hour=10, minute=0, second=0, microsecond=0)
    return int(due.timestamp())


async def build_onboarding_queue(max_items: int = 20) -> dict[str, Any]:
    rows = await asyncio.to_thread(google_sheets_service.get_rows, force_refresh=True)
    new_rows = [row for row in rows if _clean(row.product) and not _clean(row.lead_number)]
    unsorted = await kommo_service.get_all_unsorted_leads(
        pipeline_id=settings.lead_status_sync_pipeline_id or None
    )
    facebook = [lead for lead in unsorted.get("leads") or [] if is_facebook_lead_name(lead.get("name"))]

    details_list: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for lead in facebook[: max(1, min(max_items * 3, 60))]:
        try:
            details = await kommo_service.get_lead_details(int(lead["id"]))
            details_list.append((lead, details))
        except Exception as exc:
            logger.warning("Could not load unsorted Facebook lead %s: %s", lead.get("id"), exc)

    used_rows: set[int] = set()
    matched: list[dict[str, Any]] = []
    unmatched: list[dict[str, Any]] = []
    for lead, details in details_list:
        identity = extract_lead_identity(details)
        result = match_lead_to_rows(
            phones=identity.get("phones"),
            emails=identity.get("emails"),
            contact_name=identity.get("contact_name"),
            company=identity.get("company"),
            product_hint=identity.get("product"),
            rows=new_rows,
            require_lead_number=False,
        )
        candidate = result.single
        if candidate is None:
            unmatched.append(
                {
                    "kommo_lead_id": lead.get("id"),
                    "kommo_name": lead.get("name"),
                    "client_name": identity.get("contact_name"),
                    "phone": (identity.get("phones") or [None])[0],
                    "reason": "ambiguous" if len(result.candidates) > 1 else "no_exact_identity_match",
                    "candidate_rows": [item.row.row_number for item in result.candidates],
                }
            )
            continue
        row = candidate.row
        if row.row_number in used_rows:
            unmatched.append(
                {
                    "kommo_lead_id": lead.get("id"),
                    "kommo_name": lead.get("name"),
                    "client_name": identity.get("contact_name"),
                    "phone": (identity.get("phones") or [None])[0],
                    "reason": "sheet_row_already_used",
                    "candidate_rows": [row.row_number],
                }
            )
            continue
        used_rows.add(row.row_number)
        product_ru = await _short_product_ru(_clean(row.product))
        pipeline_id = details.get("pipeline_id")
        target_status_id = await lead_status_sync_service._first_contact_status_id(
            pipeline_id if isinstance(pipeline_id, int) else None
        )
        item = {
            "kommo_lead_id": int(lead["id"]),
            "unsorted_uid": lead.get("unsorted_uid"),
            "kommo_old_name": _clean(details.get("name") or lead.get("name")),
            "kommo_url": details.get("url") or lead.get("url"),
            "pipeline_id": pipeline_id,
            "current_status_id": details.get("status_id"),
            "target_status_id": target_status_id,
            "row_number": row.row_number,
            "lead_number": str(row.row_number),
            "row_fingerprint": _row_fingerprint(row),
            "old_comment": _clean(row.marketing_comment),
            "client_name": _clean(row.client_name) or _clean(identity.get("contact_name")),
            "phone": _clean(row.phone) or _clean((identity.get("phones") or [""])[0]),
            "email": _clean(row.email) or _clean((identity.get("emails") or [""])[0]),
            "product_original": _clean(row.product) or _clean(identity.get("product")),
            "product_ru": product_ru,
            "proposed_name": build_proposed_name(str(row.row_number), product_ru),
            "budget": _clean(row.budget) or _clean(identity.get("budget")),
            "channel": _clean(row.contact_channel) or _clean(identity.get("channel")),
            "region": _clean(row.region) or _clean(identity.get("region")),
            "matched_by": candidate.matched_by,
        }
        item["analysis"] = await _generate_analysis(item)
        item["task_due_at"] = _task_due_timestamp(item["analysis"].get("contact_method", "manual"))
        matched.append(item)
        if len(matched) >= max_items:
            break

    digest_payload = [
        (item["kommo_lead_id"], item["row_number"], item["proposed_name"])
        for item in matched
    ]
    digest = hashlib.sha256(
        json.dumps(digest_payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:20]
    unmatched_rows = [
        {
            "row_number": row.row_number,
            "client_name": row.client_name,
            "product": row.product,
        }
        for row in new_rows
        if row.row_number not in used_rows
    ]
    return {
        "generated_at": datetime.utcnow().isoformat(),
        "digest": digest,
        "pipeline_name": unsorted.get("pipeline_name"),
        "new_rows_count": len(new_rows),
        "facebook_unsorted_count": len(facebook),
        "matched_count": len(matched),
        "items": matched,
        "unmatched_leads": unmatched,
        "unmatched_rows": unmatched_rows,
    }


async def refresh_item_analysis(item: dict[str, Any]) -> dict[str, Any]:
    updated = dict(item)
    updated["analysis"] = await _generate_analysis(updated)
    updated["task_due_at"] = _task_due_timestamp(
        updated["analysis"].get("contact_method", "manual")
    )
    return updated


def format_queue_summary(report: dict[str, Any]) -> str:
    lines = [
        "🆕 <b>НОВЫЕ FACEBOOK-ЛИДЫ</b>",
        "",
        f"Воронка: <b>{html.escape(_clean(report.get('pipeline_name')) or '—')}</b>",
        f"Facebook-лидов в «Неразобранном»: <b>{int(report.get('facebook_unsorted_count') or 0)}</b>",
        f"Строк Google Sheets без Y: <b>{int(report.get('new_rows_count') or 0)}</b>",
        f"Надёжно сопоставлено: <b>{int(report.get('matched_count') or 0)}</b>",
        "",
        "🔒 <b>Пока ничего не изменено.</b>",
        "Каждый лид будет показан отдельно: анализ → сообщение/звонок → подтверждение → следующий лид.",
    ]
    unmatched = list(report.get("unmatched_leads") or [])
    if unmatched:
        lines.extend(["", f"⚠️ Не сопоставлено автоматически: <b>{len(unmatched)}</b>"])
        for item in unmatched[:5]:
            reason = {
                "ambiguous": "несколько строк",
                "no_exact_identity_match": "нет точного телефона/email/имени",
                "sheet_row_already_used": "строка уже сопоставлена",
            }.get(item.get("reason"), _clean(item.get("reason")))
            lines.append(
                f"• {html.escape(_clean(item.get('kommo_name')) or 'Facebook-лид')} — {html.escape(reason)}"
            )
    return "\n".join(lines)[:4000]


def _bullets(values: list[Any], limit: int = 6) -> str:
    clean = [_clean(value) for value in values if _clean(value)][:limit]
    return "\n".join(f"• {html.escape(value)}" for value in clean) or "• —"


def format_item_card(item: dict[str, Any], index: int, total: int) -> str:
    analysis = dict(item.get("analysis") or {})
    method = analysis.get("contact_method")
    method_label = {
        "whatsapp": "WhatsApp",
        "call": "звонок",
        "email": "email",
        "manual": "ручная квалификация",
    }.get(method, _clean(method) or "—")
    lines = [
        f"<b>ЛИД {index + 1} ИЗ {total}</b>",
        f"<b>{html.escape(_clean(item.get('proposed_name')))}</b>",
        "",
        f"Клиент: <b>{html.escape(_clean(item.get('client_name')) or '—')}</b>",
        f"Телефон: <code>{html.escape(_clean(item.get('phone')) or '—')}</code>",
        f"Email: {html.escape(_clean(item.get('email')) or '—')}",
        f"Запрос: {html.escape(_clean(item.get('product_original')))} → <b>{html.escape(_clean(item.get('product_ru')))}</b>",
        f"Бюджет: {html.escape(_clean(item.get('budget')) or '—')}",
        f"Канал: {html.escape(_clean(item.get('channel')) or '—')}",
        "",
        "<b>Личный анализ</b>",
        html.escape(_clean(analysis.get("personal_analysis"))[:850]),
        f"Потенциал: <b>{html.escape(_clean(analysis.get('potential')))}</b>",
        f"Готовность: <b>{html.escape(_clean(analysis.get('readiness')))}</b>",
        f"Приоритет: <b>{html.escape(_clean(analysis.get('priority')))}</b> — {html.escape(_clean(analysis.get('priority_reason')))}",
        "",
        "<b>Что нужно уточнить</b>",
        _bullets(list(analysis.get("missing_data") or [])),
        "",
        f"<b>Рекомендация: {html.escape(method_label)}</b>",
        html.escape(_clean(analysis.get("contact_reason"))[:500]),
    ]
    if method == "call":
        lines.extend(
            ["", "<b>Что говорить</b>", html.escape(_clean(analysis.get("call_script"))[:900])]
        )
    else:
        lines.extend(
            ["", "<b>Готовое сообщение клиенту</b>", html.escape(_clean(analysis.get("client_message"))[:1200])]
        )
    lines.extend(
        [
            "",
            "<b>После подтверждения бот выполнит строго по порядку</b>",
            f"1. Запишет Y = {html.escape(_clean(item.get('lead_number')))} в строку {int(item.get('row_number') or 0)}.",
            "2. Переведёт сделку на «Первый контакт».",
            f"3. Переименует в «{html.escape(_clean(item.get('proposed_name')))}».",
            "4. Добавит полный анализ и одну конкретную задачу.",
        ]
    )
    return "\n".join(lines)[:4000]


def whatsapp_url(item: dict[str, Any]) -> str | None:
    analysis = dict(item.get("analysis") or {})
    if analysis.get("contact_method") != "whatsapp":
        return None
    digits = re.sub(r"\D", "", _clean(item.get("phone")))
    body = _clean(analysis.get("client_message"))
    if not digits or not body:
        return None
    return f"https://wa.me/{digits}?text={quote(body[:1400])}"


def item_markup(item: dict[str, Any]) -> dict[str, Any]:
    rows: list[list[dict[str, Any]]] = []
    url = whatsapp_url(item)
    if url:
        rows.append([{"text": "💬 Открыть WhatsApp с текстом", "url": url}])
    rows.extend(
        [
            [{"text": "✅ Оформить и перейти дальше", "callback_data": "sync:confirm"}],
            [
                {"text": "🔄 Пересчитать анализ", "callback_data": "onboard:refresh"},
                {"text": "⏭ Пропустить", "callback_data": "onboard:skip"},
            ],
            [{"text": "❌ Завершить обработку", "callback_data": "onboard:cancel"}],
        ]
    )
    if item.get("kommo_url"):
        rows.append([{"text": "Открыть исходный Facebook-лид", "url": str(item["kommo_url"])}])
    return {"inline_keyboard": rows}


def _format_kommo_note(item: dict[str, Any]) -> str:
    analysis = dict(item.get("analysis") or {})
    try:
        date = datetime.now(ZoneInfo(settings.manager_timezone or "Europe/Warsaw")).strftime("%d.%m.%Y")
    except Exception:
        date = datetime.utcnow().strftime("%d.%m.%Y")
    marker = f"[BBS-ONBOARD-{item['lead_number']}-{item['kommo_lead_id']}]"
    lines = [
        marker,
        f"{date} — заявка / {_clean(item.get('channel')) or 'реклама'}",
        "",
        f"Клиент: {_clean(item.get('client_name')) or 'не указан'}",
        f"Телефон: {_clean(item.get('phone')) or 'не указан'}",
        f"Email: {_clean(item.get('email')) or 'не указан'}",
        "",
        f"Запрос клиента: {_clean(item.get('product_original')) or 'не указан'}.",
        f"Краткое название: {_clean(item.get('product_ru'))}.",
        f"Заявленный бюджет: {_clean(item.get('budget')) or 'не указан'}.",
        f"Регион: {_clean(item.get('region')) or 'не указан'}.",
        "",
        "ЛИЧНЫЙ АНАЛИЗ",
        _clean(analysis.get("personal_analysis")),
        f"Потенциал: {_clean(analysis.get('potential'))}.",
        f"Готовность: {_clean(analysis.get('readiness'))}.",
        f"Приоритет: {_clean(analysis.get('priority'))} — {_clean(analysis.get('priority_reason'))}.",
        "",
        "ОСНОВНЫЕ РИСКИ",
        *[f"– {_clean(value)}" for value in analysis.get("risks") or []],
        "",
        "ЧТО НУЖНО УТОЧНИТЬ",
        *[f"– {_clean(value)}" for value in analysis.get("missing_data") or []],
        "",
        "ЧТО ДЕЛАТЬ ДАЛЬШЕ",
        _clean(analysis.get("recommended_action")),
        _clean(analysis.get("contact_reason")),
    ]
    if analysis.get("client_message"):
        lines.extend(["", "ГОТОВОЕ СООБЩЕНИЕ КЛИЕНТУ", _clean(analysis.get("client_message"))])
    if analysis.get("call_script"):
        lines.extend(["", "СЦЕНАРИЙ ЗВОНКА", _clean(analysis.get("call_script"))])
    lines.extend(
        [
            "",
            "КОНТРОЛЬ",
            _clean(analysis.get("followup_plan")),
            f"Следующая задача: {_clean(analysis.get('task_text'))}.",
            f"Сопоставление: Google Sheets строка {item['row_number']} ↔ Kommo {item['kommo_lead_id']} ({item.get('matched_by')}).",
            "Колонки W и X не изменялись.",
        ]
    )
    return "\n".join(line for line in lines if line is not None)[:13_500]


async def apply_item(item: dict[str, Any]) -> dict[str, Any]:
    """Apply one item idempotently and keep the requested write order."""
    lead_id = int(item["kommo_lead_id"])
    desired_number = _clean(item.get("lead_number"))
    desired_name = _clean(item.get("proposed_name"))
    steps: list[str] = []

    rows = await asyncio.to_thread(google_sheets_service.get_rows, force_refresh=True)
    row = next((candidate for candidate in rows if candidate.row_number == int(item["row_number"])), None)
    if row is None:
        return {"success": False, "partial": False, "error": "Строка Google Sheets исчезла.", "steps": steps}
    current_y = _clean(row.lead_number)
    if current_y not in {"", desired_number}:
        return {
            "success": False,
            "partial": False,
            "error": f"В Y уже указан другой номер: {current_y}.",
            "steps": steps,
        }

    details = await kommo_service.get_lead_details(lead_id)
    current_name = _clean(details.get("name"))
    current_number = lead_status_sync_service.parse_internal_number(current_name)
    if current_number and current_number != desired_number:
        return {
            "success": False,
            "partial": False,
            "error": f"Сделка уже содержит другой внутренний номер: {current_number}.",
            "steps": steps,
        }
    if current_name != desired_name and not is_facebook_lead_name(current_name) and not current_number:
        return {
            "success": False,
            "partial": False,
            "error": "Название сделки изменилось вручную после предпросмотра.",
            "steps": steps,
        }

    try:
        if not current_y:
            update = {
                "row_number": int(item["row_number"]),
                "row_fingerprint": list(item.get("row_fingerprint") or []),
                "old_lead_number": "",
                "new_lead_number": desired_number,
                "old_comment": _clean(item.get("old_comment")),
                "new_comment": _clean(item.get("old_comment")),
                "marketing_status": None,
                "product": item.get("product_original"),
                "kommo_lead_id": lead_id,
                "matched_by": item.get("matched_by"),
            }
            sheet_result = await asyncio.to_thread(
                google_sheets_service.apply_lead_registry_updates, [update]
            )
            if int(sheet_result.get("updated_count") or 0) != 1:
                return {
                    "success": False,
                    "partial": False,
                    "error": "Номер Y не записан: строка изменилась после предпросмотра.",
                    "steps": steps,
                    "sheet": sheet_result,
                }
        steps.append("google_sheets_y")

        target_status_id = item.get("target_status_id")
        if isinstance(target_status_id, int) and details.get("status_id") != target_status_id:
            await kommo_service.update_kommo_lead(lead_id, status_id=target_status_id)
        steps.append("first_contact")
        await asyncio.sleep(0.5)

        marker = f"[BBS-ONBOARD-{desired_number}-{lead_id}]"
        notes = await kommo_service.get_recent_common_notes(lead_id, limit=50)
        if not any(marker in _clean(note.get("text")) for note in notes):
            await kommo_service.add_common_note(lead_id, _format_kommo_note(item))
        steps.append("analysis_note")

        tasks = await kommo_service.get_open_lead_tasks(lead_id, limit=50)
        task_marker = f"№{desired_number}"
        task_text = _clean((item.get("analysis") or {}).get("task_text")) or "Квалифицировать новый лид"
        if task_marker not in task_text:
            task_text = f"№{desired_number} · {task_text}"
        if not any(task_marker in _clean(task.get("text")) for task in tasks):
            await kommo_service.create_lead_task(
                lead_id=lead_id,
                text=task_text[:1000],
                complete_till=int(item.get("task_due_at") or lead_status_sync_service._task_due_timestamp()),
            )
        steps.append("qualification_task")

        # Rename last: Kommo robots triggered by leaving Incoming leads may rewrite
        # the Facebook title.  Re-read and verify after the final title update.
        for delay in (0.0, 0.8, 1.8):
            if delay:
                await asyncio.sleep(delay)
            current = await kommo_service.get_lead_details(lead_id)
            if _clean(current.get("name")) == desired_name:
                break
            await kommo_service.update_kommo_lead(lead_id, name=desired_name)
        final = await kommo_service.get_lead_details(lead_id)
        if _clean(final.get("name")) != desired_name:
            return {
                "success": False,
                "partial": True,
                "error": "Kommo снова изменил название после записи. Остальные шаги выполнены; название требует повторной проверки.",
                "steps": steps,
            }
        steps.append("final_name")
    except Exception as exc:
        logger.exception("Sequential onboarding failed for lead %s", lead_id)
        return {
            "success": False,
            "partial": bool(steps),
            "error": f"{type(exc).__name__}: {_clean(exc)}",
            "steps": steps,
        }

    return {
        "success": True,
        "partial": False,
        "steps": steps,
        "lead_id": lead_id,
        "lead_name": desired_name,
        "kommo_url": item.get("kommo_url"),
        "whatsapp_url": whatsapp_url(item),
        "task_text": _clean((item.get("analysis") or {}).get("task_text")),
    }
