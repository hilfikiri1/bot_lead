from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncGenerator

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    async_playwright,
)

from app.config import settings
from app.logging_config import get_logger

logger = get_logger(__name__)


class BrowserSessionManager:
    """
    Manages a shared Playwright Chromium browser.
    Each context (one per scraping job) gets isolated cookies/storage
    but optionally seeds from the pre-saved 1688 storage state.
    """

    def __init__(self) -> None:
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        async with self._lock:
            if self._browser is not None:
                return
            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(
                headless=settings.playwright_headless,
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-blink-features=AutomationControlled",
                    "--disable-infobars",
                ],
            )
            logger.info("browser_started", headless=settings.playwright_headless)

    async def stop(self) -> None:
        async with self._lock:
            if self._browser:
                await self._browser.close()
                self._browser = None
            if self._playwright:
                await self._playwright.stop()
                self._playwright = None
            logger.info("browser_stopped")

    @asynccontextmanager
    async def new_context(self) -> AsyncGenerator[BrowserContext, None]:
        """Yield a new browser context, seeded with stored 1688 session if available."""
        if self._browser is None:
            await self.start()

        assert self._browser is not None

        storage_path = settings.storage_state_path
        context_kwargs: dict = {
            "viewport": {"width": 1440, "height": 900},
            "user_agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "locale": "zh-CN",
            "extra_http_headers": {
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            },
        }
        if storage_path:
            logger.info("loading_browser_storage_state", path=str(storage_path))
            context_kwargs["storage_state"] = str(storage_path)

        context = await self._browser.new_context(**context_kwargs)
        try:
            yield context
        finally:
            await context.close()


# Module-level singleton
browser_manager = BrowserSessionManager()
