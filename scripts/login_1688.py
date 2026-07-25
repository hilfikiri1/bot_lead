#!/usr/bin/env python3
"""Manual 1688 login — saves Playwright storage state."""

from __future__ import annotations

import asyncio
from pathlib import Path

from playwright.async_api import async_playwright

from app.config import get_settings

STORAGE_PATH = Path("storage/browser/1688_storage_state.json")
LOGIN_URL = "https://login.1688.com/member/signin.htm"


async def main() -> None:
    settings = get_settings()
    path = Path(settings.playwright_storage_state)
    path.parent.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False)
        context = await browser.new_context(viewport={"width": 1440, "height": 900}, locale="zh-CN")
        page = await context.new_page()
        print("Откройте окно браузера и войдите в 1688 вручную...")
        await page.goto(LOGIN_URL)
        input("После входа нажмите Enter...")
        await context.storage_state(path=str(path))
        print(f"Сессия сохранена: {path}")
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
