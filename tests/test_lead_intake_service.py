"""End-to-end saga tests for the Facebook lead-intake pipeline.

Covers the mandatory Andrzej Janka scenario plus the required idempotency,
retry, checkpoint-resume and dry-run test cases.
"""

from __future__ import annotations

from contextlib import ExitStack
from unittest.mock import AsyncMock, patch

import pytest

from app.services.google_sheets_service import SpreadsheetRow
from app.services.lead_intake import detection, kommo_notes, repository, service
from app.services.lead_intake.schema import LeadQualification, LeadQualificationError
from tests.lead_intake_helpers import make_row, temp_db_session

ANDRZEJ_RAW_SNAPSHOT = {
    "kommo_lead_id": 555001,
    "original_title": "Facebook #12312412",
    "facebook_lead_id": "12312412",
    "facebook_technical_tag": "Facebook Lead Ads form",
    "source": "facebook_lead_ads",
    "name": "Andrzej Janka",
    "phone": "728387128",
    "email": "jan_ovo@wp.pl",
    "product": "narzędzia",
    "budget": "$5_000_-_$10_000",
    "contact_channel": "whats_app",
    "region": "kujawsko pomorskie",
    "created_at": 1700000000,
    "custom_fields": [],
    "unsorted_metadata": {},
}

QUALIFICATION_WHATSAPP = {
    "product_name_ru": "Инструменты",
    "potential": "medium",
    "readiness": "low",
    "priority": "C",
    "priority_label_ru": "квалификация",
    "recommended_action": "whatsapp",
    "recommended_action_reason_ru": "Категория товара указана слишком широко.",
    "lead_analysis_ru": "Клиент указал только общую категорию инструментов.",
    "main_risks_ru": ["Не указан вид инструментов"],
    "missing_information_ru": ["Перечень товаров"],
    "next_steps_ru": ["Отправить сообщение в WhatsApp"],
    "client_message": {"language": "pl", "channel": "whatsapp", "text": "Dzień dobry Panie Andrzeju"},
    "call_script": None,
    "kommo_note_ru": "Полный текст примечания",
    "task": {
        "type": "follow_up",
        "title_ru": "Получить перечень инструментов",
        "due_rule": "next_business_day",
        "due_at": None,
    },
    "second_follow_up": {"enabled": True, "days_after_first": 2, "text": "Dzień dobry, wracam"},
}


def _lead_details(name: str = "Facebook #12312412", pipeline_id: int = 9, status_id: int = 1):
    return {
        "id": 555001,
        "name": name,
        "pipeline_id": pipeline_id,
        "status_id": status_id,
        "url": "https://example.kommo.com/leads/detail/555001",
    }


class _KommoState:
    """Small in-memory stand-in for the parts of Kommo we mutate in tests."""

    def __init__(self):
        self.details = _lead_details()
        self.notes: list[dict] = []
        self.tasks: list[dict] = []
        self.rename_calls = 0
        self.stage_calls = 0

    async def get_lead_details(self, lead_id):
        return dict(self.details)

    async def update_kommo_lead(self, lead_id, **kwargs):
        if "name" in kwargs:
            self.details["name"] = kwargs["name"]
            self.rename_calls += 1
        if "status_id" in kwargs:
            self.details["status_id"] = kwargs["status_id"]
            self.stage_calls += 1
        if "pipeline_id" in kwargs:
            self.details["pipeline_id"] = kwargs["pipeline_id"]
        return {"lead_id": lead_id, **kwargs}

    async def get_recent_common_notes(self, lead_id, limit=50):
        return list(self.notes)

    async def add_common_note(self, lead_id, text):
        self.notes.append({"text": text})
        return True

    async def get_open_lead_tasks(self, lead_id, limit=50):
        return list(self.tasks)

    async def create_lead_task(self, *, lead_id, text, complete_till, responsible_user_id=None):
        self.tasks.append({"text": text, "complete_till": complete_till})
        return {"task_id": len(self.tasks)}

    async def get_pipeline_statuses(self, pipeline_id):
        return [{"id": 42, "name": "Первый контакт", "sort": 10}]


