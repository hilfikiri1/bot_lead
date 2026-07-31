from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock, patch

import pytest

from app.services import crm_call_ai_service
from app.services.call_analysis_models import (
    CRMCallAnalysis,
    CRMCallInput,
    CallContext,
    CallIdentity,
    ClientMessageDraft,
    KommoUpdateDecision,
    LeadContext,
    PriorityDecision,
)


def _input() -> CRMCallInput:
    return CRMCallInput(
        lead_context=LeadContext(
            lead_id="1440927531405186",
            kommo_deal_id=987654,
            lead_name="169 - Produkty rolne",
            contact_name="Marcin Bojdo",
            company_name="BOFERM",
            phone="+48519392197",
            email="Marcin.bojdo@boferm.pl",
            product_from_form="Produkty rolne",
            budget_from_form="powyżej 20 000 USD",
            current_stage="Квалификация",
        ),
        call_context=CallContext(
            call_date=date(2026, 7, 31),
            call_time="10:00",
            manager_name="Kirill",
            transcript="Klient prześle informacje w poniedziałek.",
        ),
    )


def _valid_output() -> str:
    model = CRMCallAnalysis(
        identity=CallIdentity(
            lead_id="1440927531405186",
            contact_name="Marcin Bojdo",
            company_name="BOFERM",
            phone="+48519392197",
            email="Marcin.bojdo@boferm.pl",
            identity_confidence=1,
            match_method="lead_id",
        ),
        summary="Клиент пришлёт данные.",
        known_from_crm=["Товар из заявки: Produkty rolne"],
        confirmed_in_call=["Клиент пришлёт данные в понедельник."],
        new_information=[],
        inferences=[],
        unknown=["Конкретные товары"],
        contradictions=[],
        client_goal="Получить оценку закупки.",
        client_commitment="Прислать список товаров.",
        manager_commitment="Проверить список.",
        waiting_for="client",
        priority=PriorityDecision(value="A2", reason="Бюджет выше 20 000 USD."),
        kommo_update=KommoUpdateDecision(
            should_add_note=True,
            note="Комментарий",
            should_change_stage=True,
            new_stage="Ожидание данных клиента",
            stage_reason="Ожидается список товаров.",
            should_create_task=True,
            task_title="Проверить список товаров Marcin Bojdo",
            task_description="Проверить присланные товары, объёмы и цены и подготовить оценку.",
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
    return model.model_dump_json()


@pytest.mark.asyncio
async def test_invalid_json_gets_exactly_one_repair_attempt():
    with patch(
        "app.services.crm_call_ai_service._completion",
        new_callable=AsyncMock,
        side_effect=["not-json", _valid_output()],
    ) as completion:
        result = await crm_call_ai_service.analyse_crm_call(_input())

    assert result.identity.contact_name == "Marcin Bojdo"
    assert completion.await_count == 2


@pytest.mark.asyncio
async def test_invalid_json_after_repair_raises_and_cannot_be_written():
    with patch(
        "app.services.crm_call_ai_service._completion",
        new_callable=AsyncMock,
        side_effect=["not-json", '{"still":"invalid"}'],
    ) as completion:
        with pytest.raises(crm_call_ai_service.InvalidCRMCallAnalysis):
            await crm_call_ai_service.analyse_crm_call(_input())

    assert completion.await_count == 2
