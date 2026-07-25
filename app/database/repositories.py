from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import CatalogJob


class CatalogJobRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        telegram_user_id: int,
        telegram_chat_id: int,
        source_url: str,
    ) -> CatalogJob:
        job = CatalogJob(
            id=uuid.uuid4(),
            telegram_user_id=telegram_user_id,
            telegram_chat_id=telegram_chat_id,
            source_url=source_url,
            status="received",
        )
        self._session.add(job)
        await self._session.flush()
        return job

    async def get_by_id(self, job_id: uuid.UUID) -> Optional[CatalogJob]:
        result = await self._session.execute(
            select(CatalogJob).where(CatalogJob.id == job_id)
        )
        return result.scalar_one_or_none()

    async def get_active_job_for_user(
        self, telegram_user_id: int
    ) -> Optional[CatalogJob]:
        result = await self._session.execute(
            select(CatalogJob).where(
                CatalogJob.telegram_user_id == telegram_user_id,
                CatalogJob.status.in_(CatalogJob.ACTIVE_STATUSES),
            )
        )
        return result.scalar_one_or_none()

    async def update_status(
        self,
        job_id: uuid.UUID,
        status: str,
        *,
        product_title: Optional[str] = None,
        output_file: Optional[str] = None,
        error_code: Optional[str] = None,
        error_message: Optional[str] = None,
    ) -> None:
        values: dict = {
            "status": status,
            "updated_at": datetime.now(timezone.utc),
        }
        if product_title is not None:
            values["product_title"] = product_title
        if output_file is not None:
            values["output_file"] = output_file
        if error_code is not None:
            values["error_code"] = error_code
        if error_message is not None:
            values["error_message"] = error_message
        if status in ("completed", "failed"):
            values["completed_at"] = datetime.now(timezone.utc)

        await self._session.execute(
            update(CatalogJob).where(CatalogJob.id == job_id).values(**values)
        )
