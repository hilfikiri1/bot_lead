from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.services import facebook_lead_onboarding_hardening_runtime as hardening
from app.services import facebook_lead_onboarding_runtime as onboarding
from app.services.google_sheets_service import SpreadsheetRow


def _row(*, number: str = "") -> SpreadsheetRow:
    return SpreadsheetRow(
        row_number=167,
        phone="728387128",
        email="jan_ovo@wp.pl",
        client_name="Andrzej Janka",
        company=None,
        product="narzędzia",
        lead_number=number,
        lead_status=None,
        marketing_comment="",
        budget="$5_000_-_$10_000",
        contact_channel="whats_app",
        region="kujawsko pomorskie",
    )


def _details(state: dict[str, object] | None = None) -> dict:
    state = state or {"name": "Facebook #12312412", "status_id": 100}
    return {
        "id": 15402709,
        "name": state["name"],
        "status_id": state["status_id"],
        "pipeline_id": 10,
        "responsible_user_id": 77,
        "url": "https://example.kommo.com/leads/detail/15402709",
        "contacts": [{"name": "Andrzej Janka", "phones": [], "emails": []}],
        "custom_fields": [
            {
                "name": "Proszę podać swój numer kontaktowy",
                "values": [{"value": "728387128"}],
            },
            {
                "name": "Poczta",
                "values": [{"value": "jan_ovo@wp.pl"}],
            },
            {
                "name": "Jakiego produktu potrzebuje Twoja firma",
                "values": [{"value": "narzędzia"}],
            },
        ],
    }


def _preview(row: SpreadsheetRow, details: dict) -> dict:
    return {
        "lead_id": 15402709,
        "row_number": 167,
        "lead_number": "167",
        "old_name": "Facebook #12312412",
        "new_name": "167 - Инструменты",
        "target_status_id": 200,
        "matched_by": "phone",
        "product_ru": "Инструменты",
        "row_fingerprint": onboarding._fingerprint(row),
        "digest": onboarding._digest(row, details, "167", "Инструменты"),
        "analysis_note": "[BBS-SMART-ONBOARD-167-15402709]\nЛИЧНЫЙ АНАЛИЗ",
        "task_text": "Получить перечень инструментов и количество · №167",
        "task_due_at": 1_900_000_000,
        "kommo_url": details["url"],
    }


def test_identity_uses_phone_and_email_from_facebook_lead_fields():
    snapshot = hardening._identity_snapshot(_details())
    assert snapshot["phones"] == ["728387128"]
    assert snapshot["emails"] == ["jan_ovo@wp.pl"]
    assert snapshot["contact_name"] == "Andrzej Janka"


@pytest.mark.asyncio
async def test_discovery_matches_unsorted_facebook_lead_from_form_phone():
    row = _row()
    unsorted = {
        "leads": [
            {
                "id": 15402709,
                "name": "Facebook #12312412",
                "created_at": 1,
            }
        ]
    }
    with (
        patch.object(onboarding.google_sheets_service, "get_rows", return_value=[row]),
        patch.object(
            onboarding.kommo_service,
            "get_all_unsorted_leads",
            new_callable=AsyncMock,
            return_value=unsorted,
        ),
        patch.object(
            onboarding.kommo_service,
            "get_lead_details",
            new_callable=AsyncMock,
            return_value=_details(),
        ),
    ):
        result = await hardening._discover_from_facebook_form_fields()

    assert result["items"] == [
        {"lead_id": 15402709, "row_number": 167, "matched_by": "phone"}
    ]
    assert result["unmatched"] == []


