"""Orchestrates the one-lead-at-a-time Facebook intake saga.

Steps 1-4 of the contract (detect -> match -> assign/reuse number -> persist
state) happen in ``find_or_create_next_job`` / ``build_preview`` and never
touch Kommo or Google Sheets for writes. Only ``apply_job`` performs
external writes, through an ordered, checkpointed, retry-safe saga.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.security import sanitize_text
from app.config import get_settings
from app.models.lead_processing_job import LEAD_PROCESSING_CHECKPOINTS, LeadProcessingJob
from app.services import google_sheets_service, kommo_service, phone_utils, product_title_service
from app.services.google_sheets_service import SpreadsheetRow
from app.services.lead_intake import ai_service, business_hours, detection, kommo_notes, matching, numbering, repository
from app.services.lead_intake.errors import LeadIntakeError
from app.services.lead_intake.matching import LeadSnapshot
from app.services.lead_intake.schema import LeadQualification, LeadQualificationError

logger = logging.getLogger(__name__)
settings = get_settings()

EDITABLE_FIELDS = {
    "product_name_ru",
    "priority",
    "recommended_action",
    "client_message_text",
    "kommo_note_ru",
    "task_title_ru",
    "task_due_at",
}

PreviewKind = Literal["preview", "manual_match", "error", "no_leads"]
ApplyStatus = Literal[
    "completed", "already_completed", "not_ready", "dry_run", "error", "not_applicable", "already_done"
]


@dataclass
class PreviewResult:
    kind: PreviewKind
    job: LeadProcessingJob | None = None
    snapshot: LeadSnapshot | None = None
    qualification: LeadQualification | None = None
    candidates: list[SpreadsheetRow] = field(default_factory=list)
    message: str | None = None


@dataclass
class ApplyResult:
    status: ApplyStatus
    job: LeadProcessingJob
    error: str | None = None


def _clean(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()


def _row_key(row: SpreadsheetRow) -> str:
    parts = [str(row.phone or ""), str(row.email or ""), str(row.client_name or ""), str(row.product or "")]
    return hashlib.sha256("|".join(parts).casefold().encode("utf-8")).hexdigest()[:32]


def _row_fingerprint(row: SpreadsheetRow) -> tuple[str, str, str, str]:
    def norm(value: Any) -> str:
        return " ".join(str(value or "").split()).casefold()

    return (norm(row.phone), norm(row.email), norm(row.client_name), norm(row.product))


def _row_summary(row: SpreadsheetRow) -> dict[str, Any]:
    return {
        "row_number": row.row_number,
        "client_name": row.client_name,
        "product": row.product,
        "phone": row.phone,
        "email": row.email,
    }


def _floor_hint_from_rows(rows: list[SpreadsheetRow]) -> int:
    best = 0
    for row in rows:
        value = str(row.lead_number or "").strip()
        if value.isdigit():
            best = max(best, int(value))
    return best


def snapshot_from_job(job: LeadProcessingJob) -> LeadSnapshot:
    raw = dict(job.raw_snapshot_json or {})
    return LeadSnapshot(
        facebook_lead_id=raw.get("facebook_lead_id"),
        phone=raw.get("phone"),
        email=raw.get("email"),
        name=raw.get("name"),
        product=raw.get("product"),
        region=raw.get("region"),
        created_at=raw.get("created_at"),
    )


def effective_qualification_dict(job: LeadProcessingJob) -> dict[str, Any]:
    """Merge the AI-generated payload with manager edit overrides."""
    base: dict[str, Any] = dict(job.ai_payload_json or {})
    edits = dict(job.edited_payload_json or {})
    if not base:
        return base
    if "product_name_ru" in edits:
        base["product_name_ru"] = edits["product_name_ru"]
    if "priority" in edits:
        base["priority"] = edits["priority"]
    if "recommended_action" in edits:
        base["recommended_action"] = edits["recommended_action"]
    if "client_message_text" in edits:
        base["client_message"] = {**(base.get("client_message") or {}), "text": edits["client_message_text"]}
    if "kommo_note_ru" in edits:
        base["kommo_note_ru"] = edits["kommo_note_ru"]
    if "task_title_ru" in edits:
        base["task"] = {**(base.get("task") or {}), "title_ru": edits["task_title_ru"]}
    if "task_due_at" in edits:
        base["task"] = {**(base.get("task") or {}), "due_at": edits["task_due_at"], "due_rule": "manual"}
    return base


def qualification_from_job(job: LeadProcessingJob) -> LeadQualification | None:
    payload = effective_qualification_dict(job)
    if not payload:
        return None
    return LeadQualification.model_validate(payload)


def _ai_payload(job: LeadProcessingJob, snapshot: LeadSnapshot, row: SpreadsheetRow, product_ru: str) -> dict[str, Any]:
    raw = dict(job.raw_snapshot_json or {})
    return {
        "kommo_lead_id": job.kommo_lead_id,
        "assigned_internal_number": job.assigned_number,
        "client_name": snapshot.name or row.client_name,
        "company_name": row.company,
        "phone_e164": phone_utils.to_e164(snapshot.phone),
        "email": snapshot.email,
        "region": snapshot.region or row.region,
        "preferred_contact_channel": raw.get("contact_channel") or row.contact_channel,
        "requested_product_raw": snapshot.product or row.product,
        "requested_product_translation_hint_ru": product_ru,
        "budget_raw": raw.get("budget") or row.budget,
        "lead_created_at": snapshot.created_at,
        "facebook_lead_id": snapshot.facebook_lead_id,
        "sheet_row_number": row.row_number,
        "company_research_rule": (
            "If company_name is present, form a cautious B2B hypothesis from the "
            "name + product only. Do not invent founding years, assortment lists, "
            "or website facts."
        ),
    }


def _job_pipeline_id(job: LeadProcessingJob) -> int | None:
    raw = job.raw_snapshot_json or {}
    value = raw.get("pipeline_id")
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


async def _resolve_job_pipeline_id(db: AsyncSession, job: LeadProcessingJob) -> int | None:
    """Return the job's Kommo pipeline, backfilling it into the snapshot when missing."""
    known = _job_pipeline_id(job)
    if known is not None:
        return known
    try:
        details = await kommo_service.get_lead_details(job.kommo_lead_id)
    except Exception as exc:  # pragma: no cover - network guard
        logger.warning(
            "lead_intake.pipeline_lookup_failed kommo_lead_id=%s err=%s",
            job.kommo_lead_id,
            type(exc).__name__,
        )
        return None
    pipeline_id = details.get("pipeline_id")
    if pipeline_id is None:
        return None
    raw = dict(job.raw_snapshot_json or {})
    raw["pipeline_id"] = pipeline_id
    raw["pipeline_name"] = details.get("pipeline_name")
    await repository.save(db, job, raw_snapshot_json=raw)
    try:
        return int(pipeline_id)
    except (TypeError, ValueError):
        return None


