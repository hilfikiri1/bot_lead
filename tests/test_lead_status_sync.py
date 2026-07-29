"""Tests for guarded Kommo <-> Google Sheets lead registry synchronization."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services import google_sheets_service, kommo_service, lead_status_sync_service
from app.services.google_sheets_service import SpreadsheetRow


def _row(
    number: str = "",
    status: str = "",
    *,
    row_number: int,
    product: str = "produkt",
    phone: str | None = None,
    email: str | None = None,
    client_name: str | None = "Klient",
    comment: str | None = None,
    budget: str | None = "$5_000_–_$10_000",
    channel: str | None = "whats_app",
    region: str | None = "Polska",
) -> SpreadsheetRow:
    return SpreadsheetRow(
        row_number=row_number,
        phone=phone,
        email=email,
        client_name=client_name,
        company=None,
        product=product,
        lead_number=number,
        lead_status=status,
        marketing_comment=comment,
        budget=budget,
        contact_channel=channel,
        region=region,
    )


def test_parse_internal_number_accepts_dash_and_historical_space_format():
    assert lead_status_sync_service.parse_internal_number("110 - Игрушки") == "110"
    assert lead_status_sync_service.parse_internal_number(" 7– Maszyna ") == "7"
    assert lead_status_sync_service.parse_internal_number("68 Пилы") == "68"
    assert lead_status_sync_service.parse_internal_number("Facebook №123") is None


@pytest.mark.asyncio
async def test_report_assigns_next_number_and_preserves_existing_x_and_w():
    rows = [
        _row(
            "165",
            "Первый контакт",
            row_number=165,
            product="magazyn energii",
            phone="504051504",
        ),
        _row(
            "",
            "SQL",
            row_number=166,
            product="herbaty ryż mango",
            phone="698 136 090",
            client_name="Przemek Bryłka",
            comment="Существующий комментарий X",
        ),
    ]
    kommo_result = {
        "leads": [
            {
                "id": 10,
                "name": "165 - Накопители энергии",
                "status_name": "Первый контакт",
            },
            {
                "id": 11,
                "name": "Facebook lead",
                "status_name": "Получен ответ",
                "contact_id": 22,
            },
            {
                "id": 12,
                "name": "170 - Пилы",
                "status_name": "Получено ТЗ",
            },
        ],
        "truncated": False,
        "pipeline_id": 1,
        "pipeline_name": "Польша",
    }
    enriched = {
        **kommo_result["leads"][1],
        "phones": ["+48 698 136 090"],
        "emails": [],
        "contact_name": "Przemek Bryłka",
    }
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
            return_value=[enriched],
        ),
        patch(
            "app.services.lead_status_sync_service.product_title_service.short_product_title",
            new_callable=AsyncMock,
            return_value="Чай",
        ),
    ):
        report = await lead_status_sync_service.build_status_sync_report()

    assert report["marketing_status_preserved"] is True
    assert report["comment_updates_count"] == 0
    assert report["number_assignments_count"] == 1
    assert report["updates_count"] == 1

    new_row = report["sheet_updates"][0]
    assert new_row["row_number"] == 166
    assert new_row["old_lead_number"] == ""
    assert new_row["new_lead_number"] == "171"
    assert new_row["marketing_status"] == "SQL"
    assert new_row["old_comment"] == "Существующий комментарий X"
    assert new_row["new_comment"] == "Существующий комментарий X"
    assert "new_status" not in new_row
    assert report["kommo_renames"][0]["new_name"] == "171 - Чай"
    assert report["onboarding_actions"][0]["lead_number"] == "171"


@pytest.mark.asyncio
async def test_confirmed_report_rejects_stale_preview():
    fresh = {
        "updates_digest": "fresh",
        "updates_count": 1,
        "sheet_updates": [{"row_number": 166}],
        "onboarding_actions": [],
    }
    with (
        patch(
            "app.services.lead_status_sync_service.build_status_sync_report",
            new_callable=AsyncMock,
            return_value=fresh,
        ),
        patch(
            "app.services.lead_status_sync_service.google_sheets_service.apply_lead_registry_updates"
        ) as apply_mock,
    ):
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
            google_sheets_service.apply_lead_registry_updates(
                [
                    {
                        "row_number": 166,
                        "old_lead_number": "",
                        "new_lead_number": "166",
                        "old_comment": "",
                        "new_comment": "",
                    }
                ]
            )


def test_manual_onboarding_sheet_write_updates_only_y_and_preserves_x_w():
    rows = [
        _row(
            "",
            "SQL",
            row_number=166,
            phone="698136090",
            comment="Существующий X",
        )
    ]
    mock_service = MagicMock()
    with (
        patch.object(
            google_sheets_service.settings, "google_sheets_write_enabled", True
        ),
        patch.object(
            google_sheets_service.settings,
            "google_sheets_spreadsheet_id",
            "sheet-id",
        ),
        patch.object(
            google_sheets_service.settings, "google_sheets_worksheet_name", "FB"
        ),
        patch.object(
            google_sheets_service.settings, "google_sheets_comment_column", "X"
        ),
        patch.object(
            google_sheets_service.settings, "google_sheets_lead_number_column", "Y"
        ),
        patch(
            "app.services.google_sheets_service.is_configured", return_value=True
        ),
        patch("app.services.google_sheets_service.get_rows", return_value=rows),
        patch(
            "app.services.google_sheets_service._sheets_service",
            return_value=mock_service,
        ),
        patch("app.services.google_sheets_service.clear_cache"),
    ):
        result = google_sheets_service.apply_lead_registry_updates(
            [
                {
                    "row_number": 166,
                    "row_fingerprint": [
                        "698136090",
                        "",
                        "klient",
                        "produkt",
                    ],
                    "old_lead_number": "",
                    "new_lead_number": "166",
                    "old_comment": "Существующий X",
                    "new_comment": "Существующий X",
                }
            ]
        )

    assert result["updated_count"] == 1
    assert result["updated_cells_count"] == 1
    call = (
        mock_service.spreadsheets.return_value.values.return_value.batchUpdate.call_args
    )
    data = call.kwargs["body"]["data"]
    assert {item["range"] for item in data} == {"'FB'!Y166"}
    assert all("X166" not in item["range"] for item in data)
    assert all("W166" not in item["range"] for item in data)


def test_sheet_write_rechecks_manual_comment():
    rows = [_row("166", "SQL", row_number=166, comment="Изменено вручную")]
    with (
        patch.object(
            google_sheets_service.settings, "google_sheets_write_enabled", True
        ),
        patch(
            "app.services.google_sheets_service.is_configured", return_value=True
        ),
        patch("app.services.google_sheets_service.get_rows", return_value=rows),
    ):
        result = google_sheets_service.apply_lead_registry_updates(
            [
                {
                    "row_number": 166,
                    "old_lead_number": "166",
                    "new_lead_number": "166",
                    "old_comment": "Старое значение",
                    "new_comment": "Старое значение",
                }
            ]
        )
    assert result["updated_count"] == 0
    assert result["skipped"][0]["reason"] == "comment_changed_manually"


@pytest.mark.asyncio
async def test_confirmed_onboarding_writes_y_then_updates_verified_kommo_lead():
    due_at = int(time.time()) + 3600
    report = {
        "updates_digest": "same",
        "updates_count": 1,
        "sheet_updates": [{"row_number": 166, "new_lead_number": "166"}],
        "onboarding_actions": [
            {
                "kommo_lead_id": 77,
                "row_number": 166,
                "lead_number": "166",
                "old_name": "Facebook lead",
                "new_name": "166 - Чай",
                "target_status_id": None,
                "analysis_note": "[BBS-ONBOARD-166-77]\nАнализ",
                "task_text": "Позвонить клиенту по лиду №166: Чай",
                "task_due_at": due_at,
                "contact_card": {},
            }
        ],
    }
    sheet_result = {
        "updated_count": 1,
        "updated_cells_count": 1,
        "updated": [],
        "skipped": [],
    }
    with (
        patch(
            "app.services.lead_status_sync_service.build_status_sync_report",
            new_callable=AsyncMock,
            return_value=report,
        ),
        patch(
            "app.services.lead_status_sync_service.google_sheets_service.apply_lead_registry_updates",
            return_value=sheet_result,
        ) as sheet_write,
        patch(
            "app.services.lead_status_sync_service.google_sheets_service.get_rows",
            return_value=[_row("166", "SQL", row_number=166)],
        ),
        patch(
            "app.services.lead_status_sync_service.kommo_service.get_lead_details",
            new_callable=AsyncMock,
            return_value={"id": 77, "name": "Facebook lead", "status_id": 10},
        ),
        patch(
            "app.services.lead_status_sync_service.kommo_service.update_kommo_lead",
            new_callable=AsyncMock,
            return_value={"lead_id": 77, "lead_name": "166 - Чай"},
        ) as rename,
        patch(
            "app.services.lead_status_sync_service.kommo_service.get_recent_common_notes",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch(
            "app.services.lead_status_sync_service.kommo_service.add_common_note",
            new_callable=AsyncMock,
        ) as add_note,
        patch(
            "app.services.lead_status_sync_service.kommo_service.get_open_lead_tasks",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch(
            "app.services.lead_status_sync_service.kommo_service.create_lead_task",
            new_callable=AsyncMock,
        ) as create_task,
    ):
        result = await lead_status_sync_service.apply_confirmed_report(
            expected_digest="same",
            expected_updates_count=1,
        )

    sheet_write.assert_called_once()
    rename.assert_awaited_once_with(77, name="166 - Чай")
    add_note.assert_awaited_once()
    create_task.assert_awaited_once_with(
        lead_id=77,
        text="Позвонить клиенту по лиду №166: Чай",
        complete_till=due_at,
    )
    assert result["renamed_count"] == 1
    assert result["note_count"] == 1
    assert result["task_count"] == 1


@pytest.mark.asyncio
async def test_kommo_registry_sync_includes_closed_leads_contacts_and_paginates():
    first_page = {
        "_embedded": {
            "leads": [
                {
                    "id": 1,
                    "name": "1 - Open",
                    "pipeline_id": 5,
                    "status_id": 10,
                    "closed_at": None,
                    "_embedded": {"contacts": [{"id": 101}]},
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
    with (
        patch("app.services.kommo_service._request", request_mock),
        patch(
            "app.services.kommo_service.get_pipeline_index",
            new_callable=AsyncMock,
            return_value=(
                {5: "Польша"},
                {(5, 10): "MQL", (5, 20): "Закрыто", (5, 30): "Успешно"},
            ),
        ),
        patch.object(kommo_service, "PAGE_SIZE", 2),
        patch.object(kommo_service.settings, "lead_status_sync_pipeline_id", 5),
    ):
        result = await kommo_service.get_all_leads_for_status_sync(max_pages=5)

    assert result["count"] == 3
    assert result["leads"][0]["contact_id"] == 101
    assert any(lead["closed_at"] for lead in result["leads"])
    assert request_mock.await_count == 2
    assert request_mock.await_args_list[0].kwargs["params"]["with"] == "contacts"
