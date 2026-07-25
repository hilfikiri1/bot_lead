from __future__ import annotations

import asyncio

import uvicorn
from aiogram import Bot
from fastapi import FastAPI

from app.api.health import router as health_router
from app.bot import build_dispatcher
from app.config import get_settings
from app.logging_config import setup_logging


async def run_bot() -> None:
    settings = get_settings()
    bot = Bot(token=settings.telegram_bot_token)
    dp = build_dispatcher()
    await dp.start_polling(bot)


def create_api() -> FastAPI:
    app = FastAPI(title="babrik-1688-catalog-bot")
    app.include_router(health_router)
    return app


async def run_api() -> None:
    api = create_api()
    config = uvicorn.Config(api, host="0.0.0.0", port=8080, log_level="info")
    server = uvicorn.Server(config)
    await server.serve()


async def main() -> None:
    settings = get_settings()
    setup_logging(settings.log_level)
    await asyncio.gather(run_bot(), run_api())


if __name__ == "__main__":
    asyncio.run(main())
