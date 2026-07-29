"""FastAPI entrypoint for Buy & Bring Solutions CRM assistant."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager, suppress

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.admin import router as admin_router
from app.api.diagnostics import router as diagnostics_router
from app.api.telegram import router as telegram_router
from app.api.whatsapp import router as whatsapp_router
from app.config import get_settings
from app.db_migrations import upgrade_database
from app.services import followup_service, lead_status_sync_service
from app.services.agent_scheduled_digest_service import start_periodic_digest_loop
from app.services.communication_timeline_runtime import (
    install_communication_timeline_runtime,
)
from app.services.diagnostic_runtime import install_diagnostic_runtime
from app.services.followup_runtime import install_followup_runtime_extensions
from app.services.lead_registry_runtime import install_lead_registry_runtime
from app.services.operator_experience_phone_patch import (
    install_operator_experience_phone_patch,
)
from app.services.operator_experience_runtime import install_operator_experience_runtime
from app.services.request_trace import install_request_tracing
from app.services.runtime_extensions import install_runtime_extensions
from app.services.supplier_workspace_runtime import install_supplier_workspace_runtime
from app.services.whatsapp_cloud_runtime import install_whatsapp_cloud_runtime
from app.services.telegram_service import (
    delete_webhook,
    register_webhook,
    set_bot_commands,
)

settings = get_settings()
APP_VERSION = "5.2.0"
install_runtime_extensions()
if followup_service.enabled():
    install_followup_runtime_extensions()
install_communication_timeline_runtime()
install_supplier_workspace_runtime()
install_whatsapp_cloud_runtime()
install_lead_registry_runtime()
install_operator_experience_runtime()
install_operator_experience_phone_patch()
# Install last so /diag wraps the final production behavior of the agent.
install_diagnostic_runtime()

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

    status_sync_task: asyncio.Task | None = None
    digest_task: asyncio.Task | None = None
    followup_task: asyncio.Task | None = None
    if settings.lead_status_sync_enabled:
        status_sync_task = asyncio.create_task(
            lead_status_sync_service.periodic_status_sync_loop(),
            name="lead-status-sync",
        )
        app_logger.info(
            "Lead status sync scheduler enabled (manual service; background loop exits)"
        )

    if settings.agent_morning_digest_enabled or settings.agent_evening_digest_enabled:
        digest_task = await start_periodic_digest_loop()
        app_logger.info(
            "Agent scheduled digest enabled (morning=%s, evening=%s)",
            settings.agent_morning_digest_enabled,
            settings.agent_evening_digest_enabled,
        )

    if followup_service.enabled():
        followup_task = await followup_service.start_periodic_followup_loop()
        app_logger.info("Automatic client follow-up scheduler enabled")

    try:
        yield
    finally:
        if followup_task:
            followup_task.cancel()
            with suppress(asyncio.CancelledError):
                await followup_task
        if digest_task:
            digest_task.cancel()
            with suppress(asyncio.CancelledError):
                await digest_task
        if status_sync_task:
            status_sync_task.cancel()
            with suppress(asyncio.CancelledError):
                await status_sync_task
        app_logger.info("Shutting down")


docs_enabled = settings.expose_api_docs and not settings.is_production
app = FastAPI(
    title="Buy & Bring Solutions — CRM Assistant API",
    description="Telegram CRM assistant for Kommo lead workflows",
    version=APP_VERSION,
    lifespan=lifespan,
    docs_url="/docs" if docs_enabled else None,
    redoc_url="/redoc" if docs_enabled else None,
    openapi_url="/openapi.json" if docs_enabled else None,
)
install_request_tracing(app)

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
            "X-Hub-Signature-256",
            "X-Request-ID",
            "X-Correlation-ID",
        ],
        allow_credentials=False,
    )

app.include_router(telegram_router)
app.include_router(whatsapp_router)
app.include_router(admin_router)
app.include_router(diagnostics_router)

if settings.enable_google_oauth_routes:
    from app.api.auth import router as auth_router

    app.include_router(auth_router)


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "service": "buy-bring-crm-assistant",
        "version": APP_VERSION,
    }


@app.get("/ready")
async def ready():
    return {
        "status": "ready",
        "service": "buy-bring-crm-assistant",
        "version": APP_VERSION,
    }


@app.get("/version")
async def version():
    return {
        "version": APP_VERSION,
        "service": "buy-bring-crm-assistant",
        "agent": "v5.2-diagnostics",
    }


@app.get("/")
async def root():
    return {
        "service": "Buy & Bring Solutions CRM Assistant",
        "version": APP_VERSION,
        "health": "/health",
        "ready": "/ready",
        "docs_enabled": docs_enabled,
    }
