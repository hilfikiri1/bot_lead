"""Celery task for 1688 catalog PDF generation."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from app.catalog_exceptions import CatalogBotError
from app.celery_app import celery_app
from app.config import get_settings
from app.database import AsyncSessionLocal
from app.services.catalog_service import CatalogService
from app.services import telegram_service

logger = logging.getLogger(__name__)
settings = get_settings()

PDF_CAPTION = (
    "Каталог сформирован автоматически на основании информации поставщика с 1688.com. "
    "Цена и наличие требуют подтверждения перед оформлением заказа."
)


def _run(coro):
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    return loop.run_until_complete(coro)


async def _process_catalog_async(
    job_id: str,
    source_url: str,
    chat_id: int,
    status_message_id: int,
) -> None:
    service = CatalogService()
    import uuid

    job_uuid = uuid.UUID(job_id)

    async def status_callback(text: str) -> None:
        try:
            await telegram_service.edit_message_text(chat_id, status_message_id, text)
        except Exception:
            pass

    async with AsyncSessionLocal() as db:
        try:
            pdf_path = await service.process(
                db,
                job_uuid,
                source_url,
                status_callback=status_callback,
            )
        except CatalogBotError as exc:
            await telegram_service.edit_message_text(chat_id, status_message_id, exc.user_message)
            return
        except Exception as exc:
            logger.exception("catalog_task_failed job_id=%s", job_id)
            await telegram_service.edit_message_text(
                chat_id,
                status_message_id,
                "Не удалось сформировать каталог из-за временной ошибки. Попробуйте повторить запрос позже.",
            )
            return

    pdf_bytes = Path(pdf_path).read_bytes()
    await telegram_service.send_pdf_document(
        chat_id,
        filename=Path(pdf_path).name,
        content=pdf_bytes,
        caption=PDF_CAPTION,
    )
    service.cleanup_job(job_uuid)


@celery_app.task(
    bind=True,
    name="app.tasks.catalog_tasks.process_catalog",
    max_retries=1,
    default_retry_delay=30,
    acks_late=True,
    soft_time_limit=20 * 60,
    time_limit=22 * 60,
)
def process_catalog(
    self,
    job_id: str,
    source_url: str,
    chat_id: int,
    status_message_id: int,
) -> None:
    _run(_process_catalog_async(job_id, source_url, chat_id, status_message_id))


async def process_catalog_async(
    job_id: str,
    source_url: str,
    chat_id: int,
    status_message_id: int,
) -> None:
    await _process_catalog_async(job_id, source_url, chat_id, status_message_id)
