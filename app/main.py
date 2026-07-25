from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.health import router as health_router
from app.config import get_settings
from app.database.session import dispose_engine
from app.logging_config import configure_logging

settings = get_settings()
configure_logging(settings.log_level)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings.ensure_directories()
    yield
    await dispose_engine()


app = FastAPI(
    title="Babrik Solutions 1688 Catalog Bot",
    version="0.1.0",
    description="Health API for Telegram bot that creates PDF catalogs from 1688 links.",
    lifespan=lifespan,
)
app.include_router(health_router)