def _kommo_patches(state: _KommoState):
    return [
        patch("app.services.lead_intake.service.kommo_service.get_lead_details", new=state.get_lead_details),
        patch("app.services.lead_intake.service.kommo_service.update_kommo_lead", new=state.update_kommo_lead),
        patch(
            "app.services.lead_intake.service.kommo_service.get_recent_common_notes",
            new=state.get_recent_common_notes,
        ),
        patch("app.services.lead_intake.service.kommo_service.add_common_note", new=state.add_common_note),
        patch("app.services.lead_intake.service.kommo_service.get_open_lead_tasks", new=state.get_open_lead_tasks),
        patch("app.services.lead_intake.service.kommo_service.create_lead_task", new=state.create_lead_task),
        patch(
            "app.services.lead_intake.service.kommo_service.get_pipeline_statuses",
            new=state.get_pipeline_statuses,
        ),
    ]


def _sheets_patches(row: SpreadsheetRow, write_result: dict | None = None):
    write_result = write_result or {"written": True, "verified": True}
    return [
        patch("app.services.lead_intake.service.google_sheets_service.get_rows", return_value=[row]),
        patch(
            "app.services.lead_intake.service.google_sheets_service.get_row_by_number",
            return_value=row,
        ),
        patch(
            "app.services.lead_intake.service.google_sheets_service.write_internal_lead_number",
            return_value=write_result,
        ),
    ]


def _ai_patch(qualification_payload: dict = QUALIFICATION_WHATSAPP):
    qualification = LeadQualification.model_validate(qualification_payload)
    return patch(
        "app.services.lead_intake.service.ai_service.generate_lead_qualification",
        new=AsyncMock(return_value=qualification),
    )


def _enter(stack: ExitStack, *groups) -> None:
    for group in groups:
        if isinstance(group, (list, tuple)):
            for manager in group:
                stack.enter_context(manager)
        else:
            stack.enter_context(group)


def _andrzej_row(lead_number: str | None = None) -> SpreadsheetRow:
    return make_row(
        row_number=167,
        phone="+48 728 387 128",
        email="jan_ovo@wp.pl",
        client_name="Andrzej Janka",
        product="narzędzia",
        lead_number=lead_number,
        region="kujawsko-pomorskie",
        budget="$5 000-10 000",
    )


async def _create_job(db, *, dry_run: bool = False):
    return await repository.create_job(
        db,
        kommo_lead_id=555001,
        original_title="Facebook #12312412",
        facebook_lead_id="12312412",
        facebook_technical_tag="Facebook Lead Ads form",
        source="facebook_lead_ads",
        raw_snapshot=dict(ANDRZEJ_RAW_SNAPSHOT),
        dry_run=dry_run,
        processing_version=1,
    )


@pytest.mark.asyncio
async def test_full_andrzej_janka_scenario_matches_and_applies():
    async with temp_db_session() as db:
        job = await _create_job(db)
        row = _andrzej_row(lead_number="167")
        state = _KommoState()

        with ExitStack() as stack:
            _enter(stack, _ai_patch(), _sheets_patches(row), _kommo_patches(state))
            preview = await service.build_preview(db, job)
            assert preview.kind == "preview"
            job = preview.job
            assert job.assigned_number == "167"  # reused existing sheet number
            assert job.match_method == "phone_and_email"
            assert job.status == "waiting_approval"
            assert preview.qualification.product_name_ru == "Инструменты"
            assert preview.qualification.priority == "C"
            assert preview.qualification.recommended_action == "whatsapp"

            result = await service.apply_job(db, job.id)
            assert result.status == "completed"

        assert state.details["name"] == "167 - Инструменты"
        assert state.rename_calls == 1
        assert state.stage_calls == 1
        assert state.details["status_id"] == 42
        assert len(state.notes) == 1
        assert kommo_notes.note_marker(555001, 1) in state.notes[0]["text"]
        # WhatsApp task creation is deferred until the manager confirms the
        # message was actually sent.
        assert state.tasks == []

        completed_job = await repository.get_by_id(db, job.id)
        assert completed_job.status == "completed"
        assert completed_job.current_checkpoint == "completed"

        with patch(
            "app.services.lead_intake.service.kommo_service.get_open_lead_tasks",
            new=state.get_open_lead_tasks,
        ), patch(
            "app.services.lead_intake.service.kommo_service.create_lead_task",
            new=state.create_lead_task,
        ):
            confirm = await service.confirm_whatsapp_sent(db, job.id)
        assert confirm.status == "completed"
        assert len(state.tasks) == 1
        assert kommo_notes.task_marker(555001, 1) in state.tasks[0]["text"]

        # Repeated confirmation must not create a second task.
        with patch(
            "app.services.lead_intake.service.kommo_service.get_open_lead_tasks",
            new=state.get_open_lead_tasks,
        ), patch(
            "app.services.lead_intake.service.kommo_service.create_lead_task",
            new=state.create_lead_task,
        ):
            confirm_again = await service.confirm_whatsapp_sent(db, job.id)
        assert confirm_again.status == "already_done"
        assert len(state.tasks) == 1


