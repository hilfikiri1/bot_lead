from __future__ import annotations

from app.services import kommo_bulk_ignore_runtime as ignore_runtime
from app.services import kommo_bulk_json_runtime as bulk


def test_item_label_contains_kommo_id_and_lead_name():
    assert (
        bulk._item_label(
            {
                "lead_id": 20760893,
                "internal_lead_number": None,
                "lead_name": "209 - test lead",
            }
        )
        == "Kommo ID 20760893 · 209 - test lead"
    )


def test_item_label_contains_internal_number_name_and_kommo_id():
    assert (
        bulk._item_label(
            {
                "lead_id": 10001,
                "internal_lead_number": "218",
                "lead_name": "218 - laser",
            }
        )
        == "№218 · 218 - laser · Kommo ID 10001"
    )


def test_delete_preview_has_single_ignore_target_and_lead_name():
    report = {
        "actions_count": 1,
        "items": [
            {
                "lead_id": 20760893,
                "internal_lead_number": None,
                "lead_name": "209 - test lead",
                "action": "delete",
                "executable": True,
                "target_status_name": "Игнор",
                "changes": {},
                "note_text": "",
            }
        ],
        "unresolved": [],
        "warnings": [],
    }

    base_text = bulk.format_bulk_preview(report)
    text = base_text.replace(
        "Delete (не выполняется):",
        "DELETE → Игнор/неуспешно закрыть:",
    ).replace(
        "delete — пропустить",
        "delete",
    )

    assert "Kommo ID 20760893 · 209 - test lead" in text
    assert "delete → Игнор" in text
    assert "delete → Игнор → Игнор" not in text
