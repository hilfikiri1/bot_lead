"""FastAPI health-check application.

Kept separate from the bot so it can be scaled/deployed independently and later
extended with a Telegram webhook endpoint.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy import text

from app.config import get_settings
from app.database.session import dispose_engine, init_engine, session_scope
from app.logging_config import configure_logging, get_logger

logger = get_logger("api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging(settings.log_level)
    settings.ensure_directories()
    init_engine(settings)
    logger.info("Health API started")
    yield
    await dispose_engine()


app = FastAPI(title="Babrik Catalog Bot API", version="0.1.0", lifespan=lifespan)


@app.get("/health")
async def health() -> dict:
    """Liveness + database connectivity check."""
    db_ok = False
    try:
        async with session_scope() as session:
            await session.execute(text("SELECT 1"))
        db_ok = True
    except Exception as exc:  # noqa: BLE001
        logger.warning("Health DB check failed", error=str(exc))

    status = "ok" if db_ok else "degraded"
    return {"status": status, "database": "ok" if db_ok else "error"}


@app.get("/")
async def root() -> dict:
    return {"service": "babrik-catalog-bot", "status": "running"}
