"""Tests for richer/faster first-contact onboarding analysis."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.services import lead_status_sync_service, onboarding_briefing_service, product_title_service
from app.services.google_sheets_service import SpreadsheetRow
from app.services.onboarding_briefing_service import (
    OnboardingBriefing,
    build_heuristic_briefing,
)


def _row(
    *,
    row_number: int,
    product: str = "Artykuły elektryczne",
    phone: str | None = "606999210",
    lead_number: str = "",
    budget: str | None = "$10_000_-__$20_000",
    channel: str | None = "połączenie_telefoniczne",
) -> SpreadsheetRow:
    return SpreadsheetRow(
        row_number=row_number,
        phone=phone,
        email=None,
        client_name="Henryk",
        company=None,
        product=product,
        lead_number=lead_number,
        lead_status="",
        marketing_comment=None,
        budget=budget,
        contact_channel=channel,
        region="Polska",
    )


def test_heuristic_briefing_covers_about_and_talk_points():
    briefing = build_heuristic_briefing(
        product="Artykuły elektryczne",
        product_ru="Электротовары",
        budget="$10_000_-__$20_000",
        channel="połączenie_telefoniczne",
        region="Polska",
        client_name="Henryk",
    )
    assert "Электротовары" in briefing.about_ru or "Artykuły" in briefing.about_ru
    assert briefing.call_goal_ru
    assert len(briefing.talk_points_ru) >= 5
    assert any("объём" in point.casefold() or "объем" in point.casefold() for point in briefing.talk_points_ru)


def test_electrical_goods_deterministic_title():
    assert (
        product_title_service.deterministic_short_title("Artykuły elektryczne")
        == "Электротовары"
    )


def test_analysis_note_includes_talk_sections():
    briefing = OnboardingBriefing(
        short_product_ru="Электротовары",
        about_ru="Клиент ищет электротовары из Китая под опт.",
        talk_points_ru=["Уточнить категорию", "Спросить объём партии"],
        call_goal_ru="Понять спецификацию и бюджет.",
    )
    note = lead_status_sync_service._analysis_note(
        row=_row(row_number=168),
        lead={"id": 15466945, "name": "Facebook #1"},
        lead_number="168",
        product_ru="Электротовары",
        matched_by="phone",
        task_text="Позвонить Henryk по лиду №168: Электротовары",
        briefing=briefing,
    )
    assert "О ЧЁМ ЗАЯВКА" in note
    assert "О ЧЁМ ГОВОРИТЬ" in note
    assert "ЦЕЛЬ ПЕРВОГО КОНТАКТА" in note
    assert "Клиент ищет электротовары" in note
    assert "Уточнить категорию" in note


def test_newest_new_rows_limits_to_latest_empty_y():
    rows = [
        _row(row_number=160, product="old A"),
        _row(row_number=161, product="old B"),
        _row(row_number=168, product="fresh"),
        _row(row_number=150, lead_number="150", product="already numbered"),
    ]
    with patch.object(
        lead_status_sync_service.settings, "lead_status_sync_max_new_rows", 2
    ):
        newest = lead_status_sync_service._newest_new_rows(rows)
    assert [row.row_number for row in newest] == [168, 161]


@pytest.mark.asyncio
async def test_report_prefers_newest_row_and_uses_briefing():
    rows = [
        _row(row_number=160, product="stary produkt", phone="500111222"),
        _row(row_number=168, product="Artykuły elektryczne", phone="606999210"),
    ]
    kommo_result = {
        "leads": [
            {
                "id": 11,
                "name": "Facebook old",
                "status_name": "Incoming leads",
                "contact_id": 1,
            },
            {
                "id": 22,
                "name": "Facebook new",
                "status_name": "Incoming leads",
                "contact_id": 2,
            },
        ],
        "truncated": False,
        "pipeline_id": 1,
        "pipeline_name": "Польша",
    }
    enriched = [
        {
            **kommo_result["leads"][0],
            "phones": ["+48 500 111 222"],
            "emails": [],
            "contact_name": "Old",
        },
        {
            **kommo_result["leads"][1],
            "phones": ["+48 606 999 210"],
            "emails": [],
            "contact_name": "Henryk",
        },
    ]
    briefing = OnboardingBriefing(
        short_product_ru="Электротовары",
        about_ru="Свежий лид по электротоварам.",
        talk_points_ru=["Спросить модель", "Уточнить объём"],
        call_goal_ru="Квалифицировать запрос.",
    )
    with (
        patch(
            "app.services.lead_status_sync_service.google_sheets_service.get_rows",
            return_value=rows,
        ),
        patch(
            "app.services.lead_status_sync_service.kommo_service.get_all_leads_for_status_sync",
            new_callable=AsyncMock,
            return_value=kommo_result,
        ),
        patch(
            "app.services.lead_status_sync_service.kommo_service.enrich_leads_with_contacts",
            new_callable=AsyncMock,
            return_value=enriched,
        ),
        patch(
            "app.services.lead_status_sync_service._safe_briefing",
            new_callable=AsyncMock,
            return_value=briefing,
        ),
        patch.object(
            lead_status_sync_service.settings, "lead_status_sync_max_new_rows", 1
        ),
    ):
        report = await lead_status_sync_service.build_status_sync_report()

    assert report["newest_first"] is True
    assert report["new_rows_count"] == 1
    assert report["onboarding_actions"][0]["row_number"] == 168
    note = report["onboarding_actions"][0]["analysis_note"]
    assert "О ЧЁМ ЗАЯВКА" in note
    assert "Свежий лид по электротоварам" in note
    assert "О ЧЁМ ГОВОРИТЬ" in note


@pytest.mark.asyncio
async def test_build_onboarding_briefing_falls_back_without_openai():
    onboarding_briefing_service.clear_briefing_cache()
    with patch.object(onboarding_briefing_service.settings, "openai_api_key", ""):
        briefing = await onboarding_briefing_service.build_onboarding_briefing(
            product="Artykuły elektryczne",
            budget="$10_000",
            channel="połączenie_telefoniczne",
            region="Polska",
            client_name="Henryk",
        )
    assert briefing.short_product_ru == "Электротовары"
    assert briefing.talk_points_ru