@pytest.mark.asyncio
async def test_repeated_apply_is_idempotent_no_duplicate_side_effects():
    async with temp_db_session() as db:
        job = await _create_job(db)
        row = _andrzej_row(lead_number="167")
        state = _KommoState()

        with ExitStack() as stack:
            _enter(stack, _ai_patch(), _sheets_patches(row), _kommo_patches(state))
            preview = await service.build_preview(db, job)
            job = preview.job
            first = await service.apply_job(db, job.id)
            assert first.status == "completed"
            second = await service.apply_job(db, job.id)
            assert second.status == "already_completed"

        assert state.rename_calls == 1
        assert state.stage_calls == 1
        assert len(state.notes) == 1
        assert len(state.tasks) == 0  # whatsapp task deferred


@pytest.mark.asyncio
async def test_note_and_task_are_not_duplicated_when_marker_already_present():
    async with temp_db_session() as db:
        job = await _create_job(db)
        row = _andrzej_row(lead_number="167")
        state = _KommoState()
        # A previous partial run already left the marked note/task in place.
        state.notes.append({"text": f"old note {kommo_notes.note_marker(555001, 1)}"})
        state.tasks.append({"text": f"old task {kommo_notes.task_marker(555001, 1)}"})

        payload = dict(QUALIFICATION_WHATSAPP)
        payload["recommended_action"] = "email"
        payload["client_message"] = {"language": "pl", "channel": "email", "text": "Dzień dobry"}

        with ExitStack() as stack:
            _enter(stack, _ai_patch(payload), _sheets_patches(row), _kommo_patches(state))
            preview = await service.build_preview(db, job)
            job = preview.job
            result = await service.apply_job(db, job.id)

        assert result.status == "completed"
        assert len(state.notes) == 1  # no new note added
        assert len(state.tasks) == 1  # no new task added


@pytest.mark.asyncio
async def test_kommo_note_failure_then_retry_does_not_repeat_rename_or_stage():
    async with temp_db_session() as db:
        job = await _create_job(db)
        row = _andrzej_row(lead_number="167")
        state = _KommoState()

        async def failing_note(lead_id, text):
            raise RuntimeError("Kommo note API is down")

        with ExitStack() as stack:
            _enter(stack, _ai_patch(), _sheets_patches(row), _kommo_patches(state))
            preview = await service.build_preview(db, job)
            job = preview.job

            with patch(
                "app.services.lead_intake.service.kommo_service.add_common_note", new=failing_note
            ):
                first = await service.apply_job(db, job.id)
            assert first.status == "error"
            assert first.job.error_code == "RuntimeError"
            assert first.job.current_checkpoint == "kommo_stage_moved"
            assert state.rename_calls == 1
            assert state.stage_calls == 1

            second = await service.apply_job(db, job.id)
            assert second.status == "completed"

        # Retry must not repeat the already-completed rename/stage steps.
        assert state.rename_calls == 1
        assert state.stage_calls == 1
        assert len(state.notes) == 1


