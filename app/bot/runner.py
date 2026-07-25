from __future__ import annotations

import asyncio

from app.bot import create_bot, create_dispatcher
from app.config import get_settings
from app.logging_config import configure_logging


async def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    if not settings.telegram_bot_token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is required")
    bot = create_bot(settings)
    dispatcher = create_dispatcher(settings)
    await dispatcher.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
