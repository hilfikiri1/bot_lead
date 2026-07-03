"""Tests for unreviewed leads, spreadsheet matching and product titles."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.google_sheets_service import SpreadsheetRow, clear_cache, get_rows
from app.services.kommo_service import lead_has_internal_id
from app.services.lead_matching_service import (
    MatchCandidate,
    match_lead_to_rows,
    normalize_email,
    normalize_phone,
)
from app.services import product_title_service, unreviewed_leads_service


def _row(
    *,
    row_number: int = 2,
    phone: str | None = "+48 724 455 517",
    email: str | None = None,
    client_name: str | None = "Roman",
    company: str | None = None,
    product: str | None = "zabawki na prezent",
    lead_number: str | None = "110",
) -> SpreadsheetRow:
    return SpreadsheetRow(
        row_number=row_number,
        phone=phone,
        email=email,
        client_name=client_name,
        company=company,
        product=product,
        lead_number=lead_number,
    )


class TestPhoneNormalization:
    def test_polish_with_country_code(self):
        assert normalize_phone("+48 724 455 517") == "724455517"

    def test_polish_without_country_code(self):
        assert normalize_phone("724-455-517") == "724455517"

    def test_digits_only(self):
        assert normalize_phone("724455517") == "724455517"

    def test_too_short_returns_none(self):
        assert normalize_phone("12345") is None


class TestLeadMatching:
    def test_exact_email_match(self):
        rows = [_row(email="roman@exdor.eu", phone=None)]
        result = match_lead_to_rows(
            phones=[],
            emails=["roman@exdor.eu"],
            contact_name=None,
            company=None,
            product_hint=None,
            rows=rows,
        )
        assert len(result.candidates) == 1
        assert result.candidates[0].matched_by == "email"

    def test_exact_name_match(self):
        rows = [_row(client_name="Roman Exdor", phone=None)]
        result = match_lead_to_rows(
            phones=[],
            emails=[],
            contact_name="Roman Exdor",
            company=None,
            product_hint=None,
            rows=rows,
        )
        assert len(result.candidates) == 1
        assert result.candidates[0].matched_by == "name"

    def test_multiple_candidates(self):
        rows = [
            _row(row_number=2, phone="+48724455517", lead_number="110"),
            _row(row_number=3, phone="+48724455517", lead_number="111"),
        ]
        result = match_lead_to_rows(
            phones=["+48 724 455 517"],
            emails=[],
            contact_name=None,
            company=None,
            product_hint=None,
            rows=rows,
        )
        assert len(result.candidates) == 2

    def test_no_match(self):
        rows = [_row(phone="+48111222333")]
        result = match_lead_to_rows(
            phones=["+48999888777"],
            emails=[],
            contact_name="Other",
            company=None,
            product_hint=None,
            rows=rows,
        )
        assert result.is_empty

    def test_skip_empty_lead_number(self):
        rows = [_row(lead_number="", product="zabawki")]
        result = match_lead_to_rows(
            phones=["+48724455517"],
            emails=[],
            contact_name=None,
            company=None,
            product_hint=None,
            rows=rows,
        )
        assert result.is_empty

    def test_skip_empty_product(self):
        rows = [_row(product="", lead_number="110")]
        result = match_lead_to_rows(
            phones=["+48724455517"],
            emails=[],
            contact_name=None,
            company=None,
            product_hint=None,
            rows=rows,
        )
        assert result.is_empty


class TestProductTitle:
    @pytest.mark.asyncio
    async def test_deterministic_zabawki(self):
        title = await product_title_service.short_product_title("zabawki na prezent")
        assert title == "Игрушки"

    @pytest.mark.asyncio
    async def test_deterministic_minikoparki(self):
        title = await product_title_service.short_product_title("minikoparki 5-8 sztuk")
        assert title == "Мини-экскаваторы"

    def test_build_proposed_name(self):
        name = unreviewed_leads_service.build_proposed_name("110", "Игрушки")
        assert name == "110 - Игрушки"


class TestInternalLeadPattern:
    def test_unreviewed_names(self):
        assert not lead_has_internal_id("Facebook №44019099")
        assert not lead_has_internal_id("Новый лид")

    def test_reviewed_names(self):
        assert lead_has_internal_id("110 - Игрушки")
        assert lead_has_internal_id("90 - Мини-экскаваторы")

    def test_parse_internal_number(self):
        number, product = unreviewed_leads_service.parse_internal_lead_name(
            "109 - Пеллеты"
        )
        assert number == "109"
        assert product == "Пеллеты"


class TestUnreviewedRenameLogic:
    @pytest.mark.asyncio
    async def test_already_renamed_skips_update(self):
        preview = {
            "proposed_name": "110 - Игрушки",
            "spreadsheet_lead_number": "110",
            "short_product_ru": "Игрушки",
            "original_product": "zabawki",
            "spreadsheet_row_number": 2,
            "matched_by": "phone",
        }
        db = AsyncMock()
        with patch(
            "app.services.unreviewed_leads_service.kommo_service.update_kommo_lead",
            new_callable=AsyncMock,
        ) as mock_update:
            result = await unreviewed_leads_service.apply_lead_rename(
                db,
                lead_id=10373783,
                current_name="110 - Игрушки",
                preview=preview,
                telegram_user_id=1,
            )
        mock_update.assert_not_called()
        assert result["skipped"] is True

    @pytest.mark.asyncio
    async def test_different_internal_number_requires_replace(self):
        preview = {"proposed_name": "110 - Игрушки", "spreadsheet_lead_number": "110"}
        db = AsyncMock()
        with pytest.raises(ValueError, match="replace_required"):
            await unreviewed_leads_service.apply_lead_rename(
                db,
                lead_id=1,
                current_name="109 - Пеллеты",
                preview=preview,
                telegram_user_id=1,
                allow_replace=False,
            )

    @pytest.mark.asyncio
    async def test_successful_kommo_update(self):
        preview = {
            "proposed_name": "110 - Игрушки",
            "spreadsheet_lead_number": "110",
            "short_product_ru": "Игрушки",
            "original_product": "zabawki na prezent",
            "spreadsheet_row_number": 2,
            "matched_by": "phone",
            "matched_value_hash": "abc",
        }
        db = AsyncMock()
        with patch(
            "app.services.unreviewed_leads_service.kommo_service.update_kommo_lead",
            new_callable=AsyncMock,
            return_value={
                "lead_id": 10373783,
                "lead_name": "110 - Игрушки",
                "url": "https://example.kommo.com/leads/detail/10373783",
            },
        ) as mock_update, patch(
            "app.services.unreviewed_leads_service.crm_service.save_spreadsheet_lead_mapping",
            new_callable=AsyncMock,
        ) as mock_audit:
            result = await unreviewed_leads_service.apply_lead_rename(
                db,
                lead_id=10373783,
                current_name="Facebook №44019099",
                preview=preview,
                telegram_user_id=42,
            )
        mock_update.assert_awaited_once_with(10373783, name="110 - Игрушки")
        mock_audit.assert_awaited_once()
        assert result["skipped"] is False
        assert result["internal_number"] == "110"

    @pytest.mark.asyncio
    async def test_kommo_api_failure(self):
        from app.services.kommo_service import KommoAPIError

        preview = {
            "proposed_name": "110 - Игрушки",
            "spreadsheet_lead_number": "110",
            "short_product_ru": "Игрушки",
            "original_product": "zabawki",
            "spreadsheet_row_number": 2,
            "matched_by": "phone",
        }
        db = AsyncMock()
        with patch(
            "app.services.unreviewed_leads_service.kommo_service.update_kommo_lead",
            new_callable=AsyncMock,
            side_effect=KommoAPIError("Ошибка Kommo HTTP 500.", status_code=500),
        ):
            with pytest.raises(KommoAPIError):
                await unreviewed_leads_service.apply_lead_rename(
                    db,
                    lead_id=1,
                    current_name="Facebook",
                    preview=preview,
                    telegram_user_id=1,
                )


class TestKommoUnreviewedPagination:
    @pytest.mark.asyncio
    async def test_pagination_page_size(self):
        leads = [
            {"id": index, "name": f"Lead {index}", "status_id": 1, "updated_at": index}
            for index in range(1, 12)
        ]
        with patch(
            "app.services.kommo_service.get_all_unreviewed_leads",
            new_callable=AsyncMock,
            return_value={"leads": leads, "open_count": 11},
        ), patch(
            "app.services.kommo_service.enrich_leads_with_contacts",
            new_callable=AsyncMock,
            side_effect=lambda items: items,
        ):
            from app.services.kommo_service import get_unreviewed_leads_page

            page_one = await get_unreviewed_leads_page(page=1, page_size=8)
            page_two = await get_unreviewed_leads_page(page=2, page_size=8)
        assert len(page_one["leads"]) == 8
        assert len(page_two["leads"]) == 3
        assert page_one["total_pages"] == 2


class TestGoogleSheetsCache:
    def test_cache_refresh(self):
        from app.services import google_sheets_service as sheets

        clear_cache()
        mock_result = MagicMock()
        mock_result.execute.return_value = {
            "values": [
                [],
                [
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "724455517",
                    "zabawki",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "110",
                ],
            ]
        }
        mock_service = MagicMock()
        mock_service.spreadsheets.return_value.values.return_value.get.return_value = (
            mock_result
        )
        with patch.object(
            sheets.settings, "google_sheets_spreadsheet_id", "sheet-id"
        ), patch.object(
            sheets.settings, "google_sheets_worksheet_name", "Sheet1"
        ), patch.object(
            sheets.settings, "google_sheets_service_account_json", '{"client_email":"x"}'
        ), patch.object(
            sheets.settings, "google_sheets_cache_ttl_seconds", 300
        ), patch(
            "app.services.google_sheets_service._sheets_service",
            return_value=mock_service,
        ):
            first = get_rows()
            second = get_rows()
            third = get_rows(force_refresh=True)
        assert len(first) == 1
        assert len(second) == 1
        assert len(third) == 1
        assert (
            mock_service.spreadsheets.return_value.values.return_value.get.call_count
            == 2
        )


class TestIncomingLeadsFilter:
    @pytest.mark.asyncio
    async def test_filters_only_incoming_stage_without_internal_id(self):
        from app.services.kommo_service import get_all_unreviewed_leads

        with patch(
            "app.services.kommo_service.get_pipeline_index",
            new_callable=AsyncMock,
            return_value=(
                {1: "Sales"},
                {(1, 10): "Incoming leads", (1, 20): "Negotiation"},
            ),
        ), patch(
            "app.services.kommo_service.get_all_open_leads",
            new_callable=AsyncMock,
            return_value={
                "leads": [
                    {"id": 1, "name": "Facebook lead", "status_id": 10},
                    {"id": 2, "name": "110 - Игрушки", "status_id": 10},
                    {"id": 3, "name": "Other stage", "status_id": 20},
                ],
                "open_count": 3,
            },
        ), patch(
            "app.services.kommo_service.settings.kommo_unreviewed_status_id",
            None,
        ), patch(
            "app.services.kommo_service.settings.kommo_unreviewed_pipeline_id",
            None,
        ):
            result = await get_all_unreviewed_leads()

        assert [lead["id"] for lead in result["leads"]] == [1]
        assert result["status_label"] == "Sales → Incoming leads"

    @pytest.mark.asyncio
    async def test_explicit_status_id_override(self):
        from app.services.kommo_service import get_all_unreviewed_leads

        with patch(
            "app.services.kommo_service.get_all_open_leads",
            new_callable=AsyncMock,
            return_value={
                "leads": [
                    {"id": 1, "name": "Facebook lead", "status_id": 99},
                    {"id": 2, "name": "Other", "status_id": 10},
                ],
                "open_count": 2,
            },
        ), patch(
            "app.services.kommo_service.settings.kommo_unreviewed_status_id",
            99,
        ), patch(
            "app.services.kommo_service.settings.kommo_unreviewed_pipeline_id",
            None,
        ):
            result = await get_all_unreviewed_leads()

        assert [lead["id"] for lead in result["leads"]] == [1]


class TestGoogleSheetsCredentials:
    def test_uses_calendar_service_account_when_sheets_json_missing(self):
        from app.services import google_sheets_service as sheets

        with patch.object(
            sheets.settings, "google_sheets_service_account_json", ""
        ), patch.object(
            sheets.settings,
            "google_service_account_json",
            '{"client_email":"calendar@test","private_key":"x"}',
        ), patch.object(
            sheets.settings, "google_service_account_json_base64", ""
        ):
            info = sheets._load_service_account_info()
        assert info["client_email"] == "calendar@test"


class TestTelegramAuthorization:
    def test_unauthorized_user_blocked(self):
        from app.api import telegram as telegram_api

        with patch.object(
            telegram_api.settings, "allowed_telegram_user_ids", "111"
        ):
            assert telegram_api._is_allowed_user(111) is True
            assert telegram_api._is_allowed_user(222) is False
