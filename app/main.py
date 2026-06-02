"""
main.py — FastAPI application entrypoint for Buy & Bring Solutions voice bot.
"""
from __future__ import annotations

import logging
import structlog

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager

from app.config import get_settings
from app.database import init_db
from app.api.telegram import router as telegram_router
from app.api.admin import router as admin_router
from app.api.auth import router as auth_router
from app.services.telegram_service import delete_webhook, register_webhook

settings = get_settings()

# Configure structured logging
structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.stdlib.add_log_level,
        structlog.dev.ConsoleRenderer(),
    ],
    wrapper_class=structlog.BoundLogger,
    logger_factory=structlog.PrintLoggerFactory(),
)
logging.basicConfig(level=settings.log_level)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    logging.getLogger(__name__).info("Starting Buy & Bring Voice Bot")
    if settings.app_env == "development":
        await init_db()

    # Register Telegram webhook
    if settings.telegram_bot_token and settings.webhook_base_url != "https://your-domain.com":
        webhook_url = f"{settings.webhook_base_url}/webhook/telegram"
        try:
            deleted = await delete_webhook()
            logging.getLogger(__name__).info("Existing webhook cleared: %s", deleted)
        except Exception as e:
            logging.getLogger(__name__).warning("Webhook deletion failed (continuing): %s", e)
        try:
            result = await register_webhook(webhook_url)
            logging.getLogger(__name__).info("Webhook registered: %s", result)
        except Exception as e:
            logging.getLogger(__name__).warning("Webhook registration failed: %s", e)

    yield
    # Shutdown
    logging.getLogger(__name__).info("Shutting down")


app = FastAPI(
    title="Buy & Bring Solutions — Voice Bot API",
    description="Automated voice note processing for B2B sourcing calls",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(telegram_router)
app.include_router(admin_router)
app.include_router(auth_router)


@app.get("/health")
async def health():
    return {"status": "ok", "service": "buy-bring-voice-bot"}


@app.get("/")
async def root():
    return {
        "service": "Buy & Bring Solutions Voice Bot",
        "docs": "/docs",
        "health": "/health",
    }