@pytest.mark.asyncio
async def test_kommo_rename_failure_is_retry_safe_and_sheets_write_not_repeated():
    async with temp_db_session() as db:
        job = await _create_job(db)
        row = _andrzej_row(lead_number="167")
        state = _KommoState()
        write_calls: list[dict] = []

        def counting_write(**kwargs):
            write_calls.append(kwargs)
            return {"written": True, "verified": True}

        async def failing_rename(lead_id, **kwargs):
            if "name" in kwargs:
                raise RuntimeError("Kommo rename API failed")
            return await state.update_kommo_lead(lead_id, **kwargs)

        with ExitStack() as stack:
            _enter(
                stack,
                _ai_patch(),
                patch("app.services.lead_intake.service.google_sheets_service.get_rows", return_value=[row]),
                patch(
                    "app.services.lead_intake.service.google_sheets_service.get_row_by_number",
                    return_value=row,
                ),
                patch(
                    "app.services.lead_intake.service.google_sheets_service.write_internal_lead_number",
                    side_effect=counting_write,
                ),
                _kommo_patches(state),
            )
            preview = await service.build_preview(db, job)
            job = preview.job

            with patch(
                "app.services.lead_intake.service.kommo_service.update_kommo_lead", new=failing_rename
            ):
                first = await service.apply_job(db, job.id)
            assert first.status == "error"
            assert first.job.current_checkpoint == "sheet_number_verified"
            assert len(write_calls) == 1

            second = await service.apply_job(db, job.id)
            assert second.status == "completed"

        assert state.rename_calls == 1
        # The sheet number was already written and verified on the first
        # attempt; retrying must not write it a second time.
        assert len(write_calls) == 1


@pytest.mark.asyncio
async def test_kommo_stage_move_failure_is_retry_safe():
    async with temp_db_session() as db:
        job = await _create_job(db)
        row = _andrzej_row(lead_number="167")
        state = _KommoState()

        async def failing_stage(lead_id, **kwargs):
            if "status_id" in kwargs or "pipeline_id" in kwargs:
                raise RuntimeError("Kommo stage API failed")
            return await state.update_kommo_lead(lead_id, **kwargs)

        with ExitStack() as stack:
            _enter(stack, _ai_patch(), _sheets_patches(row), _kommo_patches(state))
            preview = await service.build_preview(db, job)
            job = preview.job

            with patch(
                "app.services.lead_intake.service.kommo_service.update_kommo_lead", new=failing_stage
            ):
                first = await service.apply_job(db, job.id)
            assert first.status == "error"
            assert first.job.current_checkpoint == "kommo_renamed"
            assert state.rename_calls == 1
            assert state.stage_calls == 0

            second = await service.apply_job(db, job.id)
            assert second.status == "completed"

        assert state.rename_calls == 1
        assert state.stage_calls == 1


@pytest.mark.asyncio
async def test_kommo_task_creation_failure_is_retry_safe():
    async with temp_db_session() as db:
        job = await _create_job(db)
        row = _andrzej_row(lead_number="167")
        state = _KommoState()

        payload = dict(QUALIFICATION_WHATSAPP, recommended_action="email")
        payload["client_message"] = {"language": "pl", "channel": "email", "text": "Dzień dobry"}

        async def failing_task(*, lead_id, text, complete_till, responsible_user_id=None):
            raise RuntimeError("Kommo task API failed")

        with ExitStack() as stack:
            _enter(stack, _ai_patch(payload), _sheets_patches(row), _kommo_patches(state))
            preview = await service.build_preview(db, job)
            job = preview.job

            with patch(
                "app.services.lead_intake.service.kommo_service.create_lead_task", new=failing_task
            ):
                first = await service.apply_job(db, job.id)
            assert first.status == "error"
            assert first.job.current_checkpoint == "kommo_note_added"
            assert len(state.notes) == 1

            second = await service.apply_job(db, job.id)
            assert second.status == "completed"

        assert len(state.notes) == 1
        assert len(state.tasks) == 1


@pytest.mark.asyncio
async def test_sheets_write_failure_blocks_apply_and_kommo_is_untouched():
    async with temp_db_session() as db:
        job = await _create_job(db)
        row = _andrzej_row(lead_number="167")
        state = _KommoState()

        with ExitStack() as stack:
            _enter(
                stack,
                _ai_patch(),
                _sheets_patches(row, write_result={"written": False, "verified": False, "reason": "row_changed"}),
                _kommo_patches(state),
            )
            preview = await service.build_preview(db, job)
            job = preview.job
            result = await service.apply_job(db, job.id)

        assert result.status == "error"
        assert result.job.error_code == "sheets_write_failed"
        assert state.rename_calls == 0
        assert state.notes == []


