from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import CatalogJob, CatalogJobStatus

ACTIVE_STATUSES = {CatalogJobStatus.received, CatalogJobStatus.validating, CatalogJobStatus.parsing, CatalogJobStatus.downloading_images, CatalogJobStatus.generating_content, CatalogJobStatus.rendering_pdf}


class CatalogJobRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def has_active_job(self, telegram_user_id: int) -> bool:
        stmt: Select[tuple[CatalogJob]] = select(CatalogJob).where(CatalogJob.telegram_user_id == telegram_user_id, CatalogJob.status.in_(ACTIVE_STATUSES)).limit(1)
        return (await self.session.execute(stmt)).scalar_one_or_none() is not None

    async def create(self, telegram_user_id: int, telegram_chat_id: int, source_url: str) -> CatalogJob:
        job = CatalogJob(telegram_user_id=telegram_user_id, telegram_chat_id=telegram_chat_id, source_url=source_url, status=CatalogJobStatus.received)
        self.session.add(job)
        await self.session.flush()
        return job

    async def update_status(self, job_id: uuid.UUID, status: CatalogJobStatus, **values) -> None:
        job = await self.session.get(CatalogJob, job_id)
        if job is None:
            return
        job.status = status
        for key, value in values.items():
            setattr(job, key, value)
        if status in {CatalogJobStatus.completed, CatalogJobStatus.failed}:
            job.completed_at = datetime.now(timezone.utc)
        await self.session.flush()
