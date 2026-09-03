from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.services import status_sync_all_runtime


def test_all_pending_rows_has_no_five_row_cap_and_keeps_newest_first():
    rows = [
        SimpleNamespace(row_number=index, product=f"Product {index}", lead_number="")
        for index in range(220, 228)
    ]
    rows.append(SimpleNamespace(row_number=228, product="Already done", lead_number="228"))
    rows.append(SimpleNamespace(row_number=229, product="", lead_number=""))

    pending = status_sync_all_runtime.all_pending_rows(rows)

    assert [row.row_number for row in pending] == list(range(227, 219, -1))
    assert len(pending) == 8


def test_all_matched_contact_cards_includes_actions_even_if_kommo_write_failed():
    result = {
        "contact_cards": [
            {
                "lead_number": "231",
                "kommo_lead_id": 1001,
                "name": "Maria",
                "phone": "+48111111111",
            }
        ],
        "report": {
            "onboarding_actions": [
                {
                    "contact_card": {
                        "lead_number": "231",
                        "kommo_lead_id": 1001,
                        "name": "Maria",
                        "phone": "+48111111111",
                    }
                },
                {
                    "contact_card": {
                        "lead_number": "230",
                        "kommo_lead_id": 1002,
                        "name": "Henryk",
                        "phone": "+48222222222",
                    }
                },
                {
                    "contact_card": {
                        "lead_number": "229",
                        "kommo_lead_id": 1003,
                        "name": "Marek",
                        "phone": "+48333333333",
                    }
                },
                {
                    "contact_card": {
                        "lead_number": "228",
                        "kommo_lead_id": 1004,
                        "name": "No phone",
                        "phone": "",
                    }
                },
            ]
        },
    }

    cards = status_sync_all_runtime._all_matched_contact_cards(result)

    assert len(cards) == 3
    assert {card["lead_number"] for card in cards} == {"229", "230", "231"}


@pytest.mark.asyncio
async def test_missing_row_is_created_only_when_no_kommo_candidate_exists():
    row_with_candidate = SimpleNamespace(
        row_number=230,
        product="Maszyny",
        lead_number="",
        phone="111",
        email="",
        client_name="A",
        company="",
    )
    safe_row = SimpleNamespace(
        row_number=229,
        product="Equipment",
        lead_number="",
        phone="222",
        email="",
        client_name="B",
        company="",
    )
    lead = {"id": 10, "name": "Facebook lead", "phones": ["111"], "emails": []}

    match_result = SimpleNamespace(
        single=SimpleNamespace(row=row_with_candidate),
    )
    with (
        patch.object(
            status_sync_all_runtime.google_sheets_service,
            "get_rows",
            return_value=[row_with_candidate, safe_row],
        ),
        patch.object(
            status_sync_all_runtime.kommo_service,
            "get_all_leads_for_status_sync",
            new=AsyncMock(return_value={"leads": [lead]}),
        ),
        patch.object(
            status_sync_all_runtime.kommo_service,
            "enrich_leads_with_contacts",
            new=AsyncMock(return_value=[lead]),
        ),
        patch.object(
            status_sync_all_runtime,
            "match_lead_to_rows",
            return_value=match_result,
        ),
    ):
        safe, ambiguous = await status_sync_all_runtime._rows_without_any_kommo_candidate(
            {229, 230}
        )

    assert [row.row_number for row in safe] == [229]
    assert [row.row_number for row in ambiguous] == [230]


def test_sync_digest_changes_when_create_action_changes():
    base = {
        "onboarding_actions": [],
        "create_actions": [
            {
                "row_number": 229,
                "lead_number": "229",
                "new_name": "229 - Equipment",
                "contact_card": {"phone": "+48111", "email": ""},
            }
        ],
    }
    changed = {
        **base,
        "create_actions": [
            {
                "row_number": 229,
                "lead_number": "230",
                "new_name": "230 - Equipment",
                "contact_card": {"phone": "+48111", "email": ""},
            }
        ],
    }

    assert status_sync_all_runtime._sync_digest(base) != status_sync_all_runtime._sync_digest(changed)
