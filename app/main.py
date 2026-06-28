"""FastAPI entrypoint for Buy & Bring Solutions voice bot."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.admin import router as admin_router
from app.api.auth import router as auth_router
from app.api.telegram import router as telegram_router
from app.config import get_settings
from app.services.telegram_service import delete_webhook, register_webhook

settings = get_settings()

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
# httpx logs full request URLs. Telegram URLs contain the bot token.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


@asynccontextmanager
async def lifespan(app: FastAPI):
    app_logger = logging.getLogger(__name__)
    app_logger.info("Starting Buy & Bring Voice Bot")

    if settings.telegram_bot_token and settings.webhook_base_url != "https://your-domain.com":
        webhook_url = f"{settings.webhook_base_url.rstrip('/')}/webhook/telegram"
        try:
            await delete_webhook()
            app_logger.info("Existing Telegram webhook cleared")
        except Exception as exc:
            app_logger.warning("Webhook deletion failed (continuing): %s", exc)
        try:
            await register_webhook(webhook_url)
            app_logger.info("Telegram webhook registered")
        except Exception as exc:
            app_logger.error("Webhook registration failed: %s", exc)

    yield
    app_logger.info("Shutting down")


app = FastAPI(
    title="Buy & Bring Solutions — Voice Bot API",
    description="Automated voice-note processing for B2B sourcing calls",
    version="1.1.0",
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
    return {"status": "ok", "service": "buy-bring-voice-bot", "version": "1.1.0"}


@app.get("/")
async def root():
    return {
        "service": "Buy & Bring Solutions Voice Bot",
        "docs": "/docs",
        "health": "/health",
    }
