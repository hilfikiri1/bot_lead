"""Browser utilities for page interaction."""

from __future__ import annotations

import asyncio
from pathlib import Path

from playwright.async_api import Page

from app.config import Settings, get_settings
from app.exceptions import AuthenticationRequiredError, CaptchaDetectedError, ProductPageNotFoundError
from app.logging_config import get_logger
from app.parser import selectors

logger = get_logger(__name__)


async def dismiss_popups(page: Page) -> None:
    for selector in selectors.POPUP_CLOSE_SELECTORS:
        try:
            locator = page.locator(selector).first
            if await locator.is_visible(timeout=500):
                await locator.click(timeout=1000)
                await asyncio.sleep(0.3)
        except Exception:
            continue


async def smooth_scroll(page: Page) -> None:
    for _ in range(selectors.MAX_SCROLL_ATTEMPTS):
        await page.evaluate("window.scrollBy(0, window.innerHeight * 0.8)")
        await asyncio.sleep(selectors.SCROLL_PAUSE_MS / 1000)


async def wait_for_key_elements(page: Page, settings: Settings | None = None) -> None:
    settings = settings or get_settings()
    for selector in selectors.KEY_ELEMENT_SELECTORS:
        try:
            await page.wait_for_selector(selector, timeout=settings.playwright_timeout_ms // 3)
            return
        except Exception:
            continue


def check_page_state(page_url: str, page_content: str) -> None:
    content_lower = page_content.lower()
    url_lower = page_url.lower()

    for indicator in selectors.LOGIN_INDICATORS:
        if indicator.lower() in url_lower or indicator.lower() in content_lower:
            logger.error("authentication_required", url=page_url)
            raise AuthenticationRequiredError()

    for indicator in selectors.CAPTCHA_INDICATORS:
        if indicator.lower() in content_lower:
            logger.error("captcha_detected", url=page_url)
            raise CaptchaDetectedError()

    for indicator in selectors.NOT_FOUND_INDICATORS:
        if indicator.lower() in content_lower:
            raise ProductPageNotFoundError()


async def save_debug_artifacts(page: Page, debug_dir: Path, prefix: str = "error") -> None:
    debug_dir.mkdir(parents=True, exist_ok=True)
    try:
        await page.screenshot(path=str(debug_dir / f"{prefix}.png"), full_page=True)
        html = await page.content()
        (debug_dir / f"{prefix}.html").write_text(html, encoding="utf-8")
        logger.info("debug_artifacts_saved", dir=str(debug_dir))
    except Exception as exc:
        logger.warning("debug_artifacts_failed", error=str(exc))
