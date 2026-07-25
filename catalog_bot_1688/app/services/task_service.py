"""Coordinates catalog jobs: DB lifecycle, concurrency limits, Telegram I/O.

Guarantees:
* at most one active job per Telegram user (checked in the DB);
* a global semaphore bounds concurrent browser jobs (``MAX_CONCURRENT_JOBS``);
* a simple per-user rate limit;
* the PDF file is only deleted after Telegram finished sending it;
* temporary artifacts (HTML, images, debug) are removed after each job.
"""

from __future__ import annotations

import asyncio
import shutil
import time
import uuid
from pathlib import Path

from aiogram import Bot
from aiogram.types import FSInputFile, Message

from app.bot import messages
from app.config import Settings
from app.database.models import JobStatus
from app.database.repositories import CatalogJobRepository
from app.database.session import session_scope
from app.exceptions import CatalogError
from app.logging_config import get_logger
from app.services.catalog_service import CatalogService

logger = get_logger(__name__)


class TaskService:
    """Owns concurrency primitives and drives a job to completion."""

    def __init__(self, settings: Settings, bot: Bot):
        self._settings = settings
        self._bot = bot
        self._semaphore = asyncio.Semaphore(max(1, settings.max_concurrent_jobs))
        self._user_last_request: dict[int, float] = {}
        self._catalog = CatalogService(settings)
        self._background_tasks: set[asyncio.Task] = set()

    # ---- gate keeping ---------------------------------------------------- #
    def is_rate_limited(self, user_id: int) -> bool:
        now = time.monotonic()
        last = self._user_last_request.get(user_id)
        if last is not None and (now - last) < self._settings.rate_limit_seconds:
            return True
        self._user_last_request[user_id] = now
        return False

    async def has_active_job(self, user_id: int) -> bool:
        async with session_scope() as session:
            repo = CatalogJobRepository(session)
            return await repo.has_active_for_user(user_id)

    # ---- entry point ----------------------------------------------------- #
    async def start_job(self, message: Message, source_url: str) -> None:
        """Validate gating, create the job and spawn background processing."""
        user_id = message.from_user.id
        chat_id = message.chat.id

        status_message = await message.answer(messages.LINK_RECEIVED)

        async with session_scope() as session:
            repo = CatalogJobRepository(session)
            job = await repo.create(
                telegram_user_id=user_id,
                telegram_chat_id=chat_id,
                source_url=source_url,
            )
            job_id = job.id

        task = asyncio.create_task(
            self._process(job_id, user_id, chat_id, source_url, status_message)
        )
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    # ---- processing ------------------------------------------------------ #
    async def _process(
        self,
        job_id: uuid.UUID,
        user_id: int,
        chat_id: int,
        source_url: str,
        status_message: Message,
    ) -> None:
        work_dir = self._settings.temporary_path / str(job_id)
        pdf_path: Path | None = None

        if self._semaphore.locked():
            await self._safe_edit(status_message, messages.SERVER_BUSY)

        async with self._semaphore:
            try:
                result = await self._catalog.build_catalog(
                    source_url,
                    work_dir=work_dir,
                    output_dir=self._settings.output_path,
                    on_status=lambda status: self._report(job_id, status, status_message),
                )
                pdf_path = result.pdf_path

                await self._safe_edit(status_message, messages.DONE)
                await self._send_pdf(chat_id, pdf_path)

                async with session_scope() as session:
                    repo = CatalogJobRepository(session)
                    await repo.mark_completed(
                        job_id,
                        output_file=str(pdf_path),
                        product_title=result.product_title_ru,
                    )
                logger.info("Job completed", job_id=str(job_id))
            except CatalogError as exc:
                logger.warning(
                    "Job failed", job_id=str(job_id), code=exc.error_code, error=str(exc)
                )
                await self._fail(job_id, status_message, exc.error_code, str(exc), exc.user_message)
            except Exception as exc:  # noqa: BLE001 - never leak tracebacks to user
                logger.exception("Unexpected job failure", job_id=str(job_id))
                await self._fail(
                    job_id,
                    status_message,
                    "internal_error",
                    str(exc),
                    "Не удалось сформировать каталог из-за временной ошибки. "
                    "Попробуйте повторить запрос позже.",
                )
            finally:
                # PDF has already been sent (or job failed) — safe to clean temp.
                self._cleanup_temp(work_dir)

    async def _send_pdf(self, chat_id: int, pdf_path: Path) -> None:
        document = FSInputFile(str(pdf_path), filename=pdf_path.name)
        await self._bot.send_document(
            chat_id=chat_id,
            document=document,
            caption=messages.DOCUMENT_CAPTION,
        )

    async def _report(
        self, job_id: uuid.UUID, status: JobStatus, status_message: Message
    ) -> None:
        async with session_scope() as session:
            repo = CatalogJobRepository(session)
            await repo.set_status(job_id, status)
        await self._safe_edit(status_message, messages.status_text(status))

    async def _fail(
        self,
        job_id: uuid.UUID,
        status_message: Message,
        error_code: str,
        error_message: str,
        user_message: str,
    ) -> None:
        async with session_scope() as session:
            repo = CatalogJobRepository(session)
            await repo.mark_failed(job_id, error_code=error_code, error_message=error_message)
        await self._safe_edit(status_message, user_message)

    async def _safe_edit(self, status_message: Message, text: str) -> None:
        try:
            await status_message.edit_text(text)
        except Exception as exc:  # noqa: BLE001 - editing must never crash the job
            logger.debug("Failed to edit status message", error=str(exc))

    def _cleanup_temp(self, work_dir: Path) -> None:
        try:
            if work_dir.exists():
                shutil.rmtree(work_dir, ignore_errors=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to remove temp dir", error=str(exc))
