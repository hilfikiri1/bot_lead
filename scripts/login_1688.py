from __future__ import annotations

import asyncio

from playwright.async_api import async_playwright

from app.parser.session_manager import get_storage_state_path

LOGIN_URL = "https://login.1688.com/"


async def main() -> None:
    storage_state = get_storage_state_path()
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=False)
        context = await browser.new_context(locale="zh-CN")
        page = await context.new_page()
        await page.goto(LOGIN_URL, wait_until="domcontentloaded")
        print("Войдите в аккаунт 1688 в открытом браузере. После успешного входа вернитесь сюда и нажмите Enter.")
        await asyncio.to_thread(input)
        await context.storage_state(path=str(storage_state))
        await browser.close()
    print(f"Storage state сохранён: {storage_state}")


if __name__ == "__main__":
    asyncio.run(main())
