"""Bot entrypoint.

Runs the aiogram bot via long polling (the architecture keeps webhook support in
reach — see ``app/api/health.py`` for the FastAPI app that a webhook route can be
added to). Also launches the background cleanup service.
"""

from __future__ import annotations

import asyncio

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from app.bot.handlers import build_router
from app.bot.middlewares import DependencyMiddleware, LoggingMiddleware
from app.config import get_settings
from app.database.session import dispose_engine, init_engine
from app.logging_config import configure_logging, get_logger
from app.services.cleanup_service import CleanupService
from app.services.task_service import TaskService

logger = get_logger("main")


async def run_bot() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    settings.ensure_directories()

    if not settings.telegram_bot_token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")

    init_engine(settings)

    bot = Bot(
        token=settings.telegram_bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dispatcher = Dispatcher()

    task_service = TaskService(settings, bot)

    dispatcher.message.middleware(LoggingMiddleware())
    dispatcher.message.middleware(DependencyMiddleware(task_service=task_service))
    dispatcher.include_router(build_router())

    cleanup = CleanupService(settings)
    cleanup_task = asyncio.create_task(cleanup.run_forever())

    logger.info(
        "Starting bot (polling)",
        model=settings.openai_model,
        max_jobs=settings.max_concurrent_jobs,
    )
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        await dispatcher.start_polling(bot, allowed_updates=dispatcher.resolve_used_update_types())
    finally:
        cleanup.stop()
        cleanup_task.cancel()
        await bot.session.close()
        await dispose_engine()


def main() -> None:
    try:
        asyncio.run(run_bot())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped")


if __name__ == "__main__":
    main()
