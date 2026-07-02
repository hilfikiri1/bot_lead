"""Database schema upgrades on application startup."""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


async def upgrade_database() -> None:
    """Apply pending Alembic migrations before serving traffic."""
    try:
        from migrations.env import run_async_migrations

        await run_async_migrations()
        logger.info("Database migrations applied")
    except Exception as exc:
        logger.exception("Database migration failed: %s", exc)
        raise
