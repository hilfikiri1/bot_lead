from __future__ import annotations

import asyncio
from pathlib import Path

from playwright.async_api import async_playwright

STORAGE_STATE_PATH = Path("storage/browser/1688_storage_state.json")


async def main() -> None:
    STORAGE_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context()
        page = await context.new_page()
        await page.goto("https://login.1688.com/", wait_until="domcontentloaded")
        print("Выполните вход вручную, затем нажмите Enter в терминале.")
        input()
        await context.storage_state(path=str(STORAGE_STATE_PATH))
        await browser.close()
    print(f"Storage state saved to {STORAGE_STATE_PATH}")


if __name__ == "__main__":
    asyncio.run(main())
