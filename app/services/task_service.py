from __future__ import annotations

import uuid
from typing import Optional

from app.database.models import CatalogJob
from app.database.repositories import CatalogJobRepository
from app.database.session import get_session
from app.exceptions import JobAlreadyActiveError
from app.logging_config import get_logger

logger = get_logger(__name__)


class TaskService:
    async def ensure_no_active_job(self, telegram_user_id: int) -> None:
        async with get_session() as session:
            repo = CatalogJobRepository(session)
            active = await repo.get_active_job_for_user(telegram_user_id)
            if active:
                raise JobAlreadyActiveError(
                    f"User {telegram_user_id} already has active job {active.id}"
                )

    async def create_job(
        self,
        telegram_user_id: int,
        telegram_chat_id: int,
        source_url: str,
    ) -> CatalogJob:
        async with get_session() as session:
            repo = CatalogJobRepository(session)
            job = await repo.create(
                telegram_user_id=telegram_user_id,
                telegram_chat_id=telegram_chat_id,
                source_url=source_url,
            )
            logger.info(
                "job_created",
                job_id=str(job.id),
                user_id=telegram_user_id,
                url=source_url[:80],
            )
            return job

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
        async with get_session() as session:
            repo = CatalogJobRepository(session)
            await repo.update_status(
                job_id,
                status,
                product_title=product_title,
                output_file=output_file,
                error_code=error_code,
                error_message=error_message,
            )
        logger.debug("job_status_updated", job_id=str(job_id), status=status)
