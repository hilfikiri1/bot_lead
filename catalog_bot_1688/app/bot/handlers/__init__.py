"""Aiogram routers."""

from aiogram import Router

from app.bot.handlers import product_link, start


def build_router() -> Router:
    """Combine all handler routers (order matters: commands first)."""
    router = Router()
    router.include_router(start.router)
    router.include_router(product_link.router)
    return router
