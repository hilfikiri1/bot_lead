"""Repository helpers for :class:`CatalogJob`."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import ACTIVE_STATUSES, CatalogJob, JobStatus


class CatalogJobRepository:
    """CRUD/query operations for catalog jobs."""

    def __init__(self, session: AsyncSession):
        self._session = session

    async def create(
        self, *, telegram_user_id: int, telegram_chat_id: int, source_url: str
    ) -> CatalogJob:
        job = CatalogJob(
            telegram_user_id=telegram_user_id,
            telegram_chat_id=telegram_chat_id,
            source_url=source_url,
            status=JobStatus.RECEIVED.value,
        )
        self._session.add(job)
        await self._session.flush()
        return job

    async def get(self, job_id: uuid.UUID) -> CatalogJob | None:
        return await self._session.get(CatalogJob, job_id)

    async def count_active_for_user(self, telegram_user_id: int) -> int:
        stmt = (
            select(func.count())
            .select_from(CatalogJob)
            .where(
                CatalogJob.telegram_user_id == telegram_user_id,
                CatalogJob.status.in_([s.value for s in ACTIVE_STATUSES]),
            )
        )
        result = await self._session.execute(stmt)
        return int(result.scalar_one())

    async def has_active_for_user(self, telegram_user_id: int) -> bool:
        return (await self.count_active_for_user(telegram_user_id)) > 0

    async def set_status(
        self, job_id: uuid.UUID, status: JobStatus, **fields
    ) -> None:
        values: dict = {"status": status.value}
        values.update(fields)
        stmt = update(CatalogJob).where(CatalogJob.id == job_id).values(**values)
        await self._session.execute(stmt)

    async def mark_completed(
        self, job_id: uuid.UUID, *, output_file: str, product_title: str | None
    ) -> None:
        await self.set_status(
            job_id,
            JobStatus.COMPLETED,
            output_file=output_file,
            product_title=product_title,
            completed_at=datetime.now(UTC),
        )

    async def mark_failed(
        self, job_id: uuid.UUID, *, error_code: str, error_message: str
    ) -> None:
        await self.set_status(
            job_id,
            JobStatus.FAILED,
            error_code=error_code,
            error_message=error_message[:1000],
            completed_at=datetime.now(UTC),
        )

    async def clear_output_file(self, job_id: uuid.UUID) -> None:
        stmt = update(CatalogJob).where(CatalogJob.id == job_id).values(output_file=None)
        await self._session.execute(stmt)

    async def find_expired(self, older_than: datetime) -> list[CatalogJob]:
        stmt = select(CatalogJob).where(
            CatalogJob.status == JobStatus.COMPLETED.value,
            CatalogJob.completed_at.is_not(None),
            CatalogJob.completed_at < older_than,
            CatalogJob.output_file.is_not(None),
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())
