"""Handler for incoming product links."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.types import Message

from app.bot import messages
from app.exceptions import CatalogError
from app.logging_config import get_logger
from app.parser.url_validator import validate_url_syntax
from app.services.task_service import TaskService

logger = get_logger(__name__)

router = Router(name="product_link")


@router.message(F.text & ~F.text.startswith("/"))
async def handle_product_link(message: Message, task_service: TaskService) -> None:
    text = (message.text or "").strip()
    user_id = message.from_user.id

    # Fast syntactic validation (full SSRF resolution happens in the pipeline).
    try:
        validate_url_syntax(text)
    except CatalogError:
        await message.answer(messages.INVALID_URL)
        return

    if task_service.is_rate_limited(user_id):
        await message.answer(messages.RATE_LIMITED)
        return

    if await task_service.has_active_job(user_id):
        await message.answer(messages.ALREADY_RUNNING)
        return

    logger.info("Accepted product link", user_id=user_id)
    await task_service.start_job(message, text)
