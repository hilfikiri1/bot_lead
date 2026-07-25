from __future__ import annotations

import asyncio
import time
from pathlib import Path

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.database.models import CatalogJobStatus
from app.database.repositories import CatalogJobRepository
from app.parser.errors import CatalogBotError
from app.services.catalog_service import CatalogService

logger = structlog.get_logger(__name__)


class TaskLimiter:
    def __init__(self, settings: Settings):
        self.browser_semaphore = asyncio.Semaphore(settings.max_concurrent_jobs)
        self.request_rate_limit_seconds = settings.request_rate_limit_seconds
        self._last_request_by_user: dict[int, float] = {}

    def check_rate_limit(self, user_id: int) -> bool:
        now = time.monotonic()
        last = self._last_request_by_user.get(user_id, 0.0)
        if now - last < self.request_rate_limit_seconds:
            return False
        self._last_request_by_user[user_id] = now
        return True


class CatalogTaskService:
    def __init__(self, settings: Settings, limiter: TaskLimiter):
        self.settings = settings
        self.limiter = limiter
        self.catalog_service = CatalogService(settings)

    async def create_job_if_allowed(self, session: AsyncSession, telegram_user_id: int, telegram_chat_id: int, source_url: str):
        repo = CatalogJobRepository(session)
        if await repo.has_active_job(telegram_user_id):
            return None
        job = await repo.create(telegram_user_id, telegram_chat_id, source_url)
        await session.commit()
        return job

    async def run_job(self, session_factory, job_id, source_url: str, status_callback=None) -> Path:
        job_dir = self.settings.temporary_dir / str(job_id)
        job_dir.mkdir(parents=True, exist_ok=True)
        async with session_factory() as session:
            repo = CatalogJobRepository(session)
            try:
                await repo.update_status(job_id, CatalogJobStatus.validating)
                await session.commit()
                async with self.limiter.browser_semaphore:
                    await repo.update_status(job_id, CatalogJobStatus.parsing)
                    await session.commit()
                    pdf = await self.catalog_service.build_from_url(source_url, job_dir, status_callback)
                await repo.update_status(job_id, CatalogJobStatus.completed, output_file=str(pdf))
                await session.commit()
                return pdf
            except CatalogBotError as exc:
                await repo.update_status(job_id, CatalogJobStatus.failed, error_code=exc.error_code, error_message=str(exc))
                await session.commit()
                logger.exception("catalog_job_failed", job_id=str(job_id), error_code=exc.error_code)
                raise
            except Exception as exc:
                await repo.update_status(job_id, CatalogJobStatus.failed, error_code="unexpected_error", error_message=str(exc))
                await session.commit()
                logger.exception("catalog_job_unexpected_failed", job_id=str(job_id))
                raise