@pytest.mark.asyncio
async def test_verification_failure_after_write_is_treated_as_failure():
    async with temp_db_session() as db:
        job = await _create_job(db)
        row = _andrzej_row(lead_number="167")
        state = _KommoState()

        with ExitStack() as stack:
            _enter(
                stack,
                _ai_patch(),
                _sheets_patches(row, write_result={"written": True, "verified": False, "reason": "unknown"}),
                _kommo_patches(state),
            )
            preview = await service.build_preview(db, job)
            job = preview.job
            result = await service.apply_job(db, job.id)

        assert result.status == "error"
        assert result.job.error_code == "sheets_write_failed"


@pytest.mark.asyncio
async def test_columns_w_and_x_are_never_touched_by_the_saga():
    async with temp_db_session() as db:
        job = await _create_job(db)
        row = _andrzej_row(lead_number="167")
        state = _KommoState()

        with ExitStack() as stack:
            _enter(stack, _ai_patch(), _sheets_patches(row), _kommo_patches(state))
            legacy_writer = stack.enter_context(
                patch("app.services.lead_intake.service.google_sheets_service.apply_lead_registry_updates")
            )
            preview = await service.build_preview(db, job)
            job = preview.job
            result = await service.apply_job(db, job.id)

        assert result.status == "completed"
        # The saga never calls the batch updater that can also touch X, and
        # never references W/X columns anywhere.
        legacy_writer.assert_not_called()


@pytest.mark.asyncio
async def test_dry_run_performs_no_external_writes():
    async with temp_db_session() as db:
        job = await _create_job(db, dry_run=True)
        row = _andrzej_row(lead_number=None)  # no existing number: dry-run must only "simulate" one
        state = _KommoState()

        with ExitStack() as stack:
            write_mock = AsyncMock()
            _enter(
                stack,
                _ai_patch(),
                patch("app.services.lead_intake.service.google_sheets_service.get_rows", return_value=[row]),
                patch(
                    "app.services.lead_intake.service.google_sheets_service.write_internal_lead_number",
                    new=write_mock,
                ),
                _kommo_patches(state),
            )
            preview = await service.build_preview(db, job)
            assert preview.kind == "preview"
            job = preview.job
            assert job.assigned_number  # a number was proposed for preview...
            result = await service.apply_job(db, job.id)

        assert result.status == "dry_run"
        write_mock.assert_not_called()
        assert state.rename_calls == 0
        assert state.stage_calls == 0
        assert state.notes == []
        assert state.tasks == []


@pytest.mark.asyncio
async def test_ambiguous_phone_requires_manual_match_then_continues():
    async with temp_db_session() as db:
        job = await _create_job(db)
        row_a = make_row(row_number=167, phone="728387128", product="narzędzia")
        row_b = make_row(row_number=181, phone="728387128", product="elektronarzędzia")
        state = _KommoState()

        with patch(
            "app.services.lead_intake.service.google_sheets_service.get_rows",
            return_value=[row_a, row_b],
        ):
            preview = await service.build_preview(db, job)
        assert preview.kind == "manual_match"
        assert preview.job.status == "manual_match_required"
        assert {row.row_number for row in preview.candidates} == {167, 181}

        with ExitStack() as stack:
            _enter(
                stack,
                _ai_patch(),
                patch(
                    "app.services.lead_intake.service.google_sheets_service.get_rows",
                    return_value=[row_a, row_b],
                ),
                patch(
                    "app.services.lead_intake.service.google_sheets_service.get_row_by_number",
                    return_value=row_a,
                ),
                patch(
                    "app.services.lead_intake.service.google_sheets_service.write_internal_lead_number",
                    return_value={"written": True, "verified": True},
                ),
                _kommo_patches(state),
            )
            picked = await service.select_manual_match(db, preview.job, row_number=167)
            assert picked.kind == "preview"
            assert picked.job.match_method == "manual_operator_selection"
            result = await service.apply_job(db, picked.job.id)

        assert result.status == "completed"


@pytest.mark.asyncio
async def test_skip_leaves_kommo_and_sheets_untouched_and_sets_status():
    async with temp_db_session() as db:
        job = await _create_job(db)
        with (
            patch(
                "app.services.lead_intake.service.google_sheets_service.apply_lead_registry_updates"
            ) as legacy_writer,
            patch("app.services.lead_intake.service.kommo_service.update_kommo_lead") as rename_mock,
        ):
            job = await service.skip_job(db, job)
        assert job.status == "skipped"
        assert job.completed_at is not None
        legacy_writer.assert_not_called()
        rename_mock.assert_not_called()


