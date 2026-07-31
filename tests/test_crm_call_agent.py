from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from app.services import call_crm_agent_service
from app.services.call_analysis_models import (
    CRMCallAnalysis,
    CallIdentity,
    ClientMessageDraft,
    KommoUpdateDecision,
    PriorityDecision,
)


def _marcin_details(*, status_id: int = 20, status_name: str = "Квалификация"):
    return {
        "id": 987654,
        "name": "169 - Produkty rolne",
        "price": 0,
        "pipeline_id": 10,
        "pipeline_name": "Polska",
        "status_id": status_id,
        "status_name": status_name,
        "responsible_user_id": 44,
        "custom_fields": [
            {"name": "Facebook Lead ID", "code": "", "value": "1440927531405186"},
            {"name": "Region", "code": "", "value": "Mazowieckie"},
            {"name": "Produkt do zakupu", "code": "", "value": "Produkty rolne"},
            {"name": "Wartość zamówienia", "code": "", "value": "powyżej 20 000 USD"},
            {"name": "Kanał kontaktowy", "code": "", "value": "WhatsApp"},
            {"name": "Firma", "code": "", "value": "BOFERM"},
        ],
        "contacts": [
            {
                "id": 555,
                "name": "Marcin Bojdo",
                "phones": ["+48 519 392 197"],
                "emails": ["Marcin.bojdo@boferm.pl"],
                "custom_fields": [],
            }
        ],
        "notes": [{"text": "Pierwszy kontakt z formularza."}],
        "url": "https://example.kommo.com/leads/detail/987654",
    }


def _marcin_analysis(*, include_hallucination: bool = False) -> CRMCallAnalysis:
    confirmed = [
        "Клиент уже закупает товары из Китая через посредников.",
        "Некоторые закупки составляют 2–3 контейнера.",
        "Клиент хочет повысить конкурентоспособность и рассмотреть более прямую работу с китайскими поставщиками.",
        "Клиент согласился продолжить разговор в понедельник и заранее прислать информацию о товарах.",
    ]
    if include_hallucination:
        confirmed.extend(
            [
                "Посредники обманывали клиента.",
                "У клиента были проблемы с качеством.",
            ]
        )
    return CRMCallAnalysis(
        identity=CallIdentity(
            lead_id="",
            contact_name="",
            company_name="",
            phone="",
            email="",
            identity_confidence=0.2,
            match_method="unresolved",
        ),
        summary="Клиент рассматривает прямые закупки из Китая и должен прислать приоритетные позиции.",
        known_from_crm=[],
        confirmed_in_call=confirmed,
        new_information=["Для отдельных закупок объём достигает 2–3 контейнеров."],
        inferences=[
            "Следует объяснить разницу между поиском контакта фабрики и проверкой поставщика."
        ],
        unknown=[
            "Конкретные товары",
            "Точные объёмы по каждой позиции",
            "Текущие цены",
            "Требования к сертификации",
        ],
        contradictions=[],
        client_goal="Повысить конкурентоспособность за счёт более прямых закупок из Китая.",
        client_commitment="Прислать 3–5 приоритетных товаров, фото или спецификации, объёмы и доступные текущие цены.",
        manager_commitment="Подготовиться к разговору после получения данных клиента.",
        waiting_for="client",
        priority=PriorityDecision(
            value="A2",
            reason="Действующая компания, опыт закупок из Китая, контейнерные объёмы и бюджет свыше 20 000 USD.",
        ),
        kommo_update=KommoUpdateDecision(
            should_add_note=True,
            note="Модельный текст будет заменён детерминированным комментарием.",
            should_change_stage=True,
            new_stage="Ожидание данных клиента",
            stage_reason="Клиент должен прислать список и параметры товаров.",
            should_create_task=True,
            task_title="Проверить материалы от Marcin Bojdo перед разговором",
            task_description=(
                "Проверить, прислал ли Marcin Bojdo список 3–5 приоритетных товаров, "
                "фотографии или спецификации, объёмы и текущие цены. Если данные получены — "
                "подготовить предварительную оценку перед разговором. Если данных нет — "
                "отправить напоминание и согласовать точное время звонка."
            ),
            task_due_date=None,
        ),
        client_message=ClientMessageDraft(
            language="pl",
            channel="WhatsApp",
            text=(
                "Dzień dobry Panie Marcinie,\n\n"
                "dziękuję za dzisiejszą krótką rozmowę. Żebyśmy mogli przygotować się "
                "do rozmowy w poniedziałek, proszę przesłać 3–5 najważniejszych produktów, "
                "zdjęcia lub specyfikacje, orientacyjne ilości oraz obecne ceny zakupu, "
                "jeżeli mogą je Państwo udostępnić. Proszę również napisać, o której "
                "godzinie w poniedziałek będzie Panu najwygodniej porozmawiać.\n\n"
                "Pozdrawiam,\nKirill\nBuy & Bring Solutions"
            ),
            send_automatically=False,
        ),
        needs_review=False,
        review_reason="",
    )


