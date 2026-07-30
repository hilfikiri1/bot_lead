from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.services import facebook_lead_onboarding_runtime as onboarding
from app.services.final_compat_runtime import install_final_compat_runtime
from app.services.google_sheets_service import SpreadsheetRow


def _row(row_number: int, lead_number: str) -> SpreadsheetRow:
    return SpreadsheetRow(
        row_number=row_number,
        phone="728387128" if row_number == 167 else "500000000",
        email="jan@example.com" if row_number == 167 else "other@example.com",
        client_name="Andrzej" if row_number == 167 else "Other",
        company=None,
        product="narzędzia",
        lead_number=lead_number,
    )


@pytest.mark.asyncio
async def test_duplicate_y_is_blocked_before_original_apply_or_any_write():
    install_final_compat_runtime()
    preview = {"lead_id": 10, "row_number": 167, "lead_number": "167"}
    rows = [_row(167, ""), _row(120, "167")]
    original = onboarding.apply
    underlying = getattr(original, "__closure__", None)

    with (
        patch.object(onboarding.google_sheets_service, "get_rows", return_value=rows),
        patch.object(
            onboarding.kommo_service,
            "get_lead_details",
            new=AsyncMock(return_value={"id": 10, "name": "Facebook №1"}),
        ),
        patch.object(
            onboarding.google_sheets_service,
            "apply_lead_registry_updates",
        ) as sheet_write,
        patch.object(
            onboarding.kommo_service,
            "update_kommo_lead",
            new=AsyncMock(),
        ) as kommo_write,
    ):
        result = await onboarding.apply(preview)

    assert result["stale"] is True
    assert result["reason"] == "duplicate_sheet_lead_number"
    assert result["duplicate_rows"] == [120]
    sheet_write.assert_not_called()
    kommo_write.assert_not_awaited()
    assert underlying is not None


@pytest.mark.asyncio
async def test_kommo_number_conflict_is_blocked_before_sheet_write():
    install_final_compat_runtime()
    preview = {"lead_id": 10, "row_number": 167, "lead_number": "167"}
    with (
        patch.object(onboarding.google_sheets_service, "get_rows", return_value=[_row(167, "")]),
        patch.object(
            onboarding.kommo_service,
            "get_lead_details",
            new=AsyncMock(return_value={"id": 10, "name": "166 - Другая заявка"}),
        ),
        patch.object(
            onboarding.google_sheets_service,
            "apply_lead_registry_updates",
        ) as sheet_write,
    ):
        result = await onboarding.apply(preview)

    assert result == {
        "stale": True,
        "reason": "kommo_number_conflict",
        "existing_number": "166",
    }
    sheet_write.assert_not_called()


@pytest.mark.asyncio
async def test_stale_confirmation_keeps_current_lead_on_screen():
    install_final_compat_runtime()
    state = {
        "mode": "smart_lead_onboarding",
        "queue": [{"lead_id": 10, "row_number": 167}],
        "index": 0,
        "results": [],
        "current_preview": {"lead_id": 10, "lead_number": "167"},
    }
    with (
        patch.object(onboarding.settings, "google_sheets_write_enabled", True),
        patch.object(
            onboarding,
            "apply",
            new=AsyncMock(
                return_value={
                    "stale": True,
                    "reason": "duplicate_sheet_lead_number",
                    "duplicate_rows": [120],
                }
            ),
        ),
        patch.object(
            onboarding.telegram_state_service,
            "set_state",
            new=AsyncMock(),
        ) as set_state,
        patch.object(
            onboarding.telegram_service,
            "send_message",
            new=AsyncMock(),
        ) as send,
        patch.object(onboarding, "_show_current", new=AsyncMock()) as show_next,
    ):
        await onboarding._confirm(1, 99, state)

    assert state["index"] == 0
    assert state["current_preview"]["lead_number"] == "167"
    assert state["results"] == []
    set_state.assert_awaited_once()
    show_next.assert_not_awaited()
    assert "Лид не изменён" in send.await_args_list[-1].args[1]
