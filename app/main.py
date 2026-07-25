"""Application entry point."""

from __future__ import annotations

import asyncio

from aiogram import Bot, Dispatcher
from fastapi import FastAPI
from uvicorn import Config, Server

from app.api.health import router as health_router
from app.bot.handlers import get_handlers_router
from app.config import get_settings
from app.logging_config import setup_logging, get_logger
from app.parser.session_manager import get_browser_manager

logger = get_logger(__name__)


def create_api() -> FastAPI:
    api = FastAPI(title="Babrik Catalog Bot API", version="0.1.0")
    api.include_router(health_router)
    return api


app = create_api()


async def run_bot() -> None:
    settings = get_settings()
    bot = Bot(token=settings.telegram_bot_token)
    dp = Dispatcher()
    dp.include_router(get_handlers_router())

    browser_manager = get_browser_manager()
    await browser_manager.start()

    try:
        logger.info("bot_starting", mode=settings.bot_mode)
        await dp.start_polling(bot)
    finally:
        await browser_manager.stop()
        await bot.session.close()


async def run_api() -> None:
    settings = get_settings()
    app = create_api()
    config = Config(app=app, host=settings.api_host, port=settings.api_port, log_level="info")
    server = Server(config)
    await server.serve()


async def main() -> None:
    settings = get_settings()
    setup_logging(settings.log_level)

    await asyncio.gather(
        run_bot(),
        run_api(),
    )


if __name__ == "__main__":
    asyncio.run(main())
