from __future__ import annotations

from pathlib import Path

from aiogram import F, Router
from aiogram.types import FSInputFile, Message

from app.bot.messages import ACTIVE_JOB, AUTH_ERROR, DOCUMENT_CAPTION, INVALID_URL, LINK_RECEIVED, RATE_LIMIT, TEMPORARY_ERROR
from app.config import Settings
from app.database.session import SessionLocal
from app.parser.errors import AuthenticationRequiredError, InvalidProductUrlError
from app.parser.url_validator import validate_product_url
from app.services.cleanup_service import CleanupService
from app.services.task_service import CatalogTaskService, TaskLimiter

router = Router()


@router.message(F.text.startswith("http"))
async def product_link(message: Message, settings: Settings, task_limiter: TaskLimiter) -> None:
    user_id = message.from_user.id if message.from_user else 0
    chat_id = message.chat.id
    source_url = message.text.strip()
    if not task_limiter.check_rate_limit(user_id):
        await message.answer(RATE_LIMIT)
        return
    try:
        await validate_product_url(source_url)
    except InvalidProductUrlError:
        await message.answer(INVALID_URL)
        return

    async with SessionLocal() as session:
        service = CatalogTaskService(settings, task_limiter)
        job = await service.create_job_if_allowed(session, user_id, chat_id, source_url)
    if job is None:
        await message.answer(ACTIVE_JOB)
        return

    status_message = await message.answer(LINK_RECEIVED)

    async def update_status(text: str) -> None:
        await status_message.edit_text(text)

    try:
        service = CatalogTaskService(settings, task_limiter)
        pdf_path = await service.run_job(SessionLocal, job.id, source_url, update_status)
        await message.answer_document(FSInputFile(Path(pdf_path)), caption=DOCUMENT_CAPTION)
        await CleanupService(settings).cleanup_job_temporary(settings.temporary_dir / str(job.id), keep_pdf=False)
    except AuthenticationRequiredError:
        await status_message.edit_text(AUTH_ERROR)
    except Exception:
        await status_message.edit_text(TEMPORARY_ERROR)


@router.message()
async def unknown_text(message: Message) -> None:
    await message.answer(INVALID_URL)
