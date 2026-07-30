"""PostgreSQL access for ``LeadProcessingJob`` — the source of truth.

Kept as a thin, easily-mockable layer so ``service.py`` can be unit tested
without a live database when needed, while integration tests can exercise
the real SQLAlchemy models against an in-memory database.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.lead_processing_job import LeadProcessingJob


async def get_by_kommo_lead_id(db: AsyncSession, kommo_lead_id: int) -> LeadProcessingJob | None:
    result = await db.execute(
        select(LeadProcessingJob).where(LeadProcessingJob.kommo_lead_id == kommo_lead_id)
    )
    return result.scalar_one_or_none()


async def get_by_id(db: AsyncSession, job_id: int) -> LeadProcessingJob | None:
    return await db.get(LeadProcessingJob, job_id)


async def get_by_id_locked(db: AsyncSession, job_id: int) -> LeadProcessingJob | None:
    """Row-locked fetch used at the start of ``apply``/``retry`` to serialize access."""
    result = await db.execute(
        select(LeadProcessingJob).where(LeadProcessingJob.id == job_id).with_for_update()
    )
    return result.scalar_one_or_none()


async def get_by_assigned_number(db: AsyncSession, assigned_number: str) -> LeadProcessingJob | None:
    result = await db.execute(
        select(LeadProcessingJob).where(LeadProcessingJob.assigned_number == assigned_number)
    )
    return result.scalar_one_or_none()


async def create_job(
    db: AsyncSession,
    *,
    kommo_lead_id: int,
    original_title: str | None,
    facebook_lead_id: str | None,
    facebook_technical_tag: str | None,
    source: str | None,
    raw_snapshot: dict[str, Any],
    dry_run: bool,
    processing_version: int,
) -> LeadProcessingJob:
    job = LeadProcessingJob(
        kommo_lead_id=kommo_lead_id,
        original_title=original_title,
        facebook_lead_id=facebook_lead_id,
        facebook_technical_tag=facebook_technical_tag,
        source=source,
        raw_snapshot_json=raw_snapshot,
        status="detected",
        current_checkpoint="started",
        dry_run=dry_run,
        processing_version=processing_version,
    )
    db.add(job)
    await db.commit()
    await db.refresh(job)
    return job


async def max_assigned_number(db: AsyncSession) -> int:
    result = await db.execute(select(LeadProcessingJob.assigned_number))
    best = 0
    for (value,) in result.all():
        text = str(value or "").strip()
        if text.isdigit():
            best = max(best, int(text))
    return best


async def save(db: AsyncSession, job: LeadProcessingJob, **fields: Any) -> LeadProcessingJob:
    for key, value in fields.items():
        setattr(job, key, value)
    await db.commit()
    await db.refresh(job)
    return job


async def mark_completed(db: AsyncSession, job: LeadProcessingJob) -> LeadProcessingJob:
    job.status = "completed"
    job.current_checkpoint = "completed"
    job.completed_at = datetime.now(timezone.utc)
    job.error_code = None
    job.error_message = None
    await db.commit()
    await db.refresh(job)
    return job


async def mark_skipped(db: AsyncSession, job: LeadProcessingJob) -> LeadProcessingJob:
    job.status = "skipped"
    job.completed_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(job)
    return job


async def mark_error(
    db: AsyncSession, job: LeadProcessingJob, *, error_code: str, error_message: str
) -> LeadProcessingJob:
    job.status = "error"
    job.error_code = error_code[:64]
    job.error_message = error_message[:4000]
    job.retry_count = int(job.retry_count or 0) + 1
    await db.commit()
    await db.refresh(job)
    return job


async def list_active_jobs(db: AsyncSession) -> list[LeadProcessingJob]:
    """Jobs that are not yet in a terminal state, oldest first."""
    result = await db.execute(
        select(LeadProcessingJob)
        .where(LeadProcessingJob.status.notin_(["completed", "skipped"]))
        .order_by(LeadProcessingJob.created_at.asc())
    )
    return list(result.scalars().all())
