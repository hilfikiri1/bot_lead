from __future__ import annotations

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from app.bot.handlers.product_link import router as product_link_router
from app.bot.handlers.start import router as start_router
from app.config import Settings
from app.services.task_service import TaskLimiter


def create_dispatcher(settings: Settings) -> Dispatcher:
    dispatcher = Dispatcher()
    dispatcher["settings"] = settings
    dispatcher["task_limiter"] = TaskLimiter(settings)
    dispatcher.include_router(start_router)
    dispatcher.include_router(product_link_router)
    return dispatcher


def create_bot(settings: Settings) -> Bot:
    return Bot(token=settings.telegram_bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
