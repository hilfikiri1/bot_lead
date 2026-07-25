from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from playwright.async_api import BrowserContext, Page, async_playwright

from app.config import get_settings


@asynccontextmanager
async def create_page() -> tuple[BrowserContext, Page]:
    settings = get_settings()
    storage_state = settings.playwright_storage_state
    Path(storage_state).parent.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=settings.playwright_headless)
        context = await browser.new_context(storage_state=storage_state if Path(storage_state).exists() else None)
        page = await context.new_page()
        try:
            yield context, page
        finally:
            await page.close()
            await context.close()
            await browser.close()
