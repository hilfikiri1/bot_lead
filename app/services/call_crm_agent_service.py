"""CRM-aware completed-call workflow for one concrete Kommo deal.

The legacy new-lead summarizer remains untouched. This service is used only when a
call is attached to an existing Kommo deal or when the deal can be resolved
unambiguously from supplied identifiers.
"""

from __future__ import annotations

import html
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Lead
from app.services import crm_service, kommo_service
from app.services.call_analysis_models import (
    ActionCompleted,
    CRMCallAnalysis,
    CRMCallInput,
    CallContext,
    CallIdentity,
    ClientMessageDraft,
    KommoUpdateDecision,
    LeadContext,
    PriorityDecision,
)
from app.services.crm_call_ai_service import InvalidCRMCallAnalysis, analyse_crm_call

logger = logging.getLogger(__name__)
WARSAW = ZoneInfo("Europe/Warsaw")


@dataclass(slots=True)
class CRMCallProcessingResult:
    lead_context: LeadContext | None
    call_context: CallContext
    analysis: CRMCallAnalysis
    legacy_analysis: dict[str, Any]
    candidates: list[dict[str, Any]] = field(default_factory=list)


_SYSTEM_PHRASES = (
    r"пользователь сейчас разговаривает(?: по другой линии)?",
    r"абонент сейчас разговаривает",
    r"оставьте сообщение после сигнала",
    r"просьба оставить сообщение после сигнала",
    r"abonent (?:jest zajęty|prowadzi rozmowę)",
    r"osoba, do której dzwonisz, (?:jest zajęta|prowadzi rozmowę)",
    r"proszę zostawić wiadomość po sygnale",
    r"zostaw wiadomość po sygnale",
    r"the person you are calling is currently unavailable",
    r"please leave a message after the tone",
)

_GENERIC_TASKS = {
    "связаться с клиентом",
    "позвонить",
    "уточнить детали",
    "написать клиенту",
    "follow up",
    "follow-up",
}

_FORBIDDEN_FACT_THEMES: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
    (
        ("обман", "мошенн", "oszuk", "fraud", "deception"),
        ("обман", "мошенн", "oszuk", "fraud", "deception"),
    ),
    (
        (
            "плохое качество",
            "низкое качество",
            "проблемы с качеством",
            "zła jakość",
            "problem z jakością",
            "bad quality",
        ),
        (
            "плохое качество",
            "низкое качество",
            "проблемы с качеством",
            "zła jakość",
            "problem z jakością",
            "bad quality",
        ),
    ),
    (
        (
            "проблемы с поставщиками",
            "проблемы с фабриками",
            "problem z dostawc",
            "supplier problems",
        ),
        (
            "проблемы с поставщиками",
            "проблемы с фабриками",
            "problem z dostawc",
            "supplier problems",
        ),
    ),
)

_STAGE_ALIASES: dict[str, tuple[str, ...]] = {
    "Первый контакт": ("первый контакт", "pierwszy kontakt"),
    "Квалификация": ("квалификация", "kwalifikacja"),
    "Ожидание данных клиента": (
        "ожидание данных клиента",
        "ожидаем данные клиента",
        "oczekiwanie na dane klienta",
        "oczekiwanie na informacje od klienta",
    ),
    "Получено ТЗ": (
        "получено тз",
        "тз получено",
        "otrzymano specyfikację",
        "otrzymano specyfikacje",
    ),
    "Поиск поставщиков": (
        "поиск поставщиков",
        "szukanie dostawców",
        "poszukiwanie dostawców",
    ),
    "Получены предложения фабрик": (
        "получены предложения фабрик",
        "oferty fabryk otrzymane",
        "otrzymano oferty",
    ),
    "Подготовка расчёта": (
        "подготовка расчёта",
        "przygotowanie kalkulacji",
        "kalkulacja",
    ),
    "Предложение отправлено": (
        "предложение отправлено",
        "oferta wysłana",
        "wysłano ofertę",
    ),
    "Ожидание решения": ("ожидание решения", "oczekiwanie na decyzję"),
    "Образцы": ("образцы", "próbki"),
    "PI/договор": ("pi/договор", "pi", "umowa", "proforma"),
    "Ожидание оплаты": (
        "ожидание оплаты",
        "oczekiwanie na płatność",
        "czekamy na płatność",
    ),
}


def normalize_polish_phone(value: str | None) -> str:
    """Normalize Polish telephone numbers to +48XXXXXXXXX."""
    digits = re.sub(r"\D", "", value or "")
    if not digits:
        return ""
    if digits.startswith("0048"):
        digits = digits[4:]
    elif digits.startswith("48") and len(digits) == 11:
        digits = digits[2:]
    if len(digits) == 9:
        return f"+48{digits}"
    if value and str(value).strip().startswith("+"):
        return f"+{digits}"
    return digits


def _norm(value: Any) -> str:
    return " ".join(str(value or "").casefold().replace("ё", "е").split())