async def find_or_create_next_job(
    db: AsyncSession, *, dry_run: bool | None = None
) -> LeadProcessingJob | None:
    """Return the job to show next: resume an in-flight one, else detect a new one."""
    target_pipeline = detection.target_pipeline_id()
    active = await repository.list_active_jobs(db)
    for job in active:
        if target_pipeline is not None:
            job_pipeline = await _resolve_job_pipeline_id(db, job)
            if job_pipeline is not None and job_pipeline != target_pipeline:
                logger.info(
                    "lead_intake.auto_skip_wrong_pipeline kommo_lead_id=%s job_pipeline=%s target=%s",
                    job.kommo_lead_id,
                    job_pipeline,
                    target_pipeline,
                )
                await repository.mark_skipped(db, job)
                continue
        return job

    effective_dry_run = settings.lead_processing_dry_run if dry_run is None else dry_run
    candidates = await detection.find_candidate_leads()
    for candidate in candidates:
        lead_id = int(candidate.get("id") or 0)
        if not lead_id:
            continue
        existing = await repository.get_by_kommo_lead_id(db, lead_id)
        if existing is not None:
            continue
        raw_snapshot, snapshot = await detection.build_snapshot(
            lead_id,
            unsorted_metadata=candidate.get("metadata"),
            unsorted_source_name=candidate.get("source_name"),
        )
        # Defense in depth: unsorted filter can miss edge cases; never queue
        # a lead from Украина / another pipeline when Poland is configured.
        if not detection.lead_matches_target_pipeline(raw_snapshot, target_pipeline):
            logger.info(
                "lead_intake.skip_non_target_pipeline kommo_lead_id=%s pipeline_id=%s target=%s",
                lead_id,
                raw_snapshot.get("pipeline_id"),
                target_pipeline,
            )
            continue
        job = await repository.create_job(
            db,
            kommo_lead_id=lead_id,
            original_title=candidate.get("name"),
            facebook_lead_id=snapshot.facebook_lead_id,
            facebook_technical_tag=raw_snapshot.get("facebook_technical_tag"),
            source=raw_snapshot.get("source"),
            raw_snapshot=raw_snapshot,
            dry_run=effective_dry_run,
            processing_version=settings.lead_processing_version,
        )
        logger.info(
            "lead_intake.detected kommo_lead_id=%s facebook_lead_id=%s pipeline_id=%s",
            lead_id,
            snapshot.facebook_lead_id,
            raw_snapshot.get("pipeline_id"),
        )
        return job
    return None