@pytest.mark.asyncio
async def test_marcin_bojdo_call_uses_kommo_context_and_applies_confirmed_actions():
    details_before = _marcin_details()
    details_after = _marcin_details(
        status_id=30, status_name="Ожидание данных клиента"
    )
    future_timestamp = int((datetime.now(timezone.utc) + timedelta(days=30)).timestamp())

    with (
        patch(
            "app.services.call_crm_agent_service.kommo_service.get_lead_details",
            new_callable=AsyncMock,
            side_effect=[details_before, details_after],
        ),
        patch(
            "app.services.call_crm_agent_service.kommo_service.get_open_lead_tasks",
            new_callable=AsyncMock,
            side_effect=[[], []],
        ),
        patch(
            "app.services.call_crm_agent_service.kommo_service.get_pipeline_statuses",
            new_callable=AsyncMock,
            return_value=[{"id": 30, "name": "Ожидание данных клиента"}],
        ),
        patch(
            "app.services.call_crm_agent_service.kommo_service.add_common_note",
            new_callable=AsyncMock,
            return_value=True,
        ) as add_note,
        patch(
            "app.services.call_crm_agent_service.kommo_service.update_kommo_lead",
            new_callable=AsyncMock,
            return_value={"lead_id": 987654},
        ) as update_stage,
        patch(
            "app.services.call_crm_agent_service.kommo_service.create_lead_task",
            new_callable=AsyncMock,
            return_value={"task_id": 777},
        ) as create_task,
        patch(
            "app.services.call_crm_agent_service.analyse_crm_call",
            new_callable=AsyncMock,
            return_value=_marcin_analysis(),
        ),
        patch(
            "app.services.call_crm_agent_service._task_timestamp",
            return_value=future_timestamp,
        ),
    ):
        result = await call_crm_agent_service.process_completed_call(
            transcript=(
                "Użytkownik teraz rozmawia. Klient powiedział, że kupuje już towary z Chin "
                "przez pośredników. Niektóre zakupy to dwa albo trzy kontenery. Chce zwiększyć "
                "konkurencyjność i rozważyć bardziej bezpośrednią współpracę z dostawcami. "
                "Ma wiele produktów. Umówiliśmy się na poniedziałek; wcześniej prześle informacje."
            ),
            kommo_deal_id=987654,
            call_date=date(2026, 7, 31),
            manager_name="Kirill",
        )

    analysis = result.analysis
    assert analysis.identity.lead_id == "1440927531405186"
    assert analysis.identity.contact_name == "Marcin Bojdo"
    assert analysis.identity.company_name == "BOFERM"
    assert analysis.identity.phone == "+48519392197"
    assert analysis.identity.email == "Marcin.bojdo@boferm.pl"
    assert analysis.identity.identity_confidence == 1
    assert analysis.priority.value == "A2"
    assert analysis.kommo_update.task_due_date == date(2026, 8, 3)
    assert analysis.kommo_update.new_stage == "Ожидание данных клиента"
    assert analysis.client_message.send_automatically is False
    assert "[Imię" not in analysis.client_message.text
    assert "плох" not in analysis.kommo_update.note.casefold()
    assert "обман" not in analysis.kommo_update.note.casefold()
    assert "Нет новых подтверждений" not in analysis.kommo_update.note

    assert [item.action for item in analysis.actions_completed] == [
        "note_created",
        "stage_updated",
        "task_created",
    ]
    add_note.assert_awaited_once()
    update_stage.assert_awaited_once_with(987654, status_id=30)
    create_task.assert_awaited_once()
    assert result.legacy_analysis["client"]["name"] == "Marcin Bojdo"
    assert result.legacy_analysis["lead"]["budget"] == "powyżej 20 000 USD"
    assert "crm_call_analysis" in result.legacy_analysis


@pytest.mark.asyncio
async def test_unsupported_quality_and_fraud_claims_force_review_and_no_kommo_writes():
    with (
        patch(
            "app.services.call_crm_agent_service.kommo_service.get_lead_details",
            new_callable=AsyncMock,
            return_value=_marcin_details(),
        ),
        patch(
            "app.services.call_crm_agent_service.kommo_service.get_open_lead_tasks",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch(
            "app.services.call_crm_agent_service.analyse_crm_call",
            new_callable=AsyncMock,
            return_value=_marcin_analysis(include_hallucination=True),
        ),
        patch(
            "app.services.call_crm_agent_service.kommo_service.add_common_note",
            new_callable=AsyncMock,
        ) as add_note,
        patch(
            "app.services.call_crm_agent_service.kommo_service.update_kommo_lead",
            new_callable=AsyncMock,
        ) as update_stage,
        patch(
            "app.services.call_crm_agent_service.kommo_service.create_lead_task",
            new_callable=AsyncMock,
        ) as create_task,
    ):
        result = await call_crm_agent_service.process_completed_call(
            transcript=(
                "Klient kupuje przez pośredników i chce rozważyć bardziej bezpośrednią współpracę. "
                "W poniedziałek prześle listę produktów."
            ),
            kommo_deal_id=987654,
            call_date=date(2026, 7, 31),
        )

    assert result.analysis.needs_review is True
    assert "неподтверждённые" in result.analysis.review_reason
    assert all("обман" not in item.casefold() for item in result.analysis.confirmed_in_call)
    assert all("качеств" not in item.casefold() for item in result.analysis.confirmed_in_call)
    assert result.analysis.actions_completed == []
    add_note.assert_not_awaited()
    update_stage.assert_not_awaited()
    create_task.assert_not_awaited()


def test_transcript_cleanup_removes_operator_and_duplicate_phrases():
    cleaned = call_crm_agent_service.clean_transcript(
        "Proszę zostawić wiadomość po sygnale. Klient prześle listę. "
        "Klient prześle listę. [noise]"
    )
    assert "wiadomość po sygnale" not in cleaned.casefold()
    assert cleaned.count("Klient prześle listę") == 1


def test_polish_phone_normalization():
    assert call_crm_agent_service.normalize_polish_phone("+48 519 392 197") == "+48519392197"
    assert call_crm_agent_service.normalize_polish_phone("519-392-197") == "+48519392197"
    assert call_crm_agent_service.normalize_polish_phone("(48) 519 392 197") == "+48519392197"
