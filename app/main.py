"""FastAPI entrypoint for Buy & Bring Solutions CRM assistant."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.admin import router as admin_router
from app.api.telegram import router as telegram_router
from app.config import get_settings
from app.db_migrations import upgrade_database
from app.services.telegram_service import (
    delete_webhook,
    register_webhook,
    set_bot_commands,
)

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
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
if settings.is_production:
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)


@asynccontextmanager
async def lifespan(app: FastAPI):
    app_logger = logging.getLogger(__name__)
    app_logger.info("Starting Buy & Bring CRM Assistant")

    try:
        await upgrade_database()
    except Exception as exc:
        app_logger.error("Startup migration failed: %s", exc)

    if (
        settings.telegram_bot_token
        and settings.webhook_base_url != "https://your-domain.com"
    ):
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
        try:
            await set_bot_commands()
            app_logger.info("Telegram bot commands registered")
        except Exception as exc:
            app_logger.warning("Telegram command registration failed: %s", exc)

    yield
    app_logger.info("Shutting down")


docs_enabled = settings.expose_api_docs and not settings.is_production
app = FastAPI(
    title="Buy & Bring Solutions — CRM Assistant API",
    description="Telegram CRM assistant for Kommo lead workflows",
    version="2.0.0-phase1",
    lifespan=lifespan,
    docs_url="/docs" if docs_enabled else None,
    redoc_url="/redoc" if docs_enabled else None,
    openapi_url="/openapi.json" if docs_enabled else None,
)

cors_origins = settings.get_cors_origins()
if cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_methods=["GET", "POST"],
        allow_headers=[
            "Content-Type",
            "X-Admin-Key",
            "X-Telegram-Bot-Api-Secret-Token",
        ],
        allow_credentials=False,
    )

app.include_router(telegram_router)
app.include_router(admin_router)

if settings.enable_google_oauth_routes:
    from app.api.auth import router as auth_router

    app.include_router(auth_router)


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "service": "buy-bring-crm-assistant",
        "version": "2.0.0-phase1",
    }


@app.get("/")
async def root():
    return {
        "service": "Buy & Bring Solutions CRM Assistant",
        "health": "/health",
        "docs_enabled": docs_enabled,
    }
