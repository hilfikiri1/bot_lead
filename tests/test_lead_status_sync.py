"""Tests for the guarded Kommo -> Google Sheets status reconciliation."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services import google_sheets_service, kommo_service, lead_status_sync_service
from app.services.google_sheets_service import SpreadsheetRow


def _row(
    number: str,
    status: str,
    *,
    row_number: int,
    product: str = "produkt",
) -> SpreadsheetRow:
    return SpreadsheetRow(
        row_number=row_number,
        phone=None,
        email=None,
        client_name=None,
        company=None,
        product=product,
        lead_number=number,
        lead_status=status,
    )


def test_parse_internal_number():
    assert lead_status_sync_service.parse_internal_number("110 - Игрушки") == "110"
    assert lead_status_sync_service.parse_internal_number(" 7- Maszyna ") == "7"
    assert lead_status_sync_service.parse_internal_number("Facebook №123") is None


@pytest.mark.asyncio
async def test_report_classifies_updates_missing_and_duplicates():
    rows = [
        _row("110", "MQL", row_number=2),
        _row("111", "SQL", row_number=3),
        _row("112", "Первый контакт", row_number=4),
        _row("113", "MQL", row_number=5),
        _row("113", "MQL", row_number=6),
    ]
    kommo_result = {
        "leads": [
            {"id": 10, "name": "110 - Игрушки", "status_name": "SQL"},
            {"id": 11, "name": "111 - Пилы", "status_name": "SQL"},
            {"id": 14, "name": "114 - Лазер", "status_name": "MQL"},
            {"id": 15, "name": "115 - A", "status_name": "MQL"},
            {"id": 16, "name": "115 - B", "status_name": "SQL"},
            {"id": 17, "name": "Facebook №17", "status_name": "Новый лид"},
        ],
        "truncated": False,
        "pipeline_id": 1,
        "pipeline_name": "Польша",
    }
    with patch(
        "app.services.lead_status_sync_service.google_sheets_service.get_rows",
        return_value=rows,
    ), patch(
        "app.services.lead_status_sync_service.kommo_service.get_all_leads_for_status_sync",
        new_callable=AsyncMock,
        return_value=kommo_result,
    ):
        report = await lead_status_sync_service.build_status_sync_report()

    assert report["matching_count"] == 1
    assert report["updates_count"] == 1
    assert report["updates"][0]["lead_number"] == "110"
    assert report["updates"][0]["old_status"] == "MQL"
    assert report["updates"][0]["new_status"] == "SQL"
    assert [item["lead_number"] for item in report["table_only"]] == ["112"]
    assert [item["lead_number"] for item in report["kommo_only"]] == ["114"]
    assert report["table_duplicates"][0]["lead_number"] == "113"
    assert report["kommo_duplicates"][0]["lead_number"] == "115"
    assert report["unnumbered_kommo_count"] == 1


@pytest.mark.asyncio
async def test_confirmed_report_rejects_stale_preview():
    fresh = {
        "updates_digest": "fresh",
        "updates_count": 1,
        "updates": [{"lead_number": "110"}],
    }
    with patch(
        "app.services.lead_status_sync_service.build_status_sync_report",
        new_callable=AsyncMock,
        return_value=fresh,
    ), patch(
        "app.services.lead_status_sync_service.google_sheets_service.apply_status_updates"
    ) as apply_mock:
        result = await lead_status_sync_service.apply_confirmed_report(
            expected_digest="old",
            expected_updates_count=1,
        )

    assert result["stale"] is True
    apply_mock.assert_not_called()


def test_sheet_write_is_disabled_by_default():
    with patch.object(
        google_sheets_service.settings, "google_sheets_write_enabled", False
    ):
        with pytest.raises(google_sheets_service.GoogleSheetsError, match="отключена"):
            google_sheets_service.apply_status_updates(
                [
                    {
                        "lead_number": "110",
                        "row_number": 2,
                        "old_status": "MQL",
                        "new_status": "SQL",
                    }
                ]
            )


def test_sheet_write_rechecks_old_status_and_row():
    rows = [
        _row("110", "MQL", row_number=2),
        _row("111", "Изменено вручную", row_number=3),
    ]
    mock_service = MagicMock()
    with patch.object(
        google_sheets_service.settings, "google_sheets_write_enabled", True
    ), patch.object(
        google_sheets_service.settings, "google_sheets_spreadsheet_id", "sheet-id"
    ), patch.object(
        google_sheets_service.settings, "google_sheets_worksheet_name", "FB"
    ), patch.object(
        google_sheets_service.settings, "google_sheets_status_column", "W"
    ), patch(
        "app.services.google_sheets_service.is_configured", return_value=True
    ), patch(
        "app.services.google_sheets_service.get_rows", return_value=rows
    ), patch(
        "app.services.google_sheets_service._sheets_service",
        return_value=mock_service,
    ), patch(
        "app.services.google_sheets_service.clear_cache"
    ):
        result = google_sheets_service.apply_status_updates(
            [
                {
                    "lead_number": "110",
                    "row_number": 2,
                    "old_status": "MQL",
                    "new_status": "SQL",
                },
                {
                    "lead_number": "111",
                    "row_number": 3,
                    "old_status": "SQL",
                    "new_status": "Закрыто",
                },
            ]
        )

    assert result["updated_count"] == 1
    assert result["skipped"] == [
        {"lead_number": "111", "reason": "status_changed_manually"}
    ]
    call = (
        mock_service.spreadsheets.return_value.values.return_value.batchUpdate.call_args
    )
    assert call.kwargs["body"]["data"] == [
        {
            "range": "'FB'!W2",
            "majorDimension": "ROWS",
            "values": [["SQL"]],
        }
    ]


@pytest.mark.asyncio
async def test_kommo_status_sync_includes_closed_leads_and_paginates():
    first_page = {
        "_embedded": {
            "leads": [
                {
                    "id": 1,
                    "name": "1 - Open",
                    "pipeline_id": 5,
                    "status_id": 10,
                    "closed_at": None,
                },
                {
                    "id": 2,
                    "name": "2 - Closed",
                    "pipeline_id": 5,
                    "status_id": 20,
                    "closed_at": 123,
                },
            ]
        }
    }
    second_page = {
        "_embedded": {
            "leads": [
                {
                    "id": 3,
                    "name": "3 - Won",
                    "pipeline_id": 5,
                    "status_id": 30,
                    "closed_at": 456,
                }
            ]
        }
    }
    request_mock = AsyncMock(side_effect=[first_page, second_page])
    with patch(
        "app.services.kommo_service._request", request_mock
    ), patch(
        "app.services.kommo_service.get_pipeline_index",
        new_callable=AsyncMock,
        return_value=(
            {5: "Польша"},
            {(5, 10): "MQL", (5, 20): "Закрыто", (5, 30): "Успешно"},
        ),
    ), patch.object(
        kommo_service, "PAGE_SIZE", 2
    ), patch.object(
        kommo_service.settings, "lead_status_sync_pipeline_id", 5
    ):
        result = await kommo_service.get_all_leads_for_status_sync(max_pages=5)

    assert result["count"] == 3
    assert {lead["status_name"] for lead in result["leads"]} == {
        "MQL",
        "Закрыто",
        "Успешно",
    }
    assert any(lead["closed_at"] for lead in result["leads"])
    assert request_mock.await_count == 2
