"""Manual 1688 login helper.

Opens a *visible* Chromium window so an administrator can log into 1688 by hand
(entering credentials / passing any CAPTCHA). After a successful login the
Playwright storage state (cookies + localStorage) is saved so the bot can reuse
the authenticated session.

Usage:
    python scripts/login_1688.py

The window stays open until you press ENTER in the terminal — do that only after
you have finished logging in and can see product pages normally.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# Allow running as a plain script (python scripts/login_1688.py).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from playwright.async_api import async_playwright  # noqa: E402

from app.config import get_settings  # noqa: E402

LOGIN_URL = "https://login.1688.com/"


async def main() -> None:
    settings = get_settings()
    settings.ensure_directories()
    storage_path = settings.storage_state_path
    storage_path.parent.mkdir(parents=True, exist_ok=True)

    print("Launching Chromium (headless=False). A browser window will open.")
    print("1. Log into your 1688 account in the opened window.")
    print("2. Open any product page to confirm you are logged in.")
    print("3. Return here and press ENTER to save the session.\n")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False, args=["--start-maximized"])
        context = await browser.new_context(
            locale="zh-CN",
            viewport=None,
        )
        page = await context.new_page()
        await page.goto(LOGIN_URL)

        # Block until the admin confirms login is complete.
        await asyncio.get_event_loop().run_in_executor(
            None, input, "Press ENTER after you have logged in..."
        )

        await context.storage_state(path=str(storage_path))
        print(f"\nSession saved to: {storage_path}")
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