@pytest.mark.asyncio
async def test_completed_and_skipped_jobs_are_excluded_from_active_list():
    async with temp_db_session() as db:
        job1 = await repository.create_job(
            db, kommo_lead_id=1, original_title="Facebook #1", facebook_lead_id="1",
            facebook_technical_tag=None, source="facebook_lead_ads", raw_snapshot={}, dry_run=False,
            processing_version=1,
        )
        job2 = await repository.create_job(
            db, kommo_lead_id=2, original_title="Facebook #2", facebook_lead_id="2",
            facebook_technical_tag=None, source="facebook_lead_ads", raw_snapshot={}, dry_run=False,
            processing_version=1,
        )
        await repository.mark_completed(db, job1)
        await repository.mark_skipped(db, job2)
        active = await repository.list_active_jobs(db)
        assert active == []


@pytest.mark.asyncio
async def test_find_or_create_next_job_resumes_active_job_before_detecting_new_ones():
    async with temp_db_session() as db:
        job = await _create_job(db)
        with (
            patch.object(service.settings, "kommo_poland_pipeline_id", None),
            patch.object(detection.settings, "kommo_poland_pipeline_id", None),
            patch(
                "app.services.lead_intake.service.detection.find_candidate_leads",
                new=AsyncMock(return_value=[]),
            ) as find_mock,
        ):
            resumed = await service.find_or_create_next_job(db)
        assert resumed.id == job.id
        find_mock.assert_not_called()


@pytest.mark.asyncio
async def test_find_or_create_next_job_auto_skips_ukraine_active_job():
    async with temp_db_session() as db:
        ukraine = await repository.create_job(
            db,
            kommo_lead_id=999001,
            original_title="Facebook #ua",
            facebook_lead_id="ua1",
            facebook_technical_tag=None,
            source="facebook_lead_ads",
            raw_snapshot={"pipeline_id": 13901771, "name": "Svetlana", "phone": "+380638569124"},
            dry_run=True,
            processing_version=1,
        )
        with (
            patch.object(service.settings, "kommo_poland_pipeline_id", 13866843),
            patch.object(detection.settings, "kommo_poland_pipeline_id", 13866843),
            patch(
                "app.services.lead_intake.service.detection.find_candidate_leads",
                new=AsyncMock(return_value=[]),
            ),
        ):
            next_job = await service.find_or_create_next_job(db)
        await db.refresh(ukraine)
        assert ukraine.status == "skipped"
        assert next_job is None


@pytest.mark.asyncio
async def test_lead_is_tracked_by_kommo_id_after_rename_not_by_title():
    async with temp_db_session() as db:
        job = await _create_job(db)
        row = _andrzej_row(lead_number="167")
        state = _KommoState()

        with ExitStack() as stack:
            _enter(stack, _ai_patch(), _sheets_patches(row), _kommo_patches(state))
            preview = await service.build_preview(db, job)
            job = preview.job
            await service.apply_job(db, job.id)

        assert state.details["name"] == "167 - Инструменты"
        # Even though the mutable Kommo title changed, the job is still
        # reachable by its permanent kommo_lead_id.
        again = await repository.get_by_kommo_lead_id(db, 555001)
        assert again is not None
        assert again.id == job.id

        with patch(
            "app.services.lead_intake.service.detection.find_candidate_leads",
            new=AsyncMock(return_value=[]),
        ):
            next_job = await service.find_or_create_next_job(db)
        assert next_job is None


@pytest.mark.asyncio
async def test_invalid_ai_json_is_never_written_to_kommo():
    async with temp_db_session() as db:
        job = await _create_job(db)
        row = _andrzej_row(lead_number="167")
        state = _KommoState()

        with ExitStack() as stack:
            _enter(
                stack,
                patch(
                    "app.services.lead_intake.service.ai_service.generate_lead_qualification",
                    new=AsyncMock(side_effect=LeadQualificationError("invalid json twice")),
                ),
                _sheets_patches(row),
                _kommo_patches(state),
            )
            preview = await service.build_preview(db, job)

        assert preview.kind == "error"
        assert preview.job.status == "error"
        assert preview.job.ai_payload_json is None
        assert state.notes == []
        assert state.rename_calls == 0
