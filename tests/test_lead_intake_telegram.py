"""Telegram wiring: idempotent callbacks, dry-run banner, manual-match buttons."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.api import lead_intake_telegram
from app.services.lead_intake import repository, telegram_ui
from tests.lead_intake_helpers import temp_db_session


async def _create_completed_job(db, **overrides):
    job = await repository.create_job(
        db,
        kommo_lead_id=555001,
        original_title="Facebook #12312412",
        facebook_lead_id="12312412",
        facebook_technical_tag=None,
        source="facebook_lead_ads",
        raw_snapshot={"name": "Andrzej Janka", "phone": "728387128", "email": "jan_ovo@wp.pl"},
        dry_run=False,
        processing_version=1,
    )
    return await repository.save(db, job, **overrides)


@pytest.mark.asyncio
async def test_repeated_apply_callback_only_applies_once():
    async with temp_db_session() as db:
        job = await _create_completed_job(
            db,
            status="waiting_approval",
            assigned_number="167",
            sheet_row_number=167,
            ai_payload_json={
                "product_name_ru": "Инструменты",
                "potential": "medium",
                "readiness": "low",
                "priority": "C",
                "priority_label_ru": "квалификация",
                "recommended_action": "email",
                "recommended_action_reason_ru": "reason",
                "lead_analysis_ru": "analysis",
                "main_risks_ru": [],
                "missing_information_ru": [],
                "next_steps_ru": [],
                "client_message": {"language": "pl", "channel": "email", "text": "hi"},
                "call_script": None,
                "kommo_note_ru": "note",
                "task": {"type": "follow_up", "title_ru": "task", "due_rule": "next_business_day", "due_at": None},
                "second_follow_up": {"enabled": False, "days_after_first": 0, "text": None},
            },
        )

        apply_mock = AsyncMock()
        apply_mock.side_effect = [
            _fake_apply_result("completed", job),
            _fake_apply_result("already_completed", job),
        ]
        with (
            patch("app.api.lead_intake_telegram.service.apply_job", new=apply_mock),
            patch("app.api.lead_intake_telegram.telegram_service.send_message", new=AsyncMock()) as send_mock,
            patch("app.api.lead_intake_telegram.service.find_or_create_next_job", new=AsyncMock(return_value=None)),
        ):
            handled1 = await lead_intake_telegram.handle_callback(
                callback_data=f"lp:apply:{job.id}", chat_id=1, user_id=1, db=db
            )
            handled2 = await lead_intake_telegram.handle_callback(
                callback_data=f"lp:apply:{job.id}", chat_id=1, user_id=1, db=db
            )
        assert handled1 is True
        assert handled2 is True
        assert apply_mock.await_count == 2  # callback delivered twice by Telegram...
        # ...but the second call is a documented no-op ("already_completed"),
        # never a second set of Kommo/Sheets writes (verified in
        # test_lead_intake_service.py at the saga level).
        assert send_mock.await_count >= 2


def _fake_apply_result(status: str, job):
    from app.services.lead_intake.service import ApplyResult

    return ApplyResult(status=status, job=job)


@pytest.mark.asyncio
async def test_skip_callback_is_idempotent_and_advances_to_next_job():
    async with temp_db_session() as db:
        job = await _create_completed_job(db, status="waiting_approval")

        with (
            patch("app.api.lead_intake_telegram.service.find_or_create_next_job", new=AsyncMock(return_value=None)),
            patch("app.api.lead_intake_telegram.telegram_service.send_message", new=AsyncMock()) as send_mock,
        ):
            await lead_intake_telegram.handle_callback(
                callback_data=f"lp:skip:{job.id}", chat_id=1, user_id=1, db=db
            )
            await lead_intake_telegram.handle_callback(
                callback_data=f"lp:skip:{job.id}", chat_id=1, user_id=1, db=db
            )

        refreshed = await repository.get_by_id(db, job.id)
        assert refreshed.status == "skipped"
        assert send_mock.await_count >= 2


def test_dry_run_banner_shown_in_preview():
    from app.models.lead_processing_job import LeadProcessingJob
    from app.services.lead_intake.matching import LeadSnapshot
    from app.services.lead_intake.schema import LeadQualification

    job = LeadProcessingJob(
        id=1,
        kommo_lead_id=1,
        assigned_number="167",
        dry_run=True,
        status="waiting_approval",
    )
    snapshot = LeadSnapshot(facebook_lead_id="1", phone="728387128", email="a@b.pl", name="Andrzej Janka", product="narzędzia")
    qualification = LeadQualification.model_validate(
        {
            "product_name_ru": "Инструменты",
            "potential": "medium",
            "readiness": "low",
            "priority": "C",
            "priority_label_ru": "квалификация",
            "recommended_action": "whatsapp",
            "recommended_action_reason_ru": "reason",
            "lead_analysis_ru": "analysis",
            "main_risks_ru": [],
            "missing_information_ru": [],
            "next_steps_ru": [],
            "client_message": {"language": "pl", "channel": "whatsapp", "text": "hi"},
            "call_script": None,
            "kommo_note_ru": "note",
            "task": {"type": "follow_up", "title_ru": "task", "due_rule": "next_business_day", "due_at": None},
            "second_follow_up": {"enabled": False, "days_after_first": 0, "text": None},
        }
    )
    text = telegram_ui.render_preview(job, snapshot=snapshot, qualification=qualification)
    assert "DRY RUN" in text
    keyboard = telegram_ui.preview_keyboard(job, qualification)
    assert any("dry-run" in btn["text"].lower() for row in keyboard["inline_keyboard"] for btn in row)


def test_manual_match_renders_row_buttons():
    from app.services.lead_intake.matching import LeadSnapshot

    snapshot = LeadSnapshot(
        facebook_lead_id=None, phone="728387128", email="jan_ovo@wp.pl", name="Andrzej Janka", product="narzędzia"
    )
    from tests.lead_intake_helpers import make_row

    candidates = [
        make_row(row_number=167, product="narzędzia"),
        make_row(row_number=181, product="elektronarzędzia"),
    ]
    text, keyboard = telegram_ui.render_manual_match(
        snapshot=snapshot, candidates=candidates, reason="duplicate_phone", job_id=42
    )
    assert "167" in text and "181" in text
    callback_targets = [btn["callback_data"] for row in keyboard["inline_keyboard"] for btn in row]
    assert "lp:pick:42:167" in callback_targets
    assert "lp:pick:42:181" in callback_targets
    assert "lp:skip:42" in callback_targets
