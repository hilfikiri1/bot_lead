from __future__ import annotations

from pathlib import Path

from playwright.async_api import Browser, BrowserContext, Page, async_playwright

from app.config import Settings


class BrowserSession:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._playwright = None
        self._browser: Browser | None = None

    async def __aenter__(self) -> "BrowserSession":
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(headless=self.settings.playwright_headless)
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()

    async def new_context(self, *, storage_state: Path | None = None) -> BrowserContext:
        if self._browser is None:
            raise RuntimeError("BrowserSession is not started")
        kwargs = {"viewport": {"width": 1440, "height": 1800}, "locale": "zh-CN", "user_agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"}
        state = storage_state or self.settings.playwright_storage_state
        if state.exists():
            kwargs["storage_state"] = str(state)
        return await self._browser.new_context(**kwargs)

    async def new_page(self) -> tuple[BrowserContext, Page]:
        context = await self.new_context()
        page = await context.new_page()
        page.set_default_timeout(self.settings.playwright_timeout_seconds * 1000)
        return context, page
