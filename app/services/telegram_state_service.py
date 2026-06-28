"""Short-lived Telegram conversation state stored in Redis."""
from __future__ import annotations

import json
import logging
from typing import Any

from redis.asyncio import Redis

from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

STATE_TTL_SECONDS = 30 * 60


def _key(user_id: int) -> str:
    return f"telegram:state:{user_id}"


async def set_state(
    user_id: int,
    state: dict[str, Any],
    *,
    ttl_seconds: int = STATE_TTL_SECONDS,
) -> None:
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    try:
        await redis.set(
            _key(user_id),
            json.dumps(state, ensure_ascii=False),
            ex=max(60, ttl_seconds),
        )
    finally:
        await redis.aclose()


async def get_state(user_id: int) -> dict[str, Any] | None:
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    try:
        raw = await redis.get(_key(user_id))
    finally:
        await redis.aclose()

    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("Invalid Telegram state JSON for user_id=%s", user_id)
        await clear_state(user_id)
        return None
    return parsed if isinstance(parsed, dict) else None


async def clear_state(user_id: int) -> None:
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    try:
        await redis.delete(_key(user_id))
    finally:
        await redis.aclose()
