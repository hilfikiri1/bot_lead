from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import messages
from app.config import get_settings
from app.database.models import CatalogJobStatus
from app.database.repositories import CatalogJobRepository
from app.database.session import SessionLocal
from app.exceptions import InvalidProductUrlError
from app.parser.image_downloader import download_and_prepare_images
from app.parser.parser_1688 import parse_1688_product
from app.parser.url_validator import resolve_and_validate_redirects
from app.services.catalog_service import CatalogService
from app.services.cleanup_service import CleanupService


@dataclass
class TaskResult:
    pdf_path: str


class _RateLimiter:
    def __init__(self, max_per_minute: int) -> None:
        self.max_per_minute = max_per_minute
        self._history: dict[int, deque[datetime]] = {}

    def check(self, user_id: int) -> bool:
        now = datetime.utcnow()
        history = self._history.setdefault(user_id, deque())
        while history and now - history[0] > timedelta(minutes=1):
            history.popleft()
        if len(history) >= self.max_per_minute:
            return False
        history.append(now)
        return True


class TaskService:
    _active_users: set[int] = set()
    _global_semaphore: asyncio.Semaphore | None = None

    def __init__(self) -> None:
        self.settings = get_settings()
        if self.__class__._global_semaphore is None:
            self.__class__._global_semaphore = asyncio.Semaphore(self.settings.max_concurrent_jobs)
        self.catalog_service = CatalogService()
        self.cleanup_service = CleanupService()
        self.rate_limiter = _RateLimiter(self.settings.rate_limit_per_minute)

    async def _with_session(self) -> AsyncSession:
        return SessionLocal()

    async def process_link(
        self,
        telegram_user_id: int,
        telegram_chat_id: int,
        raw_link: str,
        update_status: Callable[[str], Awaitable],
    ) -> TaskResult:
        if telegram_user_id in self._active_users:
            raise RuntimeError(messages.ALREADY_RUNNING)
        if not self.rate_limiter.check(telegram_user_id):
            raise RuntimeError(messages.TEMPORARY_ERROR)

        self._active_users.add(telegram_user_id)
        session = await self._with_session()
        repo = CatalogJobRepository(session)
        job = await repo.create(telegram_user_id, telegram_chat_id, raw_link)
        job_dir = Path(self.settings.storage_temp_dir) / str(job.id)
        job_dir.mkdir(parents=True, exist_ok=True)

        try:
            await repo.update_status(job.id, CatalogJobStatus.validating)
            validated_url = await resolve_and_validate_redirects(raw_link)

            await update_status(messages.STATUS_OPENING)
            await repo.update_status(job.id, CatalogJobStatus.parsing)
            async with self._global_semaphore:
                parsed = await parse_1688_product(validated_url, job_dir)

            await update_status(messages.STATUS_DOWNLOADING_IMAGES)
            await repo.update_status(job.id, CatalogJobStatus.downloading_images)
            all_urls = parsed.gallery_image_urls[:8] + parsed.detail_image_urls[:4]
            local_paths = await download_and_prepare_images(all_urls, job_dir / "images", str(parsed.source_url))
            parsed.local_image_paths = local_paths

            await update_status(messages.STATUS_GENERATING_CONTENT)
            await repo.update_status(job.id, CatalogJobStatus.generating_content, product_title=parsed.title_zh)

            await update_status(messages.STATUS_RENDERING)
            await repo.update_status(job.id, CatalogJobStatus.rendering_pdf)
            output_pdf = await self.catalog_service.build_catalog(parsed, job_dir)

            await repo.update_status(
                job.id,
                CatalogJobStatus.completed,
                output_file=str(output_pdf),
                completed=True,
            )
            self.cleanup_service.cleanup_job_temp(job_dir)
            self.cleanup_service.cleanup_old_output_files()
            return TaskResult(pdf_path=str(output_pdf))
        except InvalidProductUrlError as exc:
            await repo.update_status(job.id, CatalogJobStatus.failed, error_code="invalid_url", error_message=str(exc))
            raise
        except Exception as exc:
            await repo.update_status(job.id, CatalogJobStatus.failed, error_code="runtime", error_message=str(exc))
            raise
        finally:
            await session.close()
            self._active_users.discard(telegram_user_id)
