"""Middleware that injects shared dependencies (e.g. TaskService) into handlers."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject


class DependencyMiddleware(BaseMiddleware):
    """Injects a fixed set of dependencies into every handler's ``data`` dict."""

    def __init__(self, **dependencies: Any):
        self._dependencies = dependencies

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        data.update(self._dependencies)
        return await handler(event, data)
