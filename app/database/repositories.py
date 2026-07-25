"""Database repositories."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import CatalogJob, JobStatus


class CatalogJobRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(
        self,
        telegram_user_id: int,
        telegram_chat_id: int,
        source_url: str,
    ) -> CatalogJob:
        job = CatalogJob(
            telegram_user_id=telegram_user_id,
            telegram_chat_id=telegram_chat_id,
            source_url=source_url,
            status=JobStatus.RECEIVED.value,
        )
        self.session.add(job)
        await self.session.commit()
        await self.session.refresh(job)
        return job

    async def get_by_id(self, job_id: uuid.UUID) -> CatalogJob | None:
        result = await self.session.execute(select(CatalogJob).where(CatalogJob.id == job_id))
        return result.scalar_one_or_none()

    async def get_active_for_user(self, telegram_user_id: int) -> CatalogJob | None:
        active_statuses = [
            JobStatus.RECEIVED.value,
            JobStatus.VALIDATING.value,
            JobStatus.PARSING.value,
            JobStatus.DOWNLOADING_IMAGES.value,
            JobStatus.GENERATING_CONTENT.value,
            JobStatus.RENDERING_PDF.value,
        ]
        result = await self.session.execute(
            select(CatalogJob)
            .where(CatalogJob.telegram_user_id == telegram_user_id)
            .where(CatalogJob.status.in_(active_statuses))
            .order_by(CatalogJob.created_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def update_status(
        self,
        job_id: uuid.UUID,
        status: JobStatus,
        *,
        product_title: str | None = None,
        output_file: str | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> None:
        values: dict = {"status": status.value, "updated_at": datetime.now(timezone.utc)}
        if product_title is not None:
            values["product_title"] = product_title
        if output_file is not None:
            values["output_file"] = output_file
        if error_code is not None:
            values["error_code"] = error_code
        if error_message is not None:
            values["error_message"] = error_message
        if status in (JobStatus.COMPLETED, JobStatus.FAILED):
            values["completed_at"] = datetime.now(timezone.utc)

        await self.session.execute(update(CatalogJob).where(CatalogJob.id == job_id).values(**values))
        await self.session.commit()