async def _continue_after_match(db: AsyncSession, job: LeadProcessingJob, row: SpreadsheetRow) -> PreviewResult:
    snapshot = snapshot_from_job(job)
    rows = await asyncio.to_thread(google_sheets_service.get_rows)
    db_floor = await repository.max_assigned_number(db)
    floor_hint = max(db_floor, _floor_hint_from_rows(rows))

    assigned_number, _newly = await numbering.assign_or_reuse_number(
        db, existing_number=row.lead_number, floor_hint=floor_hint, dry_run=job.dry_run
    )

    if not job.dry_run:
        conflict = await repository.get_by_assigned_number(db, assigned_number)
        if conflict is not None and conflict.id != job.id:
            job = await repository.save(
                db,
                job,
                status="manual_match_required",
                match_reason=matching.REASON_ASSIGNED_NUMBER_CONFLICT,
                error_code=matching.REASON_ASSIGNED_NUMBER_CONFLICT,
                error_message=(
                    f"Номер {assigned_number} уже используется лидом "
                    f"{conflict.kommo_lead_id}."
                ),
            )
            return PreviewResult(kind="error", job=job, message=job.error_message)

    try:
        job = await repository.save(
            db,
            job,
            sheet_id=settings.google_sheets_spreadsheet_id or None,
            sheet_row_number=row.row_number,
            sheet_row_key=_row_key(row),
            assigned_number=assigned_number,
            status="number_assigned",
            match_reason=None,
            error_code=None,
            error_message=None,
        )
    except IntegrityError:
        # Extremely unlikely last-resort backstop: two allocations raced past
        # the advisory lock (e.g. a non-Postgres deployment). The unique
        # constraint on assigned_number rejected the duplicate — never let a
        # second lead silently reuse someone else's number.
        await db.rollback()
        job = await repository.save(
            db,
            job,
            status="manual_match_required",
            match_reason=matching.REASON_ASSIGNED_NUMBER_CONFLICT,
            error_code=matching.REASON_ASSIGNED_NUMBER_CONFLICT,
            error_message=f"Номер {assigned_number} уже был занят параллельно. Повторите сопоставление.",
        )
        return PreviewResult(kind="error", job=job, message=job.error_message)
    logger.info(
        "lead_intake.number_assigned kommo_lead_id=%s assigned_number=%s match_method=%s match_score=%s",
        job.kommo_lead_id,
        assigned_number,
        job.match_method,
        job.match_score,
    )

    product_ru = await product_title_service.short_product_title(snapshot.product or "")
    payload = _ai_payload(job, snapshot, row, product_ru)
    try:
        qualification = await ai_service.generate_lead_qualification(payload)
    except LeadQualificationError as exc:
        logger.warning("lead_intake.ai_failed kommo_lead_id=%s error=%s", job.kommo_lead_id, exc)
        job = await repository.save(
            db, job, status="error", error_code="ai_qualification_failed", error_message=str(exc)[:2000]
        )
        return PreviewResult(kind="error", job=job, message=str(exc))

    # The deterministic translation service (with the project's curated
    # rules) is more reliable/testable than free-form AI wording for the
    # short Kommo title, so it always wins for product_name_ru.
    qualification = qualification.model_copy(update={"product_name_ru": product_ru})

    job = await repository.save(
        db,
        job,
        status="waiting_approval",
        ai_payload_json=qualification.model_dump(mode="json"),
        current_checkpoint="started",
    )
    logger.info(
        "lead_intake.ai_generated kommo_lead_id=%s priority=%s recommended_action=%s",
        job.kommo_lead_id,
        qualification.priority,
        qualification.recommended_action,
    )
    return PreviewResult(kind="preview", job=job, snapshot=snapshot, qualification=qualification)


