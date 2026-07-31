"""Step 1: detecting new Facebook leads and capturing their permanent identity."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.services.lead_intake import detection


def test_recognizes_hash_and_numero_sign_titles():
    assert detection.is_new_facebook_lead_title("Facebook #12312412")
    assert detection.is_new_facebook_lead_title("Facebook №1479023253985582")
    assert detection.is_new_facebook_lead_title("facebook #99")
    assert not detection.is_new_facebook_lead_title("167 - Инструменты")
    assert not detection.is_new_facebook_lead_title(None)


def test_extract_facebook_lead_id_prefers_metadata():
    fb_id = detection.extract_facebook_lead_id(
        metadata={"lead_id": "998877"}, custom_fields=[], title="Facebook #12312412"
    )
    assert fb_id == "998877"


def test_extract_facebook_lead_id_falls_back_to_custom_field():
    fb_id = detection.extract_facebook_lead_id(
        metadata={},
        custom_fields=[{"name": "Facebook Lead ID", "code": "", "value": "555111"}],
        title="Facebook #12312412",
    )
    assert fb_id == "555111"


def test_extract_facebook_lead_id_falls_back_to_title():
    fb_id = detection.extract_facebook_lead_id(metadata={}, custom_fields=[], title="Facebook #12312412")
    assert fb_id == "12312412"

    fb_id2 = detection.extract_facebook_lead_id(
        metadata={}, custom_fields=[], title="Facebook №1479023253985582"
    )
    assert fb_id2 == "1479023253985582"


@pytest.mark.asyncio
async def test_find_candidate_leads_filters_by_title_and_sorts_newest_first():
    unreviewed = {
        "leads": [
            {"id": 1, "name": "Facebook #2", "created_at": 200, "pipeline_id": 13866843},
            {"id": 2, "name": "167 - Инструменты", "created_at": 50, "pipeline_id": 13866843},
            {"id": 3, "name": "Facebook №1", "created_at": 100, "pipeline_id": 13866843},
        ]
    }
    with (
        patch.object(detection.settings, "kommo_poland_pipeline_id", None),
        patch.object(detection.settings, "kommo_unreviewed_pipeline_id", None),
        patch(
            "app.services.lead_intake.detection.kommo_service.get_all_unreviewed_leads",
            new=AsyncMock(return_value=unreviewed),
        ),
    ):
        candidates = await detection.find_candidate_leads()
    assert [item["id"] for item in candidates] == [1, 3]


@pytest.mark.asyncio
async def test_find_candidate_leads_uses_poland_pipeline_and_skips_ukraine():
    poland = {
        "leads": [
            {"id": 10, "name": "Facebook #pl", "created_at": 300, "pipeline_id": 13866843},
        ]
    }
    with (
        patch.object(detection.settings, "kommo_poland_pipeline_id", 13866843),
        patch(
            "app.services.lead_intake.detection.kommo_service.get_all_unsorted_leads",
            new=AsyncMock(return_value=poland),
        ) as unsorted_mock,
        patch(
            "app.services.lead_intake.detection.kommo_service.get_all_unreviewed_leads",
            new=AsyncMock(),
        ) as unreviewed_mock,
    ):
        candidates = await detection.find_candidate_leads()
    unsorted_mock.assert_awaited_once_with(pipeline_id=13866843)
    unreviewed_mock.assert_not_awaited()
    assert [item["id"] for item in candidates] == [10]


@pytest.mark.asyncio
async def test_build_snapshot_extracts_contact_and_custom_fields():
    details = {
        "id": 555,
        "name": "Facebook #12312412",
        "created_at": 1234567890,
        "custom_fields": [
            {"name": "Budżet", "code": "", "value": "$5_000_-_$10_000"},
            {"name": "Region", "code": "", "value": "kujawsko pomorskie"},
            {"name": "Preferowany kontakt", "code": "", "value": "whats_app"},
            {"name": "Produkt", "code": "", "value": "narzędzia"},
        ],
        "contacts": [
            {
                "id": 1,
                "name": "Andrzej Janka",
                "phones": ["728387128"],
                "emails": ["jan_ovo@wp.pl"],
                "custom_fields": [],
            }
        ],
    }
    with patch(
        "app.services.lead_intake.detection.kommo_service.get_lead_details",
        new=AsyncMock(return_value=details),
    ):
        raw, snapshot = await detection.build_snapshot(
            555, unsorted_metadata={"lead_id": "12312412"}, unsorted_source_name="Facebook Lead Ads"
        )

    assert snapshot.facebook_lead_id == "12312412"
    assert snapshot.email == "jan_ovo@wp.pl"
    assert snapshot.name == "Andrzej Janka"
    assert snapshot.product == "narzędzia"
    assert snapshot.region == "kujawsko pomorskie"
    assert raw["budget"] == "$5_000_-_$10_000"
    assert raw["contact_channel"] == "whats_app"
    assert raw["source"] == "facebook_lead_ads"
