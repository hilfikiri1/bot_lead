from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from app.services import call_analysis_policy_runtime as policy
from app.services.call_analysis_models import (
    CRMCallAnalysis,
    CallContext,
    CallIdentity,
    ClientMessageDraft,
    KommoUpdateDecision,
    LeadContext,
    PriorityDecision,
)


def _analysis(
    *,
    unknown: list[str] | None = None,
    waiting_for: str = "client",
    client_commitment: str = "Прислать список товаров и характеристики.",
    requested_stage: str = "Квалификация",
) -> CRMCallAnalysis:
    return CRMCallAnalysis(
        identity=CallIdentity(
            lead_id="1440927531405186",
            contact_name="Marcin Bojdo",
            company_name="BOFERM",
            phone="+48519392197",
            email="Marcin.bojdo@boferm.pl",
            identity_confidence=1,
            match_method="lead_id",
        ),
        summary=(
            "Клиент заинтересован в прямых закупках и должен прислать данные "
            "по приоритетным товарам."
        ),
        known_from_crm=[
            "Товар из заявки: Produkty rolne",
            "Бюджет из заявки: более 20 000 USD",
        ],
        confirmed_in_call=[
            "Клиент закупает товары из Китая через посредников.",
            "Клиент согласился продолжить разговор в понедельник.",
        ],
        new_information=[
            "Некоторые закупки клиента достигают двух или трёх контейнеров."
        ],
        inferences=[
            "Следует объяснить разницу между поиском контакта и проверкой фабрики."
        ],
        unknown=unknown if unknown is not None else ["Конкретные товары и объёмы"],
        contradictions=[],
        client_goal="Повысить конкурентоспособность за счёт прямых закупок.",
        client_commitment=client_commitment,
        manager_commitment="Проверить данные клиента и подготовиться к разговору.",
        waiting_for=waiting_for,
        priority=PriorityDecision(
            value="A2",
            reason="Есть импортный опыт, контейнерные объёмы и бюджет более 20 000 USD.",
        ),
        kommo_update=KommoUpdateDecision(
            should_add_note=True,
            note="Комментарий будет сформирован программно.",
            should_change_stage=True,
            new_stage=requested_stage,
            stage_reason="Модель предложила следующую стадию.",
            should_create_task=True,
            task_title="Проверить материалы от Marcin Bojdo",
            task_description=(
                "Проверить список товаров, фотографии, характеристики, объёмы и цены. "
                "Если данных нет, отправить напоминание и согласовать время разговора."
            ),
            task_due_date=date(2026, 8, 3),
        ),
        client_message=ClientMessageDraft(
            language="pl",
            channel="WhatsApp",
            text="Dzień dobry Panie Marcinie, proszę przesłać listę produktów.",
            send_automatically=False,
        ),
        needs_review=False,
        review_reason="",
    )


def _context(
    *,
    stage: str = "НЕДОЗВОН",
    previous_notes: list[str] | None = None,
) -> LeadContext:
    return LeadContext(
        lead_id="1440927531405186",
        kommo_deal_id=987654,
        lead_name="169 - Produkty rolne",
        contact_name="Marcin Bojdo",
        company_name="BOFERM",
        phone="+48519392197",
        email="Marcin.bojdo@boferm.pl",
        region="Mazowieckie",
        product_from_form="Produkty rolne",
        budget_from_form="powyżej 20 000 USD",
        preferred_channel="WhatsApp",
        current_stage=stage,
        previous_notes=previous_notes or [],
    )


def _call_context() -> CallContext:
    return CallContext(
        call_date=date(2026, 7, 31),
        call_time="10:00",
        manager_name="Kirill",
        transcript=(
            "Klient kupuje towary z Chin przez pośredników. W poniedziałek prześle "
            "listę produktów, zdjęcia, ilości i obecne ceny."
        ),
    )


@pytest.mark.asyncio
async def test_product_form_question_is_not_used_as_company_name():
    assert (
        await policy._company_name(
            None,
            {"Jakiego produktu potrzebuje Twoja firma?": "Produkty rolne"},
        )
        == ""
    )
    assert (
        await policy._company_name(
            None,
            {
                "Jakiego produktu potrzebuje Twoja firma?": "Produkty rolne",
                "Nazwa firmy": "BOFERM",
            },
        )
        == "BOFERM"
    )


