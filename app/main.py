from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncGenerator

import aiogram.client.default
import structlog
import uvicorn
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from fastapi import FastAPI

from app.api.health import router as health_router
from app.bot.handlers.start import router as start_router
from app.bot.handlers.product_link import router as product_link_router
from app.bot.middlewares.rate_limit import RateLimitMiddleware
from app.config import settings
from app.database.session import init_db
from app.logging_config import configure_logging, get_logger
from app.services.cleanup_service import CleanupService

configure_logging()
logger = get_logger(__name__)


def create_bot() -> Bot:
    return Bot(
        token=settings.telegram_bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )


def create_dispatcher() -> Dispatcher:
    dp = Dispatcher()
    dp.update.middleware(RateLimitMiddleware())
    dp.include_router(start_router)
    dp.include_router(product_link_router)
    return dp


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    logger.info("starting_babrik_catalog_bot")
    await init_db()

    cleanup = CleanupService()
    cleanup_task = asyncio.create_task(cleanup.run_periodic())

    bot = create_bot()
    dp = create_dispatcher()
    polling_task = asyncio.create_task(
        dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    )

    logger.info("bot_polling_started")
    try:
        yield
    finally:
        logger.info("shutting_down")
        polling_task.cancel()
        cleanup_task.cancel()
        await bot.session.close()
        for task in (polling_task, cleanup_task):
            try:
                await task
            except asyncio.CancelledError:
                pass


def create_app() -> FastAPI:
    app = FastAPI(title="Babrik Solutions Catalog Bot", lifespan=lifespan)
    app.include_router(health_router, prefix="/api")
    return app


app = create_app()


if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=8000,
        reload=False,
        log_config=None,
    )