async def build_preview(db: AsyncSession, job: LeadProcessingJob) -> PreviewResult:
    if job.status == "waiting_approval" and job.ai_payload_json:
        return PreviewResult(
            kind="preview",
            job=job,
            snapshot=snapshot_from_job(job),
            qualification=qualification_from_job(job),
        )
    if job.status in {"completed", "skipped"}:
        return PreviewResult(kind="error", job=job, message="Лид уже обработан.")

    snapshot = snapshot_from_job(job)
    rows = await asyncio.to_thread(google_sheets_service.get_rows)
    job = await repository.save(db, job, status="matching")
    outcome = matching.match_lead(snapshot, rows)

    if outcome.status == "matched":
        job = await repository.save(
            db, job, status="matched", match_method=outcome.method, match_score=outcome.score
        )
        return await _continue_after_match(db, job, outcome.row)

    job = await repository.save(
        db,
        job,
        status="manual_match_required",
        match_method=outcome.method,
        match_reason=outcome.reason,
        manual_candidates_json=[_row_summary(row) for row in outcome.candidates],
    )
    logger.info(
        "lead_intake.manual_match_required kommo_lead_id=%s reason=%s candidates=%s",
        job.kommo_lead_id,
        outcome.reason,
        len(outcome.candidates),
    )
    return PreviewResult(kind="manual_match", job=job, snapshot=snapshot, candidates=outcome.candidates, message=outcome.reason)


async def select_manual_match(db: AsyncSession, job: LeadProcessingJob, *, row_number: int) -> PreviewResult:
    rows = await asyncio.to_thread(google_sheets_service.get_rows)
    row = next((item for item in rows if item.row_number == row_number), None)
    if row is None:
        raise ValueError("row_not_found")
    job = await repository.save(
        db,
        job,
        status="matched",
        match_method="manual_operator_selection",
        match_score=50,
        match_reason=None,
        manual_candidates_json=None,
    )
    return await _continue_after_match(db, job, row)


async def edit_field(db: AsyncSession, job: LeadProcessingJob, field_name: str, value: str) -> LeadProcessingJob:
    if field_name not in EDITABLE_FIELDS:
        raise ValueError(f"Недопустимое поле для редактирования: {field_name}")
    edited = dict(job.edited_payload_json or {})
    edited[field_name] = value
    return await repository.save(db, job, edited_payload_json=edited)


async def skip_job(db: AsyncSession, job: LeadProcessingJob) -> LeadProcessingJob:
    logger.info("lead_intake.skipped kommo_lead_id=%s", job.kommo_lead_id)
    return await repository.mark_skipped(db, job)


def _checkpoint_index(checkpoint: str | None) -> int:
    try:
        return LEAD_PROCESSING_CHECKPOINTS.index(checkpoint or "started")
    except ValueError:
        return 0


async def _advance(db: AsyncSession, job: LeadProcessingJob, checkpoint: str) -> str:
    """Persist forward saga progress, never regressing an already-passed checkpoint.

    Steps 1-2 (lead/number verification) always re-run on every apply/retry
    call since they are cheap read-only guards, not external writes. Without
    this monotonic guard, re-running them would reset ``current_checkpoint``
    backwards and defeat the "skip already completed steps" logic for every
    later step on every retry.
    """
    current_index = _checkpoint_index(job.current_checkpoint)
    new_index = _checkpoint_index(checkpoint)
    if job.current_checkpoint and new_index <= current_index:
        return job.current_checkpoint
    job.current_checkpoint = checkpoint
    await db.commit()
    await db.refresh(job)
    logger.info(
        "lead_intake.checkpoint kommo_lead_id=%s checkpoint=%s", job.kommo_lead_id, checkpoint
    )
    return checkpoint


async def _resolve_first_contact_stage(details: dict[str, Any]) -> tuple[int | None, int | None]:
    configured_pipeline = settings.kommo_poland_pipeline_id
    pipeline_id = configured_pipeline or details.get("pipeline_id")

    if settings.kommo_first_contact_status_id and (
        configured_pipeline is None or pipeline_id == configured_pipeline
    ):
        return pipeline_id, settings.kommo_first_contact_status_id

    if not isinstance(pipeline_id, int):
        return pipeline_id, None

    try:
        statuses = await kommo_service.get_pipeline_statuses(pipeline_id)
    except Exception as exc:  # pragma: no cover - defensive network guard
        logger.warning("lead_intake could not load pipeline statuses: %s", exc)
        return pipeline_id, None

    wanted = {
        (settings.kommo_first_contact_status_name or "Первый контакт").strip().casefold(),
        "первый контакт",
        "first contact",
        "pierwszy kontakt",
    }
    for status in statuses:
        if str(status.get("name") or "").strip().casefold() in wanted:
            return pipeline_id, int(status["id"])
    return pipeline_id, None


