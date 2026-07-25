#!/usr/bin/env python3
"""
Manual 1688 login script.

Usage:
  python scripts/login_1688.py

Opens a Chromium window for manual login. After you complete the login,
press ENTER in the terminal. The session is saved to storage/browser/1688_storage_state.json
and used automatically by the bot on subsequent runs.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# Ensure project root is on path
sys.path.insert(0, str(Path(__file__).parent.parent))

from playwright.async_api import async_playwright

STORAGE_STATE_PATH = Path("storage/browser/1688_storage_state.json")
LOGIN_URL = "https://passport.1688.com/member/signin.htm"


async def main() -> None:
    print("=" * 60)
    print("  Babrik Solutions — 1688 Manual Login")
    print("=" * 60)
    print()
    print("A Chromium browser window will open.")
    print("1. Log in to your 1688 account manually.")
    print("2. After successful login, return to this terminal.")
    print("3. Press ENTER to save the session.")
    print()

    STORAGE_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=False,
            args=["--no-sandbox"],
        )
        context = await browser.new_context(
            viewport={"width": 1280, "height": 800},
            locale="zh-CN",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        )
        page = await context.new_page()
        await page.goto(LOGIN_URL)

        print("Browser opened. Log in to 1688, then press ENTER here...")
        input()

        await context.storage_state(path=str(STORAGE_STATE_PATH))
        print(f"\n✅  Session saved to: {STORAGE_STATE_PATH}")
        print("The bot will use this session automatically.")

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