@pytest.mark.asyncio
async def test_apply_order_is_y_stage_note_task_and_final_name():
    row = _row()
    state: dict[str, object] = {
        "name": "Facebook #12312412",
        "status_id": 100,
    }
    initial = _details(state)
    preview = _preview(row, initial)
    events: list[str] = []

    def get_rows(*, force_refresh=False):
        return [row]

    def write_sheet(updates):
        events.append("sheet")
        assert updates[0]["new_lead_number"] == "167"
        assert updates[0]["old_comment"] == updates[0]["new_comment"] == ""
        return {"updated_count": 1, "updated": updates, "skipped": []}

    async def get_details(_lead_id):
        return _details(state)

    async def update_lead(_lead_id, **changes):
        if "status_id" in changes:
            events.append("stage")
            state["status_id"] = changes["status_id"]
        if "name" in changes:
            events.append("name")
            state["name"] = changes["name"]
        return {"id": 15402709, **changes}

    async def add_note(_lead_id, text):
        events.append("note")
        assert "ЛИЧНЫЙ АНАЛИЗ" in text

    async def add_task(**kwargs):
        events.append("task")
        assert "№167" in kwargs["text"]

    async def no_sleep(_seconds):
        return None

    with (
        patch.object(onboarding.google_sheets_service, "get_rows", side_effect=get_rows),
        patch.object(
            onboarding.google_sheets_service,
            "apply_lead_registry_updates",
            side_effect=write_sheet,
        ),
        patch.object(
            onboarding.kommo_service,
            "get_lead_details",
            side_effect=get_details,
        ),
        patch.object(
            onboarding.kommo_service,
            "update_kommo_lead",
            side_effect=update_lead,
        ),
        patch.object(
            onboarding.kommo_service,
            "get_recent_common_notes",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch.object(
            onboarding.kommo_service,
            "add_common_note",
            side_effect=add_note,
        ),
        patch.object(
            onboarding.kommo_service,
            "get_open_lead_tasks",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch.object(
            onboarding.kommo_service,
            "create_lead_task",
            side_effect=add_task,
        ),
        patch.object(hardening.asyncio, "sleep", side_effect=no_sleep),
    ):
        result = await hardening._apply_in_safe_order(preview)

    assert result["stale"] is False
    assert result["completed_steps"] == [
        "google_sheets_y",
        "first_contact",
        "analysis_note",
        "qualification_task",
        "final_name",
    ]
    assert events == ["sheet", "stage", "note", "task", "name"]
    assert state["name"] == "167 - Инструменты"


@pytest.mark.asyncio
async def test_failed_final_title_is_partial_and_safe_to_retry():
    row = _row()
    state: dict[str, object] = {
        "name": "Facebook #12312412",
        "status_id": 100,
    }
    preview = _preview(row, _details(state))

    async def get_details(_lead_id):
        return _details(state)

    async def update_without_persisting_name(_lead_id, **changes):
        if "status_id" in changes:
            state["status_id"] = changes["status_id"]
        return {"id": 15402709, **changes}

    async def no_sleep(_seconds):
        return None

    with (
        patch.object(onboarding.google_sheets_service, "get_rows", return_value=[row]),
        patch.object(
            onboarding.google_sheets_service,
            "apply_lead_registry_updates",
            return_value={"updated_count": 1, "updated": [], "skipped": []},
        ),
        patch.object(
            onboarding.kommo_service,
            "get_lead_details",
            side_effect=get_details,
        ),
        patch.object(
            onboarding.kommo_service,
            "update_kommo_lead",
            side_effect=update_without_persisting_name,
        ),
        patch.object(
            onboarding.kommo_service,
            "get_recent_common_notes",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch.object(
            onboarding.kommo_service,
            "add_common_note",
            new_callable=AsyncMock,
        ),
        patch.object(
            onboarding.kommo_service,
            "get_open_lead_tasks",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch.object(
            onboarding.kommo_service,
            "create_lead_task",
            new_callable=AsyncMock,
        ),
        patch.object(hardening.asyncio, "sleep", side_effect=no_sleep),
    ):
        result = await hardening._apply_in_safe_order(preview)

    assert result["stale"] is True
    assert result["partial"] is True
    assert result["reason"] == "final_name_not_persisted"
    assert "qualification_task" in result["completed_steps"]
    assert "final_name" not in result["completed_steps"]
