from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import CatalogJob, CatalogJobStatus


class CatalogJobRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(self, telegram_user_id: int, telegram_chat_id: int, source_url: str) -> CatalogJob:
        job = CatalogJob(
            telegram_user_id=telegram_user_id,
            telegram_chat_id=telegram_chat_id,
            source_url=source_url,
            status=CatalogJobStatus.received,
        )
        self.session.add(job)
        await self.session.commit()
        await self.session.refresh(job)
        return job

    async def update_status(
        self,
        job_id: UUID,
        status: CatalogJobStatus,
        *,
        product_title: str | None = None,
        output_file: str | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        completed: bool = False,
    ) -> None:
        job = await self.session.get(CatalogJob, job_id)
        if not job:
            return
        job.status = status
        if product_title is not None:
            job.product_title = product_title
        if output_file is not None:
            job.output_file = output_file
        if error_code is not None:
            job.error_code = error_code
        if error_message is not None:
            job.error_message = error_message
        if completed:
            job.completed_at = datetime.utcnow()
        await self.session.commit()

    async def get_active_for_user(self, telegram_user_id: int) -> CatalogJob | None:
        active_statuses = {
            CatalogJobStatus.received,
            CatalogJobStatus.validating,
            CatalogJobStatus.parsing,
            CatalogJobStatus.downloading_images,
            CatalogJobStatus.generating_content,
            CatalogJobStatus.rendering_pdf,
        }
        stmt: Select[tuple[CatalogJob]] = (
            select(CatalogJob)
            .where(CatalogJob.telegram_user_id == telegram_user_id)
            .where(CatalogJob.status.in_(active_statuses))
            .order_by(CatalogJob.created_at.desc())
            .limit(1)
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()
