from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.services import facebook_lead_onboarding_runtime as onboarding
from app.services.google_sheets_service import SpreadsheetRow


def _row(*, number: str = "", product: str = "narzędzia") -> SpreadsheetRow:
    return SpreadsheetRow(
        row_number=167,
        phone="728 387 128",
        email="jan_ovo@wp.pl",
        client_name="Andrzej Janka",
        company=None,
        product=product,
        lead_number=number,
        lead_status=None,
        marketing_comment="",
        budget="$5_000_-_$10_000",
        contact_channel="whats_app",
        region="kujawsko pomorskie",
    )


def _details(*, name: str = "Facebook №1479023253985582") -> dict:
    return {
        "id": 15402709,
        "name": name,
        "status_id": 100,
        "pipeline_id": 10,
        "responsible_user_id": 77,
        "url": "https://example.kommo.com/leads/detail/15402709",
        "contacts": [
            {
                "name": "Andrzej Janka",
                "phones": ["+48 728 387 128"],
                "emails": ["jan_ovo@wp.pl"],
            }
        ],
    }


def test_facebook_detection_and_tools_translation():
    assert onboarding._is_facebook({"name": "Facebook №1479023253985582"}) is True
    assert onboarding._is_facebook({"name": "167 - Инструменты"}) is False
    assert onboarding._product_ru_source("narzędzia") == "Инструменты"


def test_generic_tools_lead_is_qualification_priority_c():
    priority = onboarding._priority(
        _row(),
        {"lead": {"quantity": None, "timeline": None}, "missing_questions": ["Количество"]},
    )
    assert priority["grade"] == "C"
    assert "квалификация" in priority["readiness"]


def test_preview_markup_has_safe_callbacks_and_ready_whatsapp_link():
    markup = onboarding._markup(
        {
            "phone": "+48 728 387 128",
            "recommended_channel": "whatsapp",
            "whatsapp_message": "Dzień dobry, proszę o listę produktów.",
        }
    )
    buttons = [button for row in markup["inline_keyboard"] for button in row]
    callbacks = [button["callback_data"] for button in buttons if "callback_data" in button]
    assert all(len(value.encode("utf-8")) <= 64 for value in callbacks)
    assert any(button.get("url", "").startswith("https://wa.me/48728387128") for button in buttons)


@pytest.mark.asyncio
async def test_discovery_starts_from_unsorted_facebook_and_matches_sheet_by_phone():
    rows = [_row()]
    unsorted = {
        "leads": [
            {"id": 15402709, "name": "Facebook №1479023253985582", "created_at": 1},
            {"id": 15402710, "name": "Manual request", "created_at": 2},
        ]
    }
    enriched = [
        {
            "id": 15402709,
            "name": "Facebook №1479023253985582",
            "created_at": 1,
            "phones": ["+48 728 387 128"],
            "emails": ["jan_ovo@wp.pl"],
            "contact_name": "Andrzej Janka",
            "company": None,
        },
        {
            "id": 15402710,
            "name": "Manual request",
            "created_at": 2,
            "phones": ["+48 728 387 128"],
            "emails": [],
            "contact_name": "Andrzej Janka",
            "company": None,
        },
    ]
    with (
        patch.object(onboarding.google_sheets_service, "get_rows", return_value=rows),
        patch.object(onboarding.kommo_service, "get_all_unsorted_leads", new_callable=AsyncMock, return_value=unsorted),
        patch.object(onboarding.kommo_service, "enrich_leads_with_contacts", new_callable=AsyncMock, return_value=enriched),
    ):
        result = await onboarding.discover()

    assert result["items"] == [
        {"lead_id": 15402709, "row_number": 167, "matched_by": "phone"}
    ]
    assert result["unmatched"] == []


