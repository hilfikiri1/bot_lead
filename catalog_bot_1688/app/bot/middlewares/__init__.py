"""Aiogram middlewares."""

from app.bot.middlewares.dependency import DependencyMiddleware
from app.bot.middlewares.logging import LoggingMiddleware

__all__ = ["DependencyMiddleware", "LoggingMiddleware"]
