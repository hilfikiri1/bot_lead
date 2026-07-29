from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.models.supplier_workspace import ProjectSupplier, SupplierInquiry, SupplierOffer
from app.services import supplier_workspace_runtime, supplier_workspace_service


def _supplier(
    supplier_id: int,
    name: str,
    *,
    lead_id: int = 135,
    status: str = "candidate",
) -> SimpleNamespace:
    return SimpleNamespace(
        id=supplier_id,
        kommo_lead_id=lead_id,
        internal_lead_number="135",
        name=name,
        company_name=None,
        platform="1688",
        source_url="https://example.1688.com",
        contact_value="WeChat demo",
        status=status,
        verification_status="not_checked",
        verification_summary=None,
        last_contact_at=None,
        next_followup_at=None,
        notes=None,
    )


def _offer(
    offer_id: int,
    supplier_id: int,
    *,
    currency: str = "USD",
    incoterm: str = "FOB",
    named_place: str = "Qingdao",
    total_price: str | None = None,
    unit_price: str | None = None,
    lead_time_days: int | None = None,
    warranty_months: int | None = None,
    certifications: list[str] | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=offer_id,
        supplier_id=supplier_id,
        kommo_lead_id=135,
        currency=currency,
        incoterm=incoterm,
        named_place=named_place,
        total_price=Decimal(total_price) if total_price else None,
        unit_price=Decimal(unit_price) if unit_price else None,
        moq="1 set",
        lead_time_days=lead_time_days,
        warranty_months=warranty_months,
        payment_terms="30/70",
        certifications_json=certifications or [],
        deviations_json=[],
        received_at=datetime.now(timezone.utc),
    )


def test_supplier_line_parses_1688_link_contact_and_notes():
    result = supplier_workspace_service.parse_supplier_line(
        "Shandong Poultry | https://shop.1688.com/page/index.html | WeChat: demo | сильный каталог"
    )
    assert result["name"] == "Shandong Poultry"
    assert result["platform"] == "1688"
    assert result["contact_value"] == "WeChat: demo"
    assert result["notes"] == "сильный каталог"


def test_supplier_line_without_url_treats_second_field_as_contact():
    result = supplier_workspace_service.parse_supplier_line(
        "Henan Farm Systems | WeChat: supplier88 | менеджер Li"
    )
    assert result["source_url"] is None
    assert "WeChat" in str(result["contact_value"])


def test_offer_line_normalizes_commercial_terms():
    result = supplier_workspace_service.parse_offer_line(
        "USD | FOB Qingdao | 17900 | 17900 | 1 set | 50 days | 18 months | 30/70 | CE, ISO | engine model pending"
    )
    assert result["currency"] == "USD"
    assert result["incoterm"] == "FOB"
    assert result["named_place"] == "Qingdao"
    assert result["unit_price"] == Decimal("17900")
    assert result["total_price"] == Decimal("17900")
    assert result["lead_time_days"] == 50
    assert result["warranty_months"] == 18
    assert result["certifications_json"] == ["CE", "ISO"]
    assert "engine" in str(result["notes"])


def test_offer_line_rejects_incomplete_commercial_data():
    with pytest.raises(ValueError, match="минимум"):
        supplier_workspace_service.parse_offer_line("USD | FOB")


def test_comparison_ranks_only_same_currency_incoterm_and_place():
    supplier_a = _supplier(1, "Factory A")
    supplier_b = _supplier(2, "Factory B")
    records = [
        (
            _offer(
                11,
                1,
                total_price="18500",
                lead_time_days=35,
                warranty_months=12,
                certifications=["CE"],
            ),
            supplier_a,
        ),
        (
            _offer(
                12,
                2,
                total_price="17900",
                lead_time_days=50,
                warranty_months=18,
                certifications=["CE", "ISO"],
            ),
            supplier_b,
        ),
    ]
    comparison = supplier_workspace_service.compare_offer_records(
        lead_id=135, records=records
    )
    assert len(comparison.comparable_groups) == 1
    assert any("Factory B" in conclusion and "17900" in conclusion for conclusion in comparison.conclusions)
    assert any("Factory A" in conclusion and "35" in conclusion for conclusion in comparison.conclusions)
    assert not any("глобально" in warning for warning in comparison.warnings)


