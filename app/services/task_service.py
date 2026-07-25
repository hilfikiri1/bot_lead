"""Task orchestration with concurrency control."""

from __future__ import annotations

import asyncio
import time
import uuid
from collections import defaultdict
from pathlib import Path

from aiogram import Bot
from aiogram.types import FSInputFile

from app.config import Settings, get_settings
from app.database.repositories import CatalogJobRepository
from app.database.session import get_async_session_factory
from app.exceptions import CatalogBotError, JobAlreadyRunningError, RateLimitError
from app.logging_config import get_logger
from app.services.catalog_service import CatalogService
from app.services.cleanup_service import CleanupService

logger = get_logger(__name__)


class TaskService:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.catalog_service = CatalogService(settings=self.settings)
        self.cleanup_service = CleanupService(settings=self.settings)
        self._semaphore = asyncio.Semaphore(self.settings.max_concurrent_jobs)
        self._user_last_request: dict[int, float] = defaultdict(float)

    def check_rate_limit(self, user_id: int) -> None:
        now = time.time()
        last = self._user_last_request[user_id]
        if now - last < self.settings.rate_limit_seconds:
            raise RateLimitError()
        self._user_last_request[user_id] = now

    async def run_catalog_job(
        self,
        bot: Bot,
        user_id: int,
        chat_id: int,
        source_url: str,
        status_message_id: int,
    ) -> None:
        self.check_rate_limit(user_id)

        async with get_async_session_factory() as session:
            repo = CatalogJobRepository(session)
            active = await repo.get_active_for_user(user_id)
            if active:
                raise JobAlreadyRunningError()
            job = await repo.create(user_id, chat_id, source_url)
            job_id = job.id

        async def update_status(text: str) -> None:
            try:
                await bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=status_message_id,
                    text=text,
                )
            except Exception:
                pass

        pdf_path: Path | None = None
        try:
            async with self._semaphore:
                async with get_async_session_factory() as session:
                    repo = CatalogJobRepository(session)

                    async def status_cb(message: str) -> None:
                        await update_status(message)

                    pdf_path = await self.catalog_service.process(
                        job_id,
                        source_url,
                        repo,
                        status_callback=status_cb,
                    )
        except CatalogBotError:
            raise
        except Exception as exc:
            logger.exception("task_failed", job_id=str(job_id))
            raise CatalogBotError(str(exc)) from exc

        if pdf_path is None:
            raise CatalogBotError("PDF not generated")

        caption = (
            "Каталог сформирован автоматически на основании информации поставщика с 1688.com. "
            "Цена и наличие требуют подтверждения перед оформлением заказа."
        )
        await bot.send_document(
            chat_id=chat_id,
            document=FSInputFile(pdf_path),
            caption=caption,
        )

        await self.cleanup_service.cleanup_job(job_id)