async def _ensure_primary_task(job: LeadProcessingJob, qualification: LeadQualification) -> bool:
    """Create the single follow-up task for this lead, if not already present.

    Returns ``True`` if a task was created, ``False`` if one already existed.
    """
    marker = kommo_notes.task_marker(job.kommo_lead_id, job.processing_version)
    tasks = await kommo_service.get_open_lead_tasks(job.kommo_lead_id, limit=50)
    if any(kommo_notes.task_has_marker(task.get("text"), marker) for task in tasks):
        return False
    due_at = business_hours.due_timestamp(
        qualification.task.due_rule, explicit_due_at=qualification.task.due_at
    )
    text = f"{qualification.task.title_ru} {marker}"
    await kommo_service.create_lead_task(lead_id=job.kommo_lead_id, text=text, complete_till=due_at)
    return True


async def apply_job(db: AsyncSession, job_id: int) -> ApplyResult:
    """Execute (or resume) the checkpointed apply saga for one job.

    Idempotent by design: repeated Telegram callback deliveries, retries
    after a partial failure, and re-processing a lead by mistake all resume
    from ``current_checkpoint`` instead of repeating completed steps.
    """
    job = await repository.get_by_id_locked(db, job_id)
    if job is None:
        raise ValueError("job_not_found")

    if job.status == "completed":
        return ApplyResult(status="already_completed", job=job)
    if job.dry_run:
        return ApplyResult(status="dry_run", job=job)
    if job.status not in {"waiting_approval", "applying", "error"}:
        return ApplyResult(status="not_ready", job=job)

    qualification = qualification_from_job(job)
    if qualification is None:
        return ApplyResult(status="not_ready", job=job)
    snapshot = snapshot_from_job(job)

    if job.status != "applying":
        job.status = "applying"
        await db.commit()
        await db.refresh(job)

    checkpoint = job.current_checkpoint or "started"
    try:
        details = await kommo_service.get_lead_details(job.kommo_lead_id)
        checkpoint = await _advance(db, job, "lead_verified")

        if not job.assigned_number:
            raise LeadIntakeError("missing_assigned_number", "Лиду не присвоен внутренний номер.")
        conflict = await repository.get_by_assigned_number(db, job.assigned_number)
        if conflict is not None and conflict.id != job.id:
            raise LeadIntakeError(
                matching.REASON_ASSIGNED_NUMBER_CONFLICT,
                f"Номер {job.assigned_number} уже используется лидом {conflict.kommo_lead_id}.",
            )
        checkpoint = await _advance(db, job, "number_confirmed")

        if _checkpoint_index(checkpoint) < _checkpoint_index("sheet_number_verified"):
            row = google_sheets_service.get_row_by_number(job.sheet_row_number or 0)
            if row is None:
                raise LeadIntakeError("sheet_row_missing", "Строка Google Sheets больше не найдена.")
            current_number = _clean(row.lead_number)
            fingerprint = None if current_number == job.assigned_number else _row_fingerprint(row)
            result = await asyncio.to_thread(
                google_sheets_service.write_internal_lead_number,
                row_number=job.sheet_row_number,
                expected_row_fingerprint=fingerprint,
                new_number=job.assigned_number,
            )
            if not result.get("verified"):
                raise LeadIntakeError(
                    "sheets_write_failed", f"Sheets: {result.get('reason', 'unknown')}"
                )
            checkpoint = await _advance(db, job, "sheet_number_written")
            checkpoint = await _advance(db, job, "sheet_number_verified")

        target_name = f"{job.assigned_number} - {qualification.product_name_ru}"[:255]
        if _checkpoint_index(checkpoint) < _checkpoint_index("kommo_renamed"):
            if _clean(details.get("name")) != target_name:
                await kommo_service.update_kommo_lead(job.kommo_lead_id, name=target_name)
            checkpoint = await _advance(db, job, "kommo_renamed")

        if _checkpoint_index(checkpoint) < _checkpoint_index("kommo_stage_moved"):
            details = await kommo_service.get_lead_details(job.kommo_lead_id)
            target_pipeline, target_status = await _resolve_first_contact_stage(details)
            update_kwargs: dict[str, Any] = {}
            if isinstance(target_pipeline, int) and details.get("pipeline_id") != target_pipeline:
                update_kwargs["pipeline_id"] = target_pipeline
            if isinstance(target_status, int) and details.get("status_id") != target_status:
                update_kwargs["status_id"] = target_status
            if update_kwargs:
                await kommo_service.update_kommo_lead(job.kommo_lead_id, **update_kwargs)
            checkpoint = await _advance(db, job, "kommo_stage_moved")

        if _checkpoint_index(checkpoint) < _checkpoint_index("kommo_note_added"):
            marker = kommo_notes.note_marker(job.kommo_lead_id, job.processing_version)
            recent_notes = await kommo_service.get_recent_common_notes(job.kommo_lead_id, limit=50)
            if not any(kommo_notes.note_has_marker(note.get("text"), marker) for note in recent_notes):
                note_text = kommo_notes.build_kommo_note(
                    snapshot=snapshot,
                    qualification=qualification,
                    kommo_lead_id=job.kommo_lead_id,
                    processing_version=job.processing_version,
                    phone_display=phone_utils.display_phone(snapshot.phone),
                )
                await kommo_service.add_common_note(job.kommo_lead_id, note_text)
            checkpoint = await _advance(db, job, "kommo_note_added")

        if _checkpoint_index(checkpoint) < _checkpoint_index("kommo_task_created"):
            # WhatsApp follow-up tasks only become active once the manager
            # confirms the message was actually sent (see confirm_message_sent).
            if qualification.recommended_action != "whatsapp":
                await _ensure_primary_task(job, qualification)
            checkpoint = await _advance(db, job, "kommo_task_created")

        job = await repository.mark_completed(db, job)
        logger.info(
            "lead_intake.completed kommo_lead_id=%s assigned_number=%s", job.kommo_lead_id, job.assigned_number
        )
        return ApplyResult(status="completed", job=job)
    except Exception as exc:  # noqa: BLE001 - saga must classify every failure
        code = getattr(exc, "code", type(exc).__name__)
        message = sanitize_text(str(exc), limit=2000) or str(exc)
        job = await repository.mark_error(db, job, error_code=str(code), error_message=message)
        logger.warning(
            "lead_intake.apply_failed kommo_lead_id=%s checkpoint=%s error_code=%s",
            job.kommo_lead_id,
            job.current_checkpoint,
            code,
        )
        return ApplyResult(status="error", job=job, error=message)