@pytest.mark.asyncio
async def test_apply_writes_y_then_stage_note_task_and_final_title():
    row = _row()
    state = {"name": "Facebook №1479023253985582", "status_id": 100}
    base_details = _details()
    events: list[str] = []

    def write_sheet(updates):
        events.append("sheet")
        assert updates[0]["new_lead_number"] == "167"
        assert updates[0]["new_comment"] == ""
        return {"updated_count": 1, "updated": updates, "skipped": []}

    async def get_details(_lead_id):
        details = _details(name=str(state["name"]))
        details["status_id"] = state["status_id"]
        return details

    async def update_lead(lead_id, **changes):
        assert lead_id == 15402709
        if "status_id" in changes:
            events.append("stage")
            state["status_id"] = changes["status_id"]
        if "name" in changes:
            events.append("name")
            state["name"] = changes["name"]

    async def add_note(_lead_id, _text):
        events.append("note")

    async def add_task(**_kwargs):
        events.append("task")

    async def no_sleep(_seconds):
        return None

    preview = {
        "lead_id": 15402709,
        "row_number": 167,
        "lead_number": "167",
        "old_name": "Facebook №1479023253985582",
        "product_ru": "Инструменты",
        "row_fingerprint": onboarding._fingerprint(row),
        "digest": onboarding._digest(row, base_details, "167", "Инструменты"),
        "new_name": "167 - Инструменты",
        "target_status_id": 200,
        "matched_by": "phone",
        "analysis_note": "[BBS-SMART-ONBOARD-167-15402709]\nПолный анализ",
        "task_text": "Написать Andrzej и получить перечень · №167",
        "task_due_at": 1_800_000_000,
        "kommo_url": base_details["url"],
    }

    with (
        patch.object(onboarding.google_sheets_service, "get_rows", return_value=[row]),
        patch.object(onboarding.google_sheets_service, "apply_lead_registry_updates", side_effect=write_sheet),
        patch.object(onboarding.kommo_service, "get_lead_details", side_effect=get_details),
        patch.object(onboarding.kommo_service, "update_kommo_lead", side_effect=update_lead),
        patch.object(onboarding.kommo_service, "get_recent_common_notes", new_callable=AsyncMock, return_value=[]),
        patch.object(onboarding.kommo_service, "add_common_note", side_effect=add_note),
        patch.object(onboarding.kommo_service, "get_open_lead_tasks", new_callable=AsyncMock, return_value=[]),
        patch.object(onboarding.kommo_service, "create_lead_task", side_effect=add_task),
        patch("app.services.facebook_lead_onboarding_hardening_runtime.asyncio.sleep", side_effect=no_sleep),
    ):
        result = await onboarding.apply(preview)

    assert events == ["sheet", "stage", "note", "task", "name"]
    assert result["stale"] is False
    assert result["note_added"] is True
    assert result["task_added"] is True
    assert state["name"] == "167 - Инструменты"


@pytest.mark.asyncio
async def test_apply_stops_when_sheet_and_kommo_contact_no_longer_match():
    row = _row()
    details = _details()
    preview = {
        "lead_id": 15402709,
        "row_number": 167,
        "lead_number": "167",
        "old_name": "Facebook №1479023253985582",
        "product_ru": "Инструменты",
        "row_fingerprint": onboarding._fingerprint(row),
        "digest": onboarding._digest(row, details, "167", "Инструменты"),
        "new_name": "167 - Инструменты",
        "target_status_id": 200,
    }
    changed = _details()
    changed["contacts"][0]["phones"] = ["+48 999 999 999"]
    changed["contacts"][0]["emails"] = ["other@example.com"]
    changed["contacts"][0]["name"] = "Different Person"

    with (
        patch.object(onboarding.google_sheets_service, "get_rows", return_value=[row]),
        patch.object(onboarding.kommo_service, "get_lead_details", new_callable=AsyncMock, return_value=changed),
        patch.object(onboarding.google_sheets_service, "apply_lead_registry_updates") as write_sheet,
        patch.object(onboarding.kommo_service, "update_kommo_lead", new_callable=AsyncMock) as update_lead,
    ):
        result = await onboarding.apply(preview)

    assert result == {"stale": True, "reason": "contact_match_changed"}
    write_sheet.assert_not_called()
    update_lead.assert_not_awaited()