def test_first_successful_call_moves_no_answer_to_first_contact():
    analysis = _analysis()
    context = _context(stage="НЕДОЗВОН")

    policy._apply_early_stage_policy(analysis, context, _call_context())

    assert analysis.kommo_update.should_change_stage is True
    assert analysis.kommo_update.new_stage == "Первый контакт"
    assert "первый содержательный разговор" in analysis.kommo_update.stage_reason


def test_second_incomplete_call_moves_to_information_collection():
    analysis = _analysis()
    context = _context(
        stage="Первый контакт",
        previous_notes=[
            "31.07.2026 — WhatsApp\n\nПодтверждено в разговоре:\n- Первый звонок"
        ],
    )

    policy._apply_early_stage_policy(analysis, context, _call_context())

    assert analysis.kommo_update.new_stage == "Сбор информации"
    assert analysis.kommo_update.should_change_stage is True


def test_qualification_is_allowed_only_when_critical_unknowns_are_closed():
    incomplete = _analysis(
        unknown=["Неизвестны точные объёмы и спецификации"],
        waiting_for="client",
    )
    context = _context(
        stage="Сбор информации",
        previous_notes=["Тип контакта: телефонный разговор"],
    )
    policy._apply_early_stage_policy(incomplete, context, _call_context())
    assert incomplete.kommo_update.new_stage == "Сбор информации"

    complete = _analysis(
        unknown=[],
        waiting_for="manager",
        client_commitment="",
        requested_stage="Квалификация",
    )
    policy._apply_early_stage_policy(complete, context, _call_context())
    assert complete.kommo_update.new_stage == "Квалификация лида"


def test_kommo_note_is_russian_and_keeps_source_transcript():
    analysis = _analysis()
    context = _context(stage="НЕДОЗВОН")
    call_context = _call_context()
    policy._apply_early_stage_policy(analysis, context, call_context)

    note = policy._build_kommo_note(analysis, context, call_context)

    assert "Тип контакта: телефонный разговор" in note
    assert "Категория разговора: Первичный телефонный контакт" in note
    assert "Результат разговора:" in note
    assert "Подтверждено в разговоре:" in note
    assert "Исходная расшифровка разговора:" in note
    assert call_context.transcript in note
    assert "Компания: Produkty rolne" not in note


def test_polish_internal_narrative_is_rejected_but_polish_client_message_is_allowed():
    payload = _analysis().model_dump()
    payload["summary"] = (
        "Rozmowa z klientem dotycząca importu towarów z Chin i dalszych kroków."
    )
    with pytest.raises(ValidationError):
        CRMCallAnalysis.model_validate(payload)

    valid = _analysis()
    assert valid.client_message.text.startswith("Dzień dobry")


class _ScalarCollection:
    def __init__(self, values):
        self._values = values

    def all(self):
        return self._values


class _Result:
    def __init__(self, *, values=None, scalar=None):
        self._values = values or []
        self._scalar = scalar

    def scalars(self):
        return _ScalarCollection(self._values)

    def scalar_one_or_none(self):
        return self._scalar


@pytest.mark.asyncio
async def test_completed_call_is_removed_from_ai_memory_but_active_lead_is_kept():
    transcript = "Klient prześle listę produktów w poniedziałek."
    session = SimpleNamespace(
        last_user_message=transcript,
        memory_summary="Podsumowanie zawierające rozmowę",
        active_kommo_lead_id=987654,
    )
    db = SimpleNamespace(
        execute=AsyncMock(
            side_effect=[
                _Result(values=[101]),
                _Result(),
                _Result(scalar=session),
            ]
        ),
        commit=AsyncMock(),
    )

    deleted = await policy._forget_completed_call_from_agent_memory(
        db,
        telegram_user_id=44,
        transcript=transcript,
    )

    assert deleted == 1
    assert session.last_user_message is None
    assert session.memory_summary is None
    assert session.active_kommo_lead_id == 987654
    db.commit.assert_awaited_once()
