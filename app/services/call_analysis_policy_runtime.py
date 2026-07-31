"""BBS operating policy for CRM-aware completed-call analysis.

The core CRM call workflow remains unchanged. This module installs narrow policy
extensions for the real Kommo funnel, Russian internal notes, reliable company
extraction and removal of completed-call transcripts from short-term AI memory.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from sqlalchemy import delete, select

from app.models.agent_message import AgentMessage
from app.models.agent_session import AgentSession
from app.services import call_crm_agent_service, crm_service, kommo_service

logger = logging.getLogger(__name__)
_INSTALLED = False


_COMPANY_FIELD_NAMES = {
    "company",
    "company name",
    "company_name",
    "firma",
    "nazwa firmy",
    "pełna nazwa firmy",
    "pelna nazwa firmy",
    "nazwa przedsiębiorstwa",
    "nazwa przedsiebiorstwa",
    "компания",
    "название компании",
}
_PRODUCT_FIELD_TOKENS = {
    "produkt",
    "towar",
    "jakiego",
    "potrzebuje",
    "product",
    "товар",
    "продукт",
    "нужен",
}
_CALL_NOTE_MARKERS = (
    "тип контакта: телефонный разговор",
    "подтверждено в разговоре:",
    "анализ разговора",
    "повторный разговор добавлен",
)
_EARLY_STAGE_TOKENS = (
    "недозвон",
    "не дозвонились",
    "no answer",
    "brak kontaktu",
    "первый контакт",
    "pierwszy kontakt",
    "сбор информации",
    "ожидание данных клиента",
    "квалификация",
    "kwalifikacja",
)


def _norm(value: Any) -> str:
    clean = re.sub(r"[_:/()\[\]{}.,;!?-]+", " ", str(value or "").casefold())
    return " ".join(clean.replace("ё", "е").split())


def _is_exact_company_field(name: str) -> bool:
    normalized = _norm(name)
    if any(_norm(token) in normalized for token in _PRODUCT_FIELD_TOKENS):
        return False
    return normalized in {_norm(item) for item in _COMPANY_FIELD_NAMES}


async def _company_name(contact_id: int | None, fields: dict[str, str]) -> str:
    """Return a company only from a dedicated field or linked Kommo company.

    Facebook form labels often contain a phrase such as "jakiego produktu potrzebuje
    Twoja firma". The old substring match treated the answer to that product question
    as the company name. Dedicated company-field names are now matched exactly.
    """
    for name, value in fields.items():
        clean_value = str(value or "").strip()
        if clean_value and _is_exact_company_field(str(name)):
            return clean_value

    if not contact_id:
        return ""
    try:
        contact = await kommo_service._request(
            "GET",
            f"/api/v4/contacts/{int(contact_id)}",
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
            "Could not load linked Kommo company for contact %s: %s",
            contact_id,
            exc,
        )
    return ""


def _previous_call_count(previous_notes: list[str]) -> int:
    normalized_markers = tuple(_norm(marker) for marker in _CALL_NOTE_MARKERS)
    count = 0
    for note in previous_notes:
        normalized = _norm(note)
        if any(marker in normalized for marker in normalized_markers):
            count += 1
    return count


def _call_number(lead_context: Any) -> int:
    return _previous_call_count(list(lead_context.previous_notes or [])) + 1


def _call_type(lead_context: Any) -> str:
    if _call_number(lead_context) == 1:
        return "Первичный телефонный контакт"
    return "Повторный телефонный разговор"


def _is_early_stage(stage: str) -> bool:
    normalized = _norm(stage)
    normalized_tokens = tuple(_norm(token) for token in _EARLY_STAGE_TOKENS)
    return not normalized or any(token in normalized for token in normalized_tokens)


def _has_substantive_call(analysis: Any, call_context: Any) -> bool:
    return bool(
        analysis.confirmed_in_call
        or analysis.new_information
        or analysis.client_commitment
        or analysis.manager_commitment
    ) and len(str(call_context.transcript or "").strip()) >= 20


def _requirements_incomplete(analysis: Any, lead_context: Any) -> bool:
    if analysis.waiting_for == "client":
        return True
    if analysis.unknown:
        return True
    if not str(lead_context.product_from_form or "").strip():
        return True
    commitment = _norm(analysis.client_commitment)
    data_tokens = (
        "прислать",
        "предоставить",
        "список",
        "фото",
        "специфика",
        "характеристик",
        "объем",
        "количеств",
        "цен",
        "тз",
    )
    return any(token in commitment for token in data_tokens)


def _apply_early_stage_policy(analysis: Any, lead_context: Any, call_context: Any) -> None:
    """Enforce the actual BBS funnel independently of model wording."""
    if not _is_early_stage(str(lead_context.current_stage or "")):
        return
    if not _has_substantive_call(analysis, call_context):
        analysis.kommo_update.should_change_stage = False
        analysis.kommo_update.new_stage = None
        analysis.kommo_update.stage_reason = (
            "В разговоре не зафиксирован содержательный контакт с клиентом."
        )
        return

    number = _call_number(lead_context)
    incomplete = _requirements_incomplete(analysis, lead_context)

    if number == 1:
        target = "Первый контакт"
        reason = (
            "Состоялся первый содержательный разговор, но данных для поиска "
            "производителей ещё недостаточно."
        )
    elif incomplete:
        target = "Сбор информации"
        reason = (
            "Это повторный разговор, и для поиска производителей ещё собираются "
            "товары, характеристики, объёмы, цены или другие параметры."
        )
    else:
        requested = str(analysis.kommo_update.new_stage or "")
        if _norm(requested) in {
            _norm("Квалификация"),
            _norm("Квалификация лида"),
        }:
            target = "Квалификация лида"
            reason = (
                "Критические данные для начала поиска производителей собраны; "
                "существенных неизвестных параметров не осталось."
            )
        else:
            return

    if _norm(target) == _norm(lead_context.current_stage):
        analysis.kommo_update.should_change_stage = False
        analysis.kommo_update.new_stage = target
        analysis.kommo_update.stage_reason = (
            "Сделка уже находится на корректной стадии."
        )
    else:
        analysis.kommo_update.should_change_stage = True
        analysis.kommo_update.new_stage = target
        analysis.kommo_update.stage_reason = reason


def _section(lines: list[str], title: str, values: list[str], empty: str) -> None:
    lines.extend(["", title])
    if values:
        lines.extend(f"- {value}" for value in values)
    else:
        lines.append(f"- {empty}")


def _build_kommo_note(analysis: Any, lead_context: Any, call_context: Any) -> str:
    """Build a Russian Kommo note plus source transcript for later call analytics."""
    channel = str(lead_context.preferred_channel or "Телефон").replace("_", " ")
    client_parts = [
        value
        for value in (
            lead_context.contact_name,
            lead_context.company_name,
            lead_context.product_from_form,
        )
        if str(value or "").strip()
    ]
    lines = [
        f"{call_context.call_date.strftime('%d.%m.%Y')} — {channel}",
        "",
        "Тип контакта: телефонный разговор",
        f"Разговор №: {_call_number(lead_context)}",
        f"Категория разговора: {_call_type(lead_context)}",
        f"Результат разговора: {analysis.summary}",
        "",
        "Клиент:",
        ", ".join(client_parts) or lead_context.lead_name,
    ]
    _section(
        lines,
        "Ранее было известно:",
        list(analysis.known_from_crm),
        "Данных в карточке недостаточно",
    )
    _section(
        lines,
        "Подтверждено в разговоре:",
        list(analysis.confirmed_in_call),
        "Новых подтверждённых фактов нет",
    )
    _section(
        lines,
        "Новые данные:",
        list(analysis.new_information),
        "Новых данных не зафиксировано",
    )
    lines.extend(
        [
            "",
            "Ожидаем от клиента:",
            analysis.client_commitment or "Ничего не ожидается",
            "",
            "Что должны сделать мы:",
            analysis.manager_commitment
            or analysis.kommo_update.task_description
            or "Следующее действие не требуется",
            "",
            "Следующий контакт:",
            (
                f"{analysis.kommo_update.task_due_date.strftime('%d.%m.%Y')} — {channel}"
                if analysis.kommo_update.task_due_date
                else "Дата не определена"
            ),
            "",
            f"Приоритет: {analysis.priority.value} — {analysis.priority.reason}",
        ]
    )
    if analysis.contradictions:
        _section(lines, "Противоречия:", list(analysis.contradictions), "Нет")

    transcript = str(call_context.transcript or "").strip()
    if transcript:
        lines.extend(
            [
                "",
                "Исходная расшифровка разговора:",
                transcript[:5000],
            ]
        )
    return "\n".join(lines).strip()[:14500]


def _stage_before(analysis: Any, lead_context: Any) -> str:
    for action in analysis.actions_completed or []:
        if action.action == "stage_updated" and action.old_value:
            return str(action.old_value)
    return str(lead_context.current_stage or "")


def _stage_after(analysis: Any, lead_context: Any) -> str:
    for action in analysis.actions_completed or []:
        if (
            action.action == "stage_updated"
            and action.status == "success"
            and action.new_value
        ):
            return str(action.new_value)
    return str(analysis.kommo_update.new_stage or lead_context.current_stage or "")


async def _forget_completed_call_from_agent_memory(
    db: Any,
    *,
    telegram_user_id: int | None,
    transcript: str | None,
) -> int:
    """Remove only the completed voice transcript from short-term agent memory.

    The durable VoiceNote transcript and AIReport remain in PostgreSQL, and the
    structured Russian note remains in Kommo. Other unrelated agent messages and
    the active lead pointer are preserved.
    """
    if not telegram_user_id or not str(transcript or "").strip():
        return 0
    stored_content = str(transcript)[:50_000]
    result = await db.execute(
        select(AgentMessage.id).where(
            AgentMessage.telegram_user_id == int(telegram_user_id),
            AgentMessage.source == "voice",
            AgentMessage.content == stored_content,
        )
    )
    ids = list(result.scalars().all())
    if ids:
        await db.execute(delete(AgentMessage).where(AgentMessage.id.in_(ids)))

    session_result = await db.execute(
        select(AgentSession).where(
            AgentSession.telegram_user_id == int(telegram_user_id)
        )
    )
    session = session_result.scalar_one_or_none()
    if session:
        if session.last_user_message == str(transcript)[:10_000]:
            session.last_user_message = None
        # A generated memory summary may contain the just-processed call. Clear
        # only the derived summary; regular unrelated messages remain available.
        session.memory_summary = None
    if ids or session:
        await db.commit()
    if ids:
        logger.info(
            "Removed completed call from AI memory: telegram_user_id=%s messages=%s",
            telegram_user_id,
            len(ids),
        )
    return len(ids)


def install_call_analysis_policy_runtime() -> None:
    """Install the focused call-analysis policy exactly once."""
    global _INSTALLED
    if _INSTALLED:
        return

    call_crm_agent_service._company_name = _company_name
    call_crm_agent_service.build_kommo_note = _build_kommo_note
    call_crm_agent_service._STAGE_ALIASES.update(
        {
            "Первый контакт": (
                "первый контакт",
                "pierwszy kontakt",
            ),
            "Сбор информации": (
                "сбор информации",
                "собираем информацию",
                "ожидание данных клиента",
                "oczekiwanie na dane klienta",
            ),
            "Квалификация лида": (
                "квалификация лида",
                "квалификация",
                "kwalifikacja leada",
                "kwalifikacja",
            ),
        }
    )

    original_postprocess = call_crm_agent_service._postprocess_analysis
    original_to_legacy = call_crm_agent_service.to_legacy_analysis
    original_save_ai_report = crm_service.save_ai_report

    def postprocess_with_bbs_policy(
        analysis: Any,
        *,
        lead_context: Any,
        call_context: Any,
        match_method: str,
        confidence: float,
    ) -> Any:
        processed = original_postprocess(
            analysis,
            lead_context=lead_context,
            call_context=call_context,
            match_method=match_method,
            confidence=confidence,
        )
        _apply_early_stage_policy(processed, lead_context, call_context)
        # The stage policy can change the decision after the original note was
        # built, so rebuild it with the final Russian decision.
        processed.kommo_update.note = _build_kommo_note(
            processed, lead_context, call_context
        )
        processed.kommo_update.should_add_note = bool(
            processed.kommo_update.note
        )
        return processed

    def to_legacy_with_call_metadata(
        analysis: Any, lead_context: Any
    ) -> dict[str, Any]:
        payload = original_to_legacy(analysis, lead_context)
        if lead_context is not None:
            payload["call_metadata"] = {
                "call_number": _call_number(lead_context),
                "call_type": _call_type(lead_context),
                "outcome": analysis.summary,
                "stage_before": _stage_before(analysis, lead_context),
                "stage_after": _stage_after(analysis, lead_context),
                "waiting_for": analysis.waiting_for,
                "stored_in": ["postgresql", "kommo"],
                "retained_in_agent_memory": False,
            }
        return payload

    async def save_ai_report_and_forget_call(
        db: Any,
        voice_note: Any,
        analysis: dict[str, Any],
    ):
        report = await original_save_ai_report(db, voice_note, analysis)
        try:
            await _forget_completed_call_from_agent_memory(
                db,
                telegram_user_id=getattr(
                    voice_note, "telegram_user_id", None
                ),
                transcript=getattr(voice_note, "transcript", None),
            )
        except Exception:
            # Memory cleanup must not roll back the durable CRM report.
            logger.exception(
                "Could not remove completed call from AI memory: voice_note_id=%s",
                getattr(voice_note, "id", None),
            )
        return report

    call_crm_agent_service._postprocess_analysis = postprocess_with_bbs_policy
    call_crm_agent_service.to_legacy_analysis = to_legacy_with_call_metadata
    crm_service.save_ai_report = save_ai_report_and_forget_call
    _INSTALLED = True
