"""Persistent state machine for the Facebook lead intake pipeline.

``LeadProcessingJob`` is the source of truth for processing progress: every
operation (matching, number allocation, AI qualification, Kommo/Sheets
writes) reads and writes through this table so that a restart, a retried
Telegram callback, or a re-run of the detection scan never repeats an
already-completed external side effect.

``LeadNumberCounter`` backs the concurrency-safe sequential internal-number
allocator (see ``app.services.lead_intake.numbering``): a single row per
counter name, mutated under ``pg_advisory_xact_lock`` + ``SELECT ... FOR
UPDATE`` so two concurrent workers can never hand out the same number.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, DateTime, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from app.database import Base

# Kept in sync with the Telegram/AI-facing status vocabulary described in
# LEAD_INTAKE.md. The DB record is the source of truth for progress, so this
# list is intentionally exhaustive rather than open-ended.
LEAD_PROCESSING_STATUSES = (
    "detected",
    "matching",
    "matched",
    "number_assigned",
    "ai_generated",
    "waiting_approval",
    "applying",
    "completed",
    "skipped",
    "manual_match_required",
    "error",
)

# Ordered saga checkpoints written to ``current_checkpoint`` as each apply
# step succeeds. Kept as free-form string (not an enum) because the saga can
# gain steps without a migration.
LEAD_PROCESSING_CHECKPOINTS = (
    "started",
    "lead_verified",
    "number_confirmed",
    "sheet_number_written",
    "sheet_number_verified",
    "kommo_renamed",
    "kommo_stage_moved",
    "kommo_note_added",
    "kommo_task_created",
    "completed",
)


class LeadProcessingJob(Base):
    __tablename__ = "lead_processing_jobs"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    # Permanent identity. Never re-derive processing from the mutable Kommo
    # title again once this row exists.
    kommo_lead_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    facebook_lead_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    source: Mapped[str | None] = mapped_column(String(64), nullable=True)
    facebook_technical_tag: Mapped[str | None] = mapped_column(Text, nullable=True)
    original_title: Mapped[str | None] = mapped_column(Text, nullable=True)

    sheet_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    sheet_row_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sheet_row_key: Mapped[str | None] = mapped_column(String(255), nullable=True)

    assigned_number: Mapped[str | None] = mapped_column(
        String(16), unique=True, nullable=True, index=True
    )

    status: Mapped[str] = mapped_column(String(32), index=True, default="detected")
    current_checkpoint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    processing_version: Mapped[int] = mapped_column(Integer, default=1)

    match_method: Mapped[str | None] = mapped_column(String(32), nullable=True)
    match_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    match_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)

    dry_run: Mapped[bool] = mapped_column(default=False)

    raw_snapshot_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    ai_payload_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    edited_payload_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    manual_candidates_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    runtime_state_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    telegram_chat_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    telegram_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    telegram_user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint(
            "status IN ("
            "'detected','matching','matched','number_assigned','ai_generated',"
            "'waiting_approval','applying','completed','skipped',"
            "'manual_match_required','error')",
            name="ck_lead_processing_jobs_status",
        ),
    )


class LeadNumberCounter(Base):
    """Single-row-per-counter table backing safe sequential number allocation."""

    __tablename__ = "lead_number_counters"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    counter_name: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    last_number: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