async def confirm_whatsapp_sent(db: AsyncSession, job_id: int) -> ApplyResult:
    job = await repository.get_by_id_locked(db, job_id)
    if job is None:
        raise ValueError("job_not_found")
    if job.status != "completed":
        return ApplyResult(status="not_ready", job=job)

    qualification = qualification_from_job(job)
    if qualification is None or qualification.recommended_action != "whatsapp":
        return ApplyResult(status="not_applicable", job=job)

    runtime = dict(job.runtime_state_json or {})
    if runtime.get("whatsapp_message_sent"):
        return ApplyResult(status="already_done", job=job)

    try:
        await _ensure_primary_task(job, qualification)
        runtime["whatsapp_message_sent"] = True
        runtime["whatsapp_message_sent_at"] = datetime.now(timezone.utc).isoformat()
        job = await repository.save(db, job, runtime_state_json=runtime)
        return ApplyResult(status="completed", job=job)
    except Exception as exc:  # noqa: BLE001
        message = sanitize_text(str(exc), limit=1000) or str(exc)
        return ApplyResult(status="error", job=job, error=message)


async def record_call_result(
    db: AsyncSession, job: LeadProcessingJob, *, outcome: str, details: str | None = None
) -> LeadProcessingJob:
    runtime = dict(job.runtime_state_json or {})
    recent_calls = list(runtime.get("call_results") or [])
    now_iso = datetime.now(timezone.utc).isoformat()
    if recent_calls and recent_calls[-1].get("outcome") == outcome:
        last_at = recent_calls[-1].get("at")
        if last_at and (datetime.now(timezone.utc) - datetime.fromisoformat(last_at)).total_seconds() < 30:
            return job  # debounce accidental double-taps

    note_text = kommo_notes.build_call_result_note(outcome=outcome, details=details)
    marker = f"[AUTO_CALL_RESULT:{job.kommo_lead_id}:{int(datetime.now(timezone.utc).timestamp())}]"
    await kommo_service.add_common_note(job.kommo_lead_id, f"{note_text}\n\n{marker}")

    recent_calls.append({"outcome": outcome, "at": now_iso, "details": details})
    runtime["call_results"] = recent_calls
    return await repository.save(db, job, runtime_state_json=runtime)
