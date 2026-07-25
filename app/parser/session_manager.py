"""Playwright browser session management."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from playwright.async_api import Browser, BrowserContext, Page, Playwright, async_playwright

from app.config import Settings, get_settings
from app.logging_config import get_logger

logger = get_logger(__name__)


class BrowserSessionManager:
    """Manages a shared Playwright browser instance."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        async with self._lock:
            if self._browser is not None:
                return
            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(
                headless=self.settings.playwright_headless,
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                ],
            )
            logger.info("browser_started", headless=self.settings.playwright_headless)

    async def stop(self) -> None:
        async with self._lock:
            if self._browser:
                await self._browser.close()
                self._browser = None
            if self._playwright:
                await self._playwright.stop()
                self._playwright = None
            logger.info("browser_stopped")

    def _storage_state_path(self) -> Path | None:
        path = Path(self.settings.playwright_storage_state)
        if path.exists():
            return path
        return None

    @asynccontextmanager
    async def new_page(self) -> AsyncIterator[Page]:
        if self._browser is None:
            await self.start()

        assert self._browser is not None
        context_kwargs: dict = {
            "viewport": {"width": 1440, "height": 900},
            "user_agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "locale": "zh-CN",
        }
        storage = self._storage_state_path()
        if storage:
            context_kwargs["storage_state"] = str(storage)

        context: BrowserContext = await self._browser.new_context(**context_kwargs)
        page = await context.new_page()
        page.set_default_timeout(self.settings.playwright_timeout_ms)
        try:
            yield page
        finally:
            await page.close()
            await context.close()

    @asynccontextmanager
    async def new_context(self, *, headless: bool | None = None) -> AsyncIterator[BrowserContext]:
        """Create isolated context (used by login script)."""
        if self._playwright is None:
            self._playwright = await async_playwright().start()

        launch_headless = self.settings.playwright_headless if headless is None else headless
        browser = await self._playwright.chromium.launch(
            headless=launch_headless,
            args=["--no-sandbox", "--disable-setuid-sandbox"],
        )
        context = await browser.new_context(
            viewport={"width": 1440, "height": 900},
            locale="zh-CN",
        )
        try:
            yield context
        finally:
            await context.close()
            await browser.close()


_browser_manager: BrowserSessionManager | None = None


def get_browser_manager() -> BrowserSessionManager:
    global _browser_manager
    if _browser_manager is None:
        _browser_manager = BrowserSessionManager()
    return _browser_manager
