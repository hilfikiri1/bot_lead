from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services import communication_timeline_runtime, lead_registry_runtime
from app.services.google_sheets_service import SpreadsheetRow


def _row(
    row_number: int,
    *,
    product: str,
    lead_number: str = "",
    phone: str | None = None,
    email: str | None = None,
    client_name: str | None = None,
    status: str | None = None,
    comment: str | None = None,
) -> SpreadsheetRow:
    return SpreadsheetRow(
        row_number=row_number,
        phone=phone,
        email=email,
        client_name=client_name,
        company=None,
        product=product,
        lead_number=lead_number,
        lead_status=status,
        marketing_comment=comment,
        budget=None,
        contact_channel="whats_app",
        region="Polska",
    )


@pytest.mark.asyncio
async def test_row_number_policy_prepares_creation_when_kommo_match_is_missing(monkeypatch):
    rows = [
        _row(
            165,
            product="magazyn energii dyness Stack100",
            phone="48504051504",
            email="biuro@fotowoltaika.biz.pl",
            client_name="Grzegorz Nowicki",
            status="Первый контакт",
        ),
        _row(
            166,
            product="herbaty ryż mango",
            phone="48798082262",
            email="niuppa1@yahoo.com",
            client_name="Przemek Bryłka",
        ),
    ]
    report = {
        "onboarding_actions": [],
        "kommo_duplicates": [],
        "table_duplicates": [],
    }
    monkeypatch.setattr(
        lead_registry_runtime,
        "_find_exact_contact_leads",
        AsyncMock(return_value=[]),
    )

    result = await lead_registry_runtime._enhance_report(report, rows)

    assert result["updates_count"] == 2
    assert result["number_assignments_count"] == 2
    assert [
        (item["row_number"], item["new_lead_number"])
        for item in result["sheet_updates"]
    ] == [(166, "166"), (165, "165")]
    assert result["onboarding_actions"] == []
    assert result["create_count"] == 2
    assert [item["row_number"] for item in result["create_actions"]] == [166, 165]
    assert result["unmatched_table_rows"] == []


@pytest.mark.asyncio
async def test_existing_match_uses_sheet_row_number_not_global_counter(monkeypatch):
    row = _row(
        165,
        product="magazyn energii dyness Stack100",
        phone="48504051504",
        client_name="Grzegorz Nowicki",
        comment="Не менять",
    )
    report = {
        "onboarding_actions": [
            {
                "kommo_lead_id": 15011969,
                "row_number": 165,
                "lead_number": "999",
                "old_name": "Facebook lead",
                "new_name": "999 - Накопитель",
                "short_product_ru": "Накопитель энергии",
                "matched_by": "phone",
                "target_status_id": None,
                "task_due_at": 1_900_000_000,
                "contact_card": {},
                "url": "https://example.invalid/leads/detail/15011969",
            }
        ],
        "kommo_duplicates": [],
        "table_duplicates": [],
    }
    monkeypatch.setattr(
        lead_registry_runtime,
        "_find_exact_contact_leads",
        AsyncMock(return_value=[]),
    )

    result = await lead_registry_runtime._enhance_report(report, [row])

    action = result["onboarding_actions"][0]
    assert action["lead_number"] == "165"
    assert action["new_name"] == "165 - Накопитель энергии"
    assert action["contact_card"]["lead_number"] == "165"
    assert result["sheet_updates"][0]["new_lead_number"] == "165"
    assert result["sheet_updates"][0]["old_comment"] == "Не менять"
    assert result["sheet_updates"][0]["new_comment"] == "Не менять"


@pytest.mark.asyncio
async def test_existing_conflicting_kommo_number_is_not_overwritten(monkeypatch):
    row = _row(165, product="silnik", phone="48500111222")
    report = {
        "onboarding_actions": [
            {
                "kommo_lead_id": 77,
                "row_number": 165,
                "lead_number": "165",
                "old_name": "120 - Двигатели",
                "new_name": "165 - Двигатели",
                "short_product_ru": "Двигатели",
                "matched_by": "phone",
                "target_status_id": None,
                "task_due_at": 1_900_000_000,
                "contact_card": {},
                "url": None,
            }
        ],
        "kommo_duplicates": [],
        "table_duplicates": [],
    }
    monkeypatch.setattr(
        lead_registry_runtime,
        "_find_exact_contact_leads",
        AsyncMock(return_value=[]),
    )

    result = await lead_registry_runtime._enhance_report(report, [row])

    action = result["onboarding_actions"][0]
    assert action["new_name"] == "120 - Двигатели"
    assert action["number_conflict"] == {
        "kommo_number": "120",
        "sheet_number": "165",
    }
    assert result["kommo_renames"] == []
    assert result["sheet_updates"][0]["new_lead_number"] == "165"


def test_unique_direct_contact_prefers_matching_row_number():
    row = _row(166, product="herbaty ryż mango")
    leads = [
        {"id": 1, "name": "150 - Inny produkt", "closed_at": None},
        {"id": 2, "name": "166 - Herbata mango", "closed_at": None},
    ]

    selected = lead_registry_runtime._select_unique_contact_lead(row, leads)

    assert selected is not None
    assert selected["id"] == 2


@pytest.mark.asyncio
async def test_timeline_callback_bypasses_generic_agent_parser():
    original = AsyncMock(return_value=SimpleNamespace(text="wrong"))

    result = await communication_timeline_runtime._delegate_agent_callback(
        original,
        db=object(),
        callback_data="agent:comms:11307801:0",
        telegram_user_id=1,
        chat_id=2,
    )

    assert result is None
    original.assert_not_awaited()


@pytest.mark.asyncio
async def test_non_timeline_agent_callback_still_delegates():
    expected = SimpleNamespace(text="ok")
    original = AsyncMock(return_value=expected)

    result = await communication_timeline_runtime._delegate_agent_callback(
        original,
        db=object(),
        callback_data="agent:lead:11307801",
        telegram_user_id=1,
        chat_id=2,
    )

    assert result is expected
    original.assert_awaited_once()
