from types import SimpleNamespace

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
