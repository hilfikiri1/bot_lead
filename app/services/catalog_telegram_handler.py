"""Telegram handler helpers for 1688 catalog generation."""

from __future__ import annotations

import logging
import re
import time
from collections import defaultdict

from sqlalchemy.ext.asyncio import AsyncSession

from app.catalog_exceptions import CatalogBotError, JobAlreadyRunningError, RateLimitError
from app.celery_app import celery_app
from app.config import get_settings
from app.parser.url_validator import validate_url_format
from app.services import catalog_service, telegram_service

logger = logging.getLogger(__name__)
settings = get_settings()

URL_PATTERN = re.compile(r"https://[^\s]+", re.I)
_last_request: dict[int, float] = defaultdict(float)


def extract_1688_url(text: str) -> str | None:
    for match in URL_PATTERN.finditer(text):
        candidate = match.group(0).rstrip(".,;)")
        try:
            validate_url_format(candidate)
            return candidate
        except CatalogBotError:
            continue
    return None


async def handle_catalog_link(
    *,
    db: AsyncSession,
    chat_id: int,
    user_id: int,
    text: str,
    spawn_background,
) -> bool:
    """Return True if message was handled as a 1688 catalog request."""
    if not settings.catalog_enabled:
        return False

    url = extract_1688_url(text)
    if not url:
        return False

    now = time.time()
    if now - _last_request[user_id] < settings.catalog_rate_limit_seconds:
        await telegram_service.send_message(chat_id, RateLimitError.user_message)
        return True
    _last_request[user_id] = now

    service = catalog_service.CatalogService()
    active = await service.get_active_job(db, user_id)
    if active:
        await telegram_service.send_message(chat_id, JobAlreadyRunningError.user_message)
        return True

    job = await service.create_job(db, user_id, chat_id, url)
    status_msg = await telegram_service.send_message(
        chat_id,
        "Ссылка получена. Загружаю информацию о товаре…\n\nОткрываю страницу 1688…",
    )
    status_message_id = status_msg["result"]["message_id"]

    mode = (settings.catalog_processing_mode or "celery").strip().lower()
    if mode == "celery":
        from app.tasks.catalog_tasks import process_catalog

        process_catalog.delay(
            str(job.id),
            url,
            chat_id,
            status_message_id,
        )
    else:
        from app.tasks.catalog_tasks import process_catalog_async

        spawn_background(
            process_catalog_async(
                str(job.id),
                url,
                chat_id,
                status_message_id,
            )
        )
    return True