def test_comparison_warns_instead_of_ranking_mixed_terms():
    records = [
        (
            _offer(11, 1, currency="USD", incoterm="FOB", total_price="18000"),
            _supplier(1, "Factory USD"),
        ),
        (
            _offer(
                12,
                2,
                currency="CNY",
                incoterm="EXW",
                named_place="Foshan",
                total_price="120000",
            ),
            _supplier(2, "Factory CNY"),
        ),
    ]
    comparison = supplier_workspace_service.compare_offer_records(
        lead_id=135, records=records
    )
    assert len(comparison.comparable_groups) == 2
    assert any("глобально ранжировать цену нельзя" in warning for warning in comparison.warnings)
    assert not comparison.conclusions


def test_missing_certificates_are_visible_in_comparison():
    comparison = supplier_workspace_service.compare_offer_records(
        lead_id=135,
        records=[
            (
                _offer(11, 1, total_price="18000", certifications=[]),
                _supplier(1, "Factory A"),
            )
        ],
    )
    assert any("сертификаты" in warning.casefold() for warning in comparison.warnings)
    text = supplier_workspace_service.format_comparison(
        comparison, lead_name="135 - Кормушки"
    )
    assert "не подтверждены" in text


def test_workspace_buttons_fit_telegram_callback_limit():
    markup = supplier_workspace_runtime._workspace_markup(
        lead_id=15011973,
        suppliers=[_supplier(123456789, "A very long factory name " * 5)],
        lead_url="https://example.kommo.com/leads/detail/15011973",
    )
    callbacks = [
        button["callback_data"]
        for row in markup["inline_keyboard"]
        for button in row
        if "callback_data" in button
    ]
    assert callbacks
    assert all(len(value.encode("utf-8")) <= 64 for value in callbacks)


@pytest.mark.asyncio
async def test_create_supplier_is_idempotent_for_same_lead_and_identity():
    existing = _supplier(1, "Factory A")
    result = MagicMock()
    result.scalar_one_or_none.return_value = existing
    db = AsyncMock()
    db.execute = AsyncMock(return_value=result)

    created = await supplier_workspace_service.create_supplier(
        db,
        kommo_lead_id=135,
        internal_lead_number="135",
        name="Factory A",
        source_url="https://example.1688.com",
        telegram_user_id=100,
    )

    assert created is existing
    db.add.assert_not_called()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_record_inquiry_sets_supplier_status_and_followup():
    supplier = _supplier(1, "Factory A")
    db = AsyncMock()
    db.add = MagicMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()

    original_get = supplier_workspace_service.get_supplier
    supplier_workspace_service.get_supplier = AsyncMock(return_value=supplier)
    try:
        inquiry = await supplier_workspace_service.record_inquiry_sent(
            db,
            supplier_id=1,
            telegram_user_id=100,
            body="Please quote.",
            channel="WeChat",
            followup_days=3,
        )
    finally:
        supplier_workspace_service.get_supplier = original_get

    assert isinstance(inquiry, SupplierInquiry)
    assert inquiry.status == "sent"
    assert inquiry.due_at > datetime.now(timezone.utc) + timedelta(days=2)
    assert supplier.status == "inquiry_sent"
    assert supplier.next_followup_at == inquiry.due_at
    db.add.assert_called_once_with(inquiry)


def test_models_are_registered_with_expected_tables():
    assert ProjectSupplier.__tablename__ == "project_suppliers"
    assert SupplierInquiry.__tablename__ == "supplier_inquiries"
    assert SupplierOffer.__tablename__ == "supplier_offers"
