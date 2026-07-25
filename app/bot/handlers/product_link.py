"""Product link handler."""

from __future__ import annotations

import re

from aiogram import Bot, Router
from aiogram.types import Message

from app.bot.messages import INVALID_URL, LINK_RECEIVED, STATUS_OPENING
from app.exceptions import CatalogBotError
from app.logging_config import get_logger
from app.parser.url_validator import validate_url_format
from app.services.task_service import TaskService

router = Router()
logger = get_logger(__name__)

URL_PATTERN = re.compile(r"https?://[^\s]+", re.I)

task_service = TaskService()


@router.message()
async def handle_message(message: Message, bot: Bot) -> None:
    if not message.text:
        return

    match = URL_PATTERN.search(message.text.strip())
    if not match:
        return

    url = match.group(0).rstrip(".,;)")
    try:
        validate_url_format(url)
    except CatalogBotError:
        await message.answer(INVALID_URL)
        return

    status_msg = await message.answer(LINK_RECEIVED)
    await bot.edit_message_text(
        chat_id=message.chat.id,
        message_id=status_msg.message_id,
        text=STATUS_OPENING,
    )

    try:
        await task_service.run_catalog_job(
            bot=bot,
            user_id=message.from_user.id,
            chat_id=message.chat.id,
            source_url=url,
            status_message_id=status_msg.message_id,
        )
    except CatalogBotError as exc:
        await bot.edit_message_text(
            chat_id=message.chat.id,
            message_id=status_msg.message_id,
            text=exc.user_message,
        )
    except Exception as exc:
        logger.exception("handler_error", error=str(exc))
        await bot.edit_message_text(
            chat_id=message.chat.id,
            message_id=status_msg.message_id,
            text="Не удалось сформировать каталог из-за временной ошибки. Попробуйте повторить запрос позже.",
        )