def clean_transcript(value: str) -> str:
    """Remove operator prompts, obvious ASR noise and exact repetitions."""
    clean = str(value or "").replace("\x00", " ")
    for pattern in _SYSTEM_PHRASES:
        clean = re.sub(pattern, " ", clean, flags=re.IGNORECASE)
    clean = re.sub(
        r"\[(?:noise|silence|музыка|шум|тишина|niezrozumiałe|unintelligible)\]",
        " ",
        clean,
        flags=re.I,
    )
    clean = re.sub(r"\s+", " ", clean).strip()
    if not clean:
        return ""

    seen: set[str] = set()
    result: list[str] = []
    for part in re.split(r"(?<=[.!?])\s+|\n+", clean):
        sentence = " ".join(part.split()).strip(" -–—")
        if len(sentence) < 2:
            continue
        key = re.sub(r"\W+", "", sentence.casefold())
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(sentence)
    return " ".join(result).strip()


def _custom_field_dict(details: dict[str, Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in details.get("custom_fields") or []:
        name = str(item.get("name") or item.get("code") or "").strip()
        value = str(item.get("value") or "").strip()
        if name and value:
            result[name] = value
    return result


def _pick_field(fields: dict[str, str], *tokens: str) -> str:
    normalized_tokens = tuple(_norm(token) for token in tokens)
    for name, value in fields.items():
        normalized_name = _norm(name)
        if any(token in normalized_name for token in normalized_tokens):
            return value
    return ""


async def _company_name(contact_id: int | None, fields: dict[str, str]) -> str:
    from_fields = _pick_field(
        fields, "company", "firma", "компания", "nazwa firmy"
    )
    if from_fields or not contact_id:
        return from_fields
    try:
        contact = await kommo_service._request(
            "GET",
            f"/api/v4/contacts/{contact_id}",
            params={"with": "companies"},
        ) or {}
        company_refs = ((contact.get("_embedded") or {}).get("companies") or [])
        company_id = company_refs[0].get("id") if company_refs else None
        if isinstance(company_id, int):
            company = await kommo_service._request(
                "GET", f"/api/v4/companies/{company_id}"
            ) or {}
            return str(company.get("name") or "").strip()
    except Exception as exc:
        logger.info(
            "Could not enrich Kommo company for contact %s: %s", contact_id, exc
        )
    return ""


def _lead_name_as_product(name: str) -> str:
    clean = re.sub(r"^\s*\d+\s*[-–—]\s*", "", str(name or "")).strip()
    if _norm(clean) in {"facebook lead", "lead", "без названия", "nowy lead"}:
        return ""
    return clean


async def build_lead_context(details: dict[str, Any]) -> LeadContext:
    """Build the explicitly marked lead_context object from Kommo."""
    contacts = details.get("contacts") or []
    contact = contacts[0] if contacts else {}
    contact_id = contact.get("id") if isinstance(contact.get("id"), int) else None
    phones = contact.get("phones") or []
    emails = contact.get("emails") or []
    lead_fields = _custom_field_dict(details)
    contact_fields = {
        str(item.get("name") or item.get("code") or ""): str(
            item.get("value") or ""
        )
        for item in contact.get("custom_fields") or []
        if item.get("value") not in (None, "")
    }
    all_fields = {**contact_fields, **lead_fields}

    tasks = await kommo_service.get_open_lead_tasks(int(details["id"]), limit=20)
    current_task = ""
    if tasks:
        task = tasks[0]
        current_task = str(task.get("text") or "").strip()
        due = task.get("complete_till")
        if due:
            try:
                due_label = datetime.fromtimestamp(int(due), WARSAW).strftime(
                    "%d.%m.%Y"
                )
            except (TypeError, ValueError, OSError):
                due_label = str(due)
            current_task = f"{current_task} (до {due_label})".strip()

    external_lead_id = _pick_field(
        all_fields,
        "facebook lead id",
        "lead id",
        "id leada",
        "id лида",
        "identyfikator leada",
    )
    budget = _pick_field(
        all_fields,
        "budget",
        "budżet",
        "kwota zamówienia",
        "wartość zamówienia",
        "value of order",
    )
    if not budget and details.get("price") not in (None, 0, "0"):
        budget = str(details.get("price"))

    product = _pick_field(
        all_fields,
        "produkt do zakupu",
        "jakiego produktu",
        "product",
        "товар",
        "продукт",
        "предмет запроса",
    ) or _lead_name_as_product(str(details.get("name") or ""))

    company = await _company_name(contact_id, all_fields)
    return LeadContext(
        lead_id=external_lead_id or str(details.get("id") or ""),
        kommo_deal_id=int(details["id"]),
        lead_name=str(details.get("name") or ""),
        contact_id=str(contact_id or ""),
        contact_name=str(contact.get("name") or ""),
        company_name=company,
        phone=normalize_polish_phone(phones[0] if phones else ""),
        email=str(emails[0] if emails else "").strip(),
        region=_pick_field(
            all_fields, "region", "województwo", "wojewodztwo", "область"
        ),
        product_from_form=product,
        budget_from_form=budget,
        preferred_channel=_pick_field(
            all_fields,
            "kanał kontaktowy",
            "kanal kontaktowy",
            "channel",
            "канал",
        ),
        current_stage=str(details.get("status_name") or ""),
        current_priority=_pick_field(
            all_fields, "priority", "priorytet", "приоритет"
        ),
        current_task=current_task,
        previous_notes=[
            str(item.get("text") or "").strip()
            for item in (details.get("notes") or [])[:10]
            if str(item.get("text") or "").strip()
        ],
        custom_fields={str(key): str(value) for key, value in all_fields.items()},
        pipeline_id=details.get("pipeline_id"),
        status_id=details.get("status_id"),
        responsible_user_id=details.get("responsible_user_id"),
        kommo_url=str(details.get("url") or ""),
    )


def _candidate(details: dict[str, Any]) -> dict[str, Any]:
    contact = (details.get("contacts") or [{}])[0]
    return {
        "kommo_deal_id": details.get("id"),
        "lead_name": details.get("name"),
        "contact_name": contact.get("name"),
        "phone": (contact.get("phones") or [""])[0],
        "email": (contact.get("emails") or [""])[0],
        "stage": details.get("status_name"),
        "url": details.get("url"),
    }


async def _details_for_search(query: str) -> list[dict[str, Any]]:
    result = await kommo_service.search_projects(query, limit=20)
    details: list[dict[str, Any]] = []
    for item in result.get("leads") or []:
        deal_id = item.get("id")
        if not isinstance(deal_id, int):
            continue
        try:
            details.append(await kommo_service.get_lead_details(deal_id))
        except Exception as exc:
            logger.info("Skipping Kommo candidate %s: %s", deal_id, exc)
    return details


def _external_id_matches(details: dict[str, Any], value: str) -> bool:
    target = str(value).strip()
    return any(
        str(field.get("value") or "").strip() == target
        for field in details.get("custom_fields") or []
    )


async def resolve_lead_context(
    *,
    kommo_deal_id: int | None = None,
    lead_id: str | int | None = None,
    phone: str | None = None,
    email: str | None = None,
    name: str | None = None,
    company: str | None = None,
) -> tuple[LeadContext | None, str, float, list[dict[str, Any]]]:
    """Identify a deal: exact lead ID, Kommo ID, phone, email, name/company."""
    if lead_id not in (None, ""):
        lead_ref = str(lead_id).strip()
        external_matches = [
            item
            for item in await _details_for_search(lead_ref)
            if _external_id_matches(item, lead_ref)
        ]
        if len(external_matches) == 1:
            return (
                await build_lead_context(external_matches[0]),
                "lead_id",
                1.0,
                [],
            )
        if len(external_matches) > 1:
            return (
                None,
                "unresolved",
                0.0,
                [_candidate(item) for item in external_matches],
            )
        if lead_ref.isdigit():
            try:
                details = await kommo_service.get_lead_details(int(lead_ref))
                return await build_lead_context(details), "lead_id", 1.0, []
            except kommo_service.KommoAPIError as exc:
                if exc.status_code != 404:
                    raise

    if kommo_deal_id:
        details = await kommo_service.get_lead_details(int(kommo_deal_id))
        return await build_lead_context(details), "kommo_deal_id", 1.0, []

    normalized_phone = normalize_polish_phone(phone)
    if normalized_phone:
        exact: list[dict[str, Any]] = []
        for item in await _details_for_search(phone or normalized_phone):
            values = [
                normalize_polish_phone(number)
                for contact in (item.get("contacts") or [])
                for number in (contact.get("phones") or [])
            ]
            if normalized_phone in values:
                exact.append(item)
        if len(exact) == 1:
            return await build_lead_context(exact[0]), "phone", 0.98, []
        if len(exact) > 1:
            return None, "unresolved", 0.0, [_candidate(item) for item in exact]

    normalized_email = str(email or "").strip().casefold()
    if normalized_email:
        exact = []
        for item in await _details_for_search(normalized_email):
            values = [
                str(address or "").strip().casefold()
                for contact in (item.get("contacts") or [])
                for address in (contact.get("emails") or [])
            ]
            if normalized_email in values:
                exact.append(item)
        if len(exact) == 1:
            return await build_lead_context(exact[0]), "email", 0.98, []
        if len(exact) > 1:
            return None, "unresolved", 0.0, [_candidate(item) for item in exact]

    helper_query = " ".join(value for value in (name, company) if value).strip()
    if helper_query:
        candidates = await _details_for_search(helper_query)
        if len(candidates) == 1:
            return (
                await build_lead_context(candidates[0]),
                "name_company",
                0.85,
                [],
            )
        if len(candidates) > 1:
            return (
                None,
                "unresolved",
                0.0,
                [_candidate(item) for item in candidates],
            )

    return None, "unresolved", 0.0, []


def _known_from_crm(context: LeadContext) -> list[str]:
    pairs = (
        ("Клиент", context.contact_name),
        ("Компания", context.company_name),
        ("Телефон", context.phone),
        ("Email", context.email),
        ("Регион", context.region),
        ("Товар из заявки", context.product_from_form),
        ("Бюджет из заявки", context.budget_from_form),
        ("Предпочтительный канал", context.preferred_channel),
        ("Текущая стадия", context.current_stage),
        ("Текущая задача", context.current_task),
    )
    return [
        f"{label}: {value}" for label, value in pairs if str(value or "").strip()
    ]


def _next_weekday(call_date: date, target_weekday: int) -> date:
    days = (target_weekday - call_date.weekday()) % 7
    return call_date + timedelta(days=days or 7)


def resolve_relative_due_date(transcript: str, call_date: date) -> date | None:
    normalized = _norm(transcript)
    explicit = re.search(
        r"\b(\d{1,2})[./-](\d{1,2})[./-](\d{4})\b", transcript
    )
    if explicit:
        try:
            return date(
                int(explicit.group(3)),
                int(explicit.group(2)),
                int(explicit.group(1)),
            )
        except ValueError:
            pass
    if any(
        token in normalized
        for token in ("pojutrze", "послезавтра", "day after tomorrow")
    ):
        return call_date + timedelta(days=2)
    if any(token in normalized for token in ("jutro", "завтра", "tomorrow")):
        return call_date + timedelta(days=1)
    weekdays = {
        0: ("poniedziałek", "poniedzialek", "понедельник", "monday"),
        1: ("wtorek", "вторник", "tuesday"),
        2: (
            "środę",
            "srode",
            "środa",
            "среду",
            "среда",
            "wednesday",
        ),
        3: ("czwartek", "четверг", "thursday"),
        4: ("piątek", "piatek", "пятницу", "пятница", "friday"),
        5: ("sobotę", "sobote", "sobota", "субботу", "saturday"),
        6: (
            "niedzielę",
            "niedziele",
            "niedziela",
            "воскресенье",
            "sunday",
        ),
    }
    for weekday, tokens in weekdays.items():
        if any(token in normalized for token in tokens):
            return _next_weekday(call_date, weekday)
    return None


def _unsupported_fact(item: str, transcript: str) -> bool:
    normalized_item = _norm(item)
    normalized_transcript = _norm(transcript)
    for output_tokens, source_tokens in _FORBIDDEN_FACT_THEMES:
        if any(token in normalized_item for token in output_tokens) and not any(
            token in normalized_transcript for token in source_tokens
        ):
            return True
    return False


def _task_is_generic(title: str, description: str) -> bool:
    normalized_title = _norm(title).strip(" .")
    normalized_description = _norm(description).strip(" .")
    if normalized_title in _GENERIC_TASKS:
        return True
    return len(f"{normalized_title} {normalized_description}") < 45


def _channel_label(context: LeadContext) -> str:
    return (context.preferred_channel or "телефон").replace("_", " ")


def build_kommo_note(
    analysis: CRMCallAnalysis,
    lead_context: LeadContext,
    call_context: CallContext,
) -> str:
    """Build a deterministic note; model inferences are deliberately excluded."""
    client_label = ", ".join(
        value
        for value in (
            lead_context.contact_name,
            lead_context.company_name,
            lead_context.product_from_form,
        )
        if value
    ) or lead_context.lead_name
    lines = [
        f"{call_context.call_date.strftime('%d.%m.%Y')} — {_channel_label(lead_context)}",
        "",
        "Клиент:",
        client_label,
        "",
        "Ранее было известно:",
    ]
    if analysis.known_from_crm:
        lines.extend(f"- {item}" for item in analysis.known_from_crm)
    else:
        lines.append("- Данных в карточке недостаточно")

    lines.extend(["", "Подтверждено в разговоре:"])
    if analysis.confirmed_in_call:
        lines.extend(f"- {item}" for item in analysis.confirmed_in_call)
    else:
        lines.append("- Новых подтверждений не зафиксировано")

    lines.extend(["", "Новые данные:"])
    if analysis.new_information:
        lines.extend(f"- {item}" for item in analysis.new_information)
    else:
        lines.append("- Новых данных не зафиксировано")

    lines.extend(
        [
            "",
            "Ожидаем от клиента:",
            analysis.client_commitment or "Не определено",
            "",
            "Что должны сделать мы:",
            analysis.manager_commitment
            or analysis.kommo_update.task_description
            or "Не определено",
            "",
            "Следующий контакт:",
            (
                f"{analysis.kommo_update.task_due_date.strftime('%d.%m.%Y')} — "
                f"{_channel_label(lead_context)}"
                if analysis.kommo_update.task_due_date
                else "Дата не определена"
            ),
            "",
            f"Приоритет: {analysis.priority.value} — {analysis.priority.reason}",
        ]
    )
    if analysis.contradictions:
        lines.extend(["", "Противоречия:"])
        lines.extend(f"- {item}" for item in analysis.contradictions)
    return "\n".join(lines).strip()[:14500]


def _append_review_reason(analysis: CRMCallAnalysis, reason: str) -> None:
    analysis.needs_review = True
    analysis.review_reason = (
        f"{analysis.review_reason}; {reason}" if analysis.review_reason else reason
    )


def _postprocess_analysis(
    analysis: CRMCallAnalysis,
    *,
    lead_context: LeadContext,
    call_context: CallContext,
    match_method: str,
    confidence: float,
) -> CRMCallAnalysis:
    analysis.identity = CallIdentity(
        lead_id=lead_context.lead_id,
        contact_name=lead_context.contact_name,
        company_name=lead_context.company_name,
        phone=lead_context.phone,
        email=lead_context.email,
        identity_confidence=confidence,
        match_method=match_method,
    )
    deterministic_known = _known_from_crm(lead_context)
    analysis.known_from_crm = list(
        dict.fromkeys(deterministic_known + analysis.known_from_crm)
    )

    filtered = False
    safe_confirmed: list[str] = []
    safe_new: list[str] = []
    for item in analysis.confirmed_in_call:
        if _unsupported_fact(item, call_context.transcript):
            filtered = True
        else:
            safe_confirmed.append(item)
    for item in analysis.new_information:
        if _unsupported_fact(item, call_context.transcript):
            filtered = True
        else:
            safe_new.append(item)
    analysis.confirmed_in_call = safe_confirmed
    analysis.new_information = safe_new

    due = analysis.kommo_update.task_due_date or resolve_relative_due_date(
        call_context.transcript, call_context.call_date
    )
    analysis.kommo_update.task_due_date = due
    if analysis.kommo_update.should_create_task:
        if due is None:
            _append_review_reason(
                analysis, "Не удалось определить дату следующей задачи"
            )
        elif due < call_context.call_date:
            _append_review_reason(analysis, "Дата следующей задачи находится в прошлом")
        if _task_is_generic(
            analysis.kommo_update.task_title,
            analysis.kommo_update.task_description,
        ):
            _append_review_reason(
                analysis, "Модель предложила неконкретную следующую задачу"
            )

    if filtered:
        _append_review_reason(analysis, "Удалены неподтверждённые утверждения модели")

    if (
        analysis.kommo_update.new_stage
        and _norm(analysis.kommo_update.new_stage)
        == _norm(lead_context.current_stage)
    ):
        analysis.kommo_update.should_change_stage = False
        analysis.kommo_update.stage_reason = "Сделка уже находится на этой стадии"

    analysis.kommo_update.note = build_kommo_note(
        analysis, lead_context, call_context
    )
    analysis.kommo_update.should_add_note = bool(analysis.kommo_update.note)
    analysis.client_message.send_automatically = False
    return analysis


def _safe_review_analysis(
    *,
    lead_context: LeadContext | None,
    reason: str,
    match_method: str = "unresolved",
    confidence: float = 0.0,
) -> CRMCallAnalysis:
    return CRMCallAnalysis(
        identity=CallIdentity(
            lead_id=lead_context.lead_id if lead_context else "",
            contact_name=lead_context.contact_name if lead_context else "",
            company_name=lead_context.company_name if lead_context else "",
            phone=lead_context.phone if lead_context else "",
            email=lead_context.email if lead_context else "",
            identity_confidence=confidence,
            match_method=match_method if lead_context else "unresolved",
        ),
        summary="Анализ не применён автоматически.",
        known_from_crm=_known_from_crm(lead_context) if lead_context else [],
        confirmed_in_call=[],
        new_information=[],
        inferences=[],
        unknown=[],
        contradictions=[],
        client_goal="",
        client_commitment="",
        manager_commitment="",
        waiting_for="other",
        priority=PriorityDecision(
            value="C", reason="Требуется ручная проверка анализа"
        ),
        kommo_update=KommoUpdateDecision(
            should_add_note=False,
            note="",
            should_change_stage=False,
            new_stage=None,
            stage_reason="",
            should_create_task=False,
            task_title="",
            task_description="",
            task_due_date=None,
        ),
        client_message=ClientMessageDraft(
            language="pl",
            channel="WhatsApp",
            text="",
            send_automatically=False,
        ),
        needs_review=True,
        review_reason=reason,
    )


def _resolve_stage_id(
    statuses: list[dict[str, Any]], canonical: str
) -> tuple[int, str] | None:
    aliases = _STAGE_ALIASES.get(canonical, (canonical,))
    normalized_aliases = {_norm(alias) for alias in aliases}
    for status in statuses:
        status_id = status.get("id")
        name = str(status.get("name") or "")
        normalized_name = _norm(name)
        if isinstance(status_id, int) and (
            normalized_name in normalized_aliases
            or any(alias in normalized_name for alias in normalized_aliases)
        ):
            return status_id, name
    return None


def _task_timestamp(due_date: date) -> int:
    due_at = datetime.combine(due_date, time(hour=9), tzinfo=WARSAW)
    now = datetime.now(tz=WARSAW)
    if due_at <= now and due_date == now.date():
        due_at = now + timedelta(hours=1)
    return int(due_at.timestamp())


def _followup_datetime(due_date: date | None) -> datetime | None:
    if due_date is None:
        return None
    return datetime.combine(due_date, time(hour=9), tzinfo=WARSAW)


async def _apply_kommo_actions(
    lead_context: LeadContext,
    analysis: CRMCallAnalysis,
) -> list[ActionCompleted]:
    if analysis.needs_review or not lead_context.kommo_deal_id:
        return []
    completed: list[ActionCompleted] = []
    deal_id = int(lead_context.kommo_deal_id)

    if analysis.kommo_update.should_add_note and analysis.kommo_update.note:
        try:
            created = await kommo_service.add_common_note(
                deal_id, analysis.kommo_update.note
            )
            if created:
                completed.append(
                    ActionCompleted(action="note_created", status="success")
                )
            else:
                completed.append(
                    ActionCompleted(
                        action="note_created",
                        status="failed",
                        error="Kommo did not confirm note creation",
                    )
                )
        except Exception as exc:
            logger.exception("Automatic Kommo note failed for lead %s", deal_id)
            completed.append(
                ActionCompleted(
                    action="note_created", status="failed", error=str(exc)[:500]
                )
            )

    update = analysis.kommo_update
    if update.should_change_stage and update.new_stage:
        try:
            statuses = await kommo_service.get_pipeline_statuses(
                int(lead_context.pipeline_id or 0)
            )
            resolved = _resolve_stage_id(statuses, update.new_stage)
            if not resolved:
                completed.append(
                    ActionCompleted(
                        action="stage_updated",
                        status="failed",
                        old_value=lead_context.current_stage,
                        new_value=update.new_stage,
                        error=(
                            "Requested stage does not exist in the current Kommo "
                            "pipeline"
                        ),
                    )
                )
            else:
                status_id, status_name = resolved
                await kommo_service.update_kommo_lead(deal_id, status_id=status_id)
                verified = await kommo_service.get_lead_details(deal_id)
                if int(verified.get("status_id") or 0) != status_id:
                    raise RuntimeError("Kommo did not confirm the requested stage")
                completed.append(
                    ActionCompleted(
                        action="stage_updated",
                        status="success",
                        old_value=lead_context.current_stage,
                        new_value=status_name,
                    )
                )
                lead_context.current_stage = status_name
                lead_context.status_id = status_id
        except Exception as exc:
            logger.exception(
                "Automatic Kommo stage update failed for lead %s", deal_id
            )
            completed.append(
                ActionCompleted(
                    action="stage_updated",
                    status="failed",
                    old_value=lead_context.current_stage,
                    new_value=update.new_stage,
                    error=str(exc)[:500],
                )
            )

    if update.should_create_task and update.task_due_date:
        task_text = f"{update.task_title}\n{update.task_description}".strip()[:1000]
        action_name: str = "task_created"
        try:
            timestamp = _task_timestamp(update.task_due_date)
            if timestamp <= int(datetime.now(tz=WARSAW).timestamp()):
                raise ValueError("Task due date is not in the future")
            existing_tasks = await kommo_service.get_open_lead_tasks(
                deal_id, limit=20
            )
            if existing_tasks and isinstance(existing_tasks[0].get("id"), int):
                action_name = "task_updated"
                task_id = int(existing_tasks[0]["id"])
                await kommo_service._request(
                    "PATCH",
                    f"/api/v4/tasks/{task_id}",
                    json_body={"text": task_text, "complete_till": timestamp},
                )
                verified_tasks = await kommo_service.get_open_lead_tasks(
                    deal_id, limit=20
                )
                verified = next(
                    (
                        item
                        for item in verified_tasks
                        if int(item.get("id") or 0) == task_id
                    ),
                    None,
                )
                if (
                    not verified
                    or int(verified.get("complete_till") or 0) != timestamp
                ):
                    raise RuntimeError("Kommo did not confirm task replacement")
                completed.append(
                    ActionCompleted(
                        action="task_updated",
                        status="success",
                        due_date=update.task_due_date,
                        old_value=str(existing_tasks[0].get("text") or ""),
                        new_value=task_text,
                    )
                )
            else:
                created = await kommo_service.create_lead_task(
                    lead_id=deal_id,
                    text=task_text,
                    complete_till=timestamp,
                    responsible_user_id=lead_context.responsible_user_id,
                )
                if not created.get("task_id"):
                    raise RuntimeError("Kommo did not return a created task ID")
                completed.append(
                    ActionCompleted(
                        action="task_created",
                        status="success",
                        due_date=update.task_due_date,
                        new_value=task_text,
                    )
                )
        except Exception as exc:
            logger.exception("Automatic Kommo task update failed for lead %s", deal_id)
            completed.append(
                ActionCompleted(
                    action=action_name,
                    status="failed",
                    due_date=update.task_due_date,
                    new_value=task_text,
                    error=str(exc)[:500],
                )
            )

    for item in completed:
        logger.info(
            "CRM call action: lead_id=%s action=%s status=%s",
            deal_id,
            item.action,
            item.status,
        )
    return completed


def to_legacy_analysis(
    analysis: CRMCallAnalysis,
    lead_context: LeadContext | None,
) -> dict[str, Any]:
    """Keep existing Telegram, PostgreSQL and Notion consumers compatible."""
    context = lead_context or LeadContext()
    due_date = analysis.kommo_update.task_due_date
    due_iso = due_date.isoformat() if due_date else None
    priority_to_urgency = {
        "A1": "high",
        "A2": "high",
        "B": "medium",
        "C": "low",
        "D": "low",
    }
    payload = analysis.model_dump(mode="json")
    payload.update(
        {
            "client": {
                "name": analysis.identity.contact_name or None,
                "phone": analysis.identity.phone or None,
                "email": analysis.identity.email or None,
                "company": analysis.identity.company_name or None,
                "language": analysis.client_message.language or "unknown",
                "kommo_contact_id": (
                    int(context.contact_id) if context.contact_id.isdigit() else None
                ),
            },
            "lead": {
                "lead_number": context.lead_id or None,
                "proposed_name": (
                    context.lead_name
                    or context.product_from_form
                    or "Разговор с клиентом"
                ),
                "product_requested": context.product_from_form or "Не указано",
                "specifications": analysis.new_information,
                "quantity": None,
                "budget": context.budget_from_form or None,
                "country": "Poland" if context.phone.startswith("+48") else None,
                "city": context.region or None,
                "delivery_terms": None,
                "certification": None,
                "timeline": due_iso,
                "urgency": priority_to_urgency[analysis.priority.value],
                "status": "follow_up",
                "next_action": analysis.kommo_update.task_description,
                "next_followup_at": due_iso,
            },
            "conversation_summary": analysis.summary,
            "confirmed_facts": analysis.confirmed_in_call,
            "what_manager_said": analysis.confirmed_in_call,
            "mistakes_or_weak_points": analysis.inferences,
            "missing_questions": analysis.unknown,
            "risks": analysis.contradictions,
            "recommended_next_step": (
                analysis.kommo_update.task_description
                or analysis.manager_commitment
            ),
            "manager_task": {
                "title": analysis.kommo_update.task_title or None,
                "due_at": due_iso,
            },
            "email": {"subject": "", "body": ""},
            "whatsapp": {"message": analysis.client_message.text},
            "calendar": {
                "title": (
                    analysis.kommo_update.task_title or "Следующий контакт"
                ),
                "description": analysis.kommo_update.task_description,
                "start_time": due_iso,
                "duration_minutes": 15,
            },
            "confidence_score": analysis.identity.identity_confidence,
            "needs_human_review": analysis.needs_review,
            "crm_call_analysis": analysis.model_dump(mode="json"),
            "actions_completed": [
                item.model_dump(mode="json")
                for item in analysis.actions_completed
            ],
        }
    )
    return payload


async def upsert_local_crm_snapshot(
    db: AsyncSession,
    *,
    lead_context: LeadContext,
    analysis: CRMCallAnalysis,
):
    """Update the existing local mirror instead of creating a duplicate lead."""
    client = await crm_service.upsert_client(
        db,
        {
            "name": lead_context.contact_name,
            "phone": lead_context.phone,
            "email": lead_context.email,
            "company": lead_context.company_name,
            "language": analysis.client_message.language,
            "kommo_contact_id": (
                int(lead_context.contact_id)
                if lead_context.contact_id.isdigit()
                else None
            ),
            "source": "kommo_call_analysis",
        },
    )
    result = await db.execute(
        select(Lead).where(
            Lead.kommo_lead_id == int(lead_context.kommo_deal_id or 0)
        )
    )
    lead = result.scalars().first()
    priority = {
        "A1": "high",
        "A2": "high",
        "B": "medium",
        "C": "low",
        "D": "low",
    }[analysis.priority.value]
    followup_at = _followup_datetime(analysis.kommo_update.task_due_date)

    if lead is None:
        lead = await crm_service.create_lead(
            db,
            client,
            {
                "product_requested": lead_context.product_from_form,
                "budget": lead_context.budget_from_form,
                "country": (
                    "Poland" if lead_context.phone.startswith("+48") else None
                ),
                "city": lead_context.region,
                "status": "follow_up",
                "urgency": (
                    "high"
                    if priority == "high"
                    else "medium"
                    if priority == "medium"
                    else "low"
                ),
                "next_action": analysis.kommo_update.task_description,
                "next_followup_at": followup_at,
            },
        )
    else:
        lead.client_id = client.id
        lead.product_requested = (
            lead_context.product_from_form or lead.product_requested
        )
        lead.budget = lead_context.budget_from_form or lead.budget
        lead.city = lead_context.region or lead.city
        lead.priority = priority
        lead.next_action = (
            analysis.kommo_update.task_description or lead.next_action
        )
        lead.next_followup_at = followup_at
        await db.commit()
        await db.refresh(lead)

    await crm_service.save_kommo_mapping(
        db,
        lead_id=int(lead.id),
        kommo_lead_id=int(lead_context.kommo_deal_id or 0),
        kommo_contact_id=(
            int(lead_context.contact_id)
            if lead_context.contact_id.isdigit()
            else None
        ),
        pipeline_id=lead_context.pipeline_id,
        status_id=lead_context.status_id,
        url=lead_context.kommo_url,
    )
    return client, lead


def format_actions_report(analysis: CRMCallAnalysis) -> str:
    if analysis.needs_review:
        return (
            "\n\n⚠️ <b>Kommo не изменён автоматически</b>\n"
            f"Причина: {html.escape(analysis.review_reason)}"
        )
    if not analysis.actions_completed:
        return "\n\nℹ️ <b>Изменений Kommo не требовалось.</b>"
    labels = {
        "note_created": "Комментарий добавлен",
        "stage_updated": "Стадия обновлена",
        "task_created": "Следующая задача создана",
        "task_updated": "Следующая задача заменена",
    }
    lines = ["", "", "✅ <b>Фактически выполнено в Kommo</b>"]
    for action in analysis.actions_completed:
        label = labels[action.action]
        if action.status == "success":
            suffix = ""
            if action.new_value and action.action == "stage_updated":
                suffix = (
                    f": {html.escape(action.old_value or '—')} → "
                    f"{html.escape(action.new_value)}"
                )
            if action.due_date:
                suffix += f" · {action.due_date.strftime('%d.%m.%Y')}"
            lines.append(f"• {label}{suffix}")
        else:
            error = html.escape(action.error or action.status)
            lines.append(f"• ❌ {label}: {error}")
    lines.append("• Сообщение клиенту только подготовлено и не отправлено")
    return "\n".join(lines)


async def process_completed_call(
    *,
    transcript: str,
    kommo_deal_id: int | None = None,
    lead_id: str | int | None = None,
    phone: str | None = None,
    email: str | None = None,
    name: str | None = None,
    company: str | None = None,
    call_date: date | None = None,
    call_time: str | None = None,
    manager_name: str | None = None,
) -> CRMCallProcessingResult:
    """Resolve, analyse and safely apply a completed telephone call."""
    now = datetime.now(tz=WARSAW)
    cleaned = clean_transcript(transcript)
    call_context = CallContext(
        call_date=call_date or now.date(),
        call_time=call_time or now.strftime("%H:%M"),
        manager_name=(
            str(manager_name or "").strip()
            or os.getenv("CALL_ANALYSIS_MANAGER_NAME", "Kirill").strip()
            or "Manager"
        ),
        transcript=cleaned,
    )
    lead_context, match_method, confidence, candidates = await resolve_lead_context(
        kommo_deal_id=kommo_deal_id,
        lead_id=lead_id,
        phone=phone,
        email=email,
        name=name,
        company=company,
    )
    if lead_context is None:
        reason = (
            "Найдено несколько возможных карточек; требуется выбор менеджера"
            if candidates
            else "Не удалось однозначно определить сделку"
        )
        analysis = _safe_review_analysis(lead_context=None, reason=reason)
        return CRMCallProcessingResult(
            lead_context=None,
            call_context=call_context,
            analysis=analysis,
            legacy_analysis=to_legacy_analysis(analysis, None),
            candidates=candidates,
        )

    if not cleaned:
        analysis = _safe_review_analysis(
            lead_context=lead_context,
            reason="После удаления системных фраз расшифровка пуста",
            match_method=match_method,
            confidence=confidence,
        )
        return CRMCallProcessingResult(
            lead_context=lead_context,
            call_context=call_context,
            analysis=analysis,
            legacy_analysis=to_legacy_analysis(analysis, lead_context),
        )

    payload = CRMCallInput(
        lead_context=lead_context,
        call_context=call_context,
    )
    try:
        analysis = await analyse_crm_call(payload)
    except InvalidCRMCallAnalysis as exc:
        logger.exception(
            "CRM call analysis invalid; no Kommo writes for deal %s",
            lead_context.kommo_deal_id,
        )
        analysis = _safe_review_analysis(
            lead_context=lead_context,
            reason=str(exc),
            match_method=match_method,
            confidence=confidence,
        )
        return CRMCallProcessingResult(
            lead_context=lead_context,
            call_context=call_context,
            analysis=analysis,
            legacy_analysis=to_legacy_analysis(analysis, lead_context),
        )

    analysis = _postprocess_analysis(
        analysis,
        lead_context=lead_context,
        call_context=call_context,
        match_method=match_method,
        confidence=confidence,
    )
    if match_method == "name_company":
        _append_review_reason(
            analysis, "Идентификация основана только на имени/компании"
        )

    analysis.actions_completed = await _apply_kommo_actions(
        lead_context, analysis
    )
    return CRMCallProcessingResult(
        lead_context=lead_context,
        call_context=call_context,
        analysis=analysis,
        legacy_analysis=to_legacy_analysis(analysis, lead_context),
    )
