"""Database schema upgrades on application startup."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _upgrade_database_sync() -> None:
    """Run Alembic through its public command API in a worker thread."""
    from alembic import command
    from alembic.config import Config

    alembic_config = Config(str(PROJECT_ROOT / "alembic.ini"))
    alembic_config.set_main_option(
        "script_location",
        str(PROJECT_ROOT / "migrations"),
    )
    command.upgrade(alembic_config, "head")


async def upgrade_database() -> None:
    """Apply pending Alembic migrations before serving traffic."""
    try:
        # migrations/env.py uses asyncio.run(), so invoking Alembic in the
        # application's running event loop would fail. A worker thread gives
        # Alembic its own synchronous entrypoint and event loop lifecycle.
        await asyncio.to_thread(_upgrade_database_sync)
        logger.info("Database migrations applied")
    except Exception as exc:
        logger.exception("Database migration failed: %s", exc)
        raise
