from __future__ import annotations

import time
from collections import defaultdict
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import Message, TelegramObject

from app.bot.messages import RATE_LIMIT_HIT
from app.logging_config import get_logger

logger = get_logger(__name__)

# Requests per minute per user
RATE_LIMIT_RPM = 10
WINDOW_SECONDS = 60


class RateLimitMiddleware(BaseMiddleware):
    def __init__(self) -> None:
        self._user_timestamps: dict[int, list[float]] = defaultdict(list)

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not isinstance(event, Message):
            return await handler(event, data)

        user = event.from_user
        if not user:
            return await handler(event, data)

        uid = user.id
        now = time.monotonic()
        window_start = now - WINDOW_SECONDS

        timestamps = self._user_timestamps[uid]
        # Remove old timestamps
        self._user_timestamps[uid] = [t for t in timestamps if t > window_start]

        if len(self._user_timestamps[uid]) >= RATE_LIMIT_RPM:
            logger.warning("rate_limit_exceeded", user_id=uid)
            await event.answer(RATE_LIMIT_HIT)
            return None

        self._user_timestamps[uid].append(now)
        return await handler(event, data)
