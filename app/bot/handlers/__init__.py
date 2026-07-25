"""Bot handlers package."""

from aiogram import Router

from app.bot.handlers.product_link import router as product_link_router
from app.bot.handlers.start import router as start_router


def get_handlers_router() -> Router:
    router = Router()
    router.include_router(start_router)
    router.include_router(product_link_router)
    return router
