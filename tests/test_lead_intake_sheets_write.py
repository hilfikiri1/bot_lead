"""google_sheets_service.write_internal_lead_number: writes only the ID column."""

from __future__ import annotations

from contextlib import ExitStack
from unittest.mock import MagicMock, patch

from app.services import google_sheets_service
from tests.lead_intake_helpers import make_row


def _configure(stack: ExitStack) -> None:
    for manager in (
        patch.object(google_sheets_service.settings, "google_sheets_write_enabled", True),
        patch.object(google_sheets_service.settings, "google_sheets_spreadsheet_id", "sheet-id"),
        patch.object(google_sheets_service.settings, "google_sheets_worksheet_name", "FB"),
        patch.object(google_sheets_service.settings, "google_sheets_lead_number_column", "Y"),
        patch.object(google_sheets_service.settings, "google_sheets_status_column", "W"),
        patch.object(google_sheets_service.settings, "google_sheets_comment_column", "X"),
        patch("app.services.google_sheets_service.is_configured", return_value=True),
    ):
        stack.enter_context(manager)


def test_write_internal_lead_number_touches_only_the_id_column():
    row_before = make_row(row_number=167, phone="+48 728 387 128", email="jan_ovo@wp.pl", lead_number=None)
    row_after = make_row(row_number=167, phone="+48 728 387 128", email="jan_ovo@wp.pl", lead_number="167")
    mock_service = MagicMock()

    with ExitStack() as stack:
        _configure(stack)
        stack.enter_context(
            patch("app.services.google_sheets_service.get_rows", side_effect=[[row_before], [row_after]])
        )
        stack.enter_context(
            patch("app.services.google_sheets_service._sheets_service", return_value=mock_service)
        )
        stack.enter_context(patch("app.services.google_sheets_service.clear_cache"))
        result = google_sheets_service.write_internal_lead_number(
            row_number=167,
            expected_row_fingerprint=None,
            new_number="167",
        )

    assert result == {"written": True, "verified": True, "row_number": 167}
    call = mock_service.spreadsheets.return_value.values.return_value.update.call_args
    assert call.kwargs["range"] == "'FB'!Y167"
    assert call.kwargs["body"] == {"values": [["167"]]}
    assert "W" not in call.kwargs["range"]
    assert "X" not in call.kwargs["range"]


def test_write_internal_lead_number_is_idempotent_when_already_written():
    row = make_row(row_number=167, lead_number="167")
    with ExitStack() as stack:
        _configure(stack)
        stack.enter_context(patch("app.services.google_sheets_service.get_rows", return_value=[row]))
        result = google_sheets_service.write_internal_lead_number(
            row_number=167, expected_row_fingerprint=None, new_number="167"
        )
    assert result == {"written": False, "reason": "already_written", "verified": True}


def test_write_internal_lead_number_refuses_to_overwrite_a_different_existing_number():
    row = make_row(row_number=167, lead_number="200")
    with ExitStack() as stack:
        _configure(stack)
        stack.enter_context(patch("app.services.google_sheets_service.get_rows", return_value=[row]))
        result = google_sheets_service.write_internal_lead_number(
            row_number=167, expected_row_fingerprint=None, new_number="167"
        )
    assert result["written"] is False
    assert result["reason"] == "row_already_has_different_number"


def test_write_internal_lead_number_detects_row_changed_since_match():
    row = make_row(row_number=167, phone="600000000", email="changed@example.com", lead_number=None)
    with ExitStack() as stack:
        _configure(stack)
        stack.enter_context(patch("app.services.google_sheets_service.get_rows", return_value=[row]))
        result = google_sheets_service.write_internal_lead_number(
            row_number=167,
            expected_row_fingerprint=("48728387128", "jan_ovo@wp.pl", "andrzej janka", "narzędzia"),
            new_number="167",
        )
    assert result == {"written": False, "reason": "row_changed", "verified": False}
