"""Lightweight logging middleware for incoming updates."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import Message, TelegramObject

from app.logging_config import get_logger

logger = get_logger("bot")


class LoggingMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if isinstance(event, Message) and event.from_user is not None:
            logger.debug(
                "Incoming message",
                user_id=event.from_user.id,
                chat_id=event.chat.id,
                has_text=bool(event.text),
            )
        return await handler(event, data)
