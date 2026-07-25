#!/usr/bin/env python3
"""Manual 1688 login script — saves Playwright storage state."""

from __future__ import annotations

import asyncio
from pathlib import Path

from playwright.async_api import async_playwright

STORAGE_PATH = Path("storage/browser/1688_storage_state.json")
LOGIN_URL = "https://login.1688.com/member/signin.htm"


async def main() -> None:
    STORAGE_PATH.parent.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False)
        context = await browser.new_context(viewport={"width": 1440, "height": 900}, locale="zh-CN")
        page = await context.new_page()

        print("Opening 1688 login page...")
        print("Please log in manually in the browser window.")
        await page.goto(LOGIN_URL)

        print("After successful login, press Enter in this terminal...")
        input()

        await context.storage_state(path=str(STORAGE_PATH))
        print(f"Storage state saved to {STORAGE_PATH}")

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
