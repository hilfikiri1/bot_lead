from __future__ import annotations

import asyncio

from aiogram import Router
from aiogram.filters import Filter
from aiogram.types import Message

from app.bot.messages import (
    INVALID_URL,
    JOB_ALREADY_ACTIVE,
    LINK_RECEIVED,
    PDF_CAPTION,
    STATUS_DONE,
    STATUS_DOWNLOADING_IMAGES,
    STATUS_OPENING,
    STATUS_RENDERING_PDF,
    STATUS_TRANSLATING,
)
from app.config import settings
from app.exceptions import (
    AuthenticationRequiredError,
    CaptchaDetectedError,
    CatalogBotError,
    InvalidProductUrlError,
    JobAlreadyActiveError,
    UnsupportedDomainError,
)
from app.logging_config import get_logger
from app.parser.url_validator import URLValidator
from app.services.catalog_service import CatalogService
from app.services.task_service import TaskService
from app.utils.filenames import safe_pdf_filename

logger = get_logger(__name__)

router = Router(name="product_link")

# Global semaphore shared across all users
_browser_semaphore = asyncio.Semaphore(settings.max_concurrent_jobs)

url_validator = URLValidator()


class IsURL(Filter):
    async def __call__(self, message: Message) -> bool:
        return bool(message.text and (
            message.text.startswith("http://") or message.text.startswith("https://")
        ))


@router.message(IsURL())
async def handle_product_link(message: Message) -> None:
    user_id = message.from_user.id if message.from_user else 0
    chat_id = message.chat.id
    raw_url = (message.text or "").strip()

    logger.info("product_link_received", user_id=user_id, url=raw_url[:100])

    # Validate URL first
    try:
        validated_url = await url_validator.validate(raw_url)
    except (InvalidProductUrlError, UnsupportedDomainError) as exc:
        await message.answer(INVALID_URL)
        return

    task_service = TaskService()

    # Check for existing active job
    try:
        await task_service.ensure_no_active_job(user_id)
    except JobAlreadyActiveError:
        await message.answer(JOB_ALREADY_ACTIVE)
        return

    # Create job record
    job = await task_service.create_job(
        telegram_user_id=user_id,
        telegram_chat_id=chat_id,
        source_url=validated_url,
    )

    status_msg = await message.answer(LINK_RECEIVED)

    async def update_status(text: str) -> None:
        try:
            await status_msg.edit_text(text)
        except Exception:
            pass

    async with _browser_semaphore:
        catalog_service = CatalogService(job_id=job.id, task_service=task_service)
        try:
            await update_status(STATUS_OPENING)
            await task_service.update_status(job.id, "parsing")
            parsed_product = await catalog_service.fetch_product(validated_url)

            await update_status(STATUS_DOWNLOADING_IMAGES)
            await task_service.update_status(job.id, "downloading_images")
            await catalog_service.download_images(parsed_product)

            await update_status(STATUS_TRANSLATING)
            await task_service.update_status(job.id, "generating_content")
            catalog_content = await catalog_service.generate_content(parsed_product)

            await update_status(STATUS_RENDERING_PDF)
            await task_service.update_status(job.id, "rendering_pdf")
            pdf_path = await catalog_service.render_pdf(parsed_product, catalog_content)

            await update_status(STATUS_DONE)
            await task_service.update_status(job.id, "completed", output_file=str(pdf_path))

            filename = safe_pdf_filename(catalog_content.product_name_ru)

            with open(pdf_path, "rb") as f:
                await message.answer_document(
                    document=f,  # type: ignore[arg-type]
                    filename=filename,
                    caption=PDF_CAPTION,
                )

        except CatalogBotError as exc:
            logger.warning(
                "catalog_bot_error",
                user_id=user_id,
                job_id=str(job.id),
                error=type(exc).__name__,
                detail=str(exc),
            )
            await task_service.update_status(
                job.id, "failed",
                error_code=type(exc).__name__,
                error_message=str(exc),
            )
            await update_status(exc.user_message)

        except Exception as exc:
            logger.exception(
                "unexpected_error",
                user_id=user_id,
                job_id=str(job.id),
            )
            await task_service.update_status(
                job.id, "failed",
                error_code="UnexpectedError",
                error_message=str(exc),
            )
            await update_status(
                "Не удалось сформировать каталог из-за временной ошибки. "
                "Попробуйте повторить запрос позже."
            )

        finally:
            await catalog_service.cleanup_temp_files()
