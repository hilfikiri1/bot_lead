from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Optional

from playwright.async_api import BrowserContext, Page, TimeoutError as PWTimeout

from app.config import settings
from app.exceptions import (
    AuthenticationRequiredError,
    CaptchaDetectedError,
    ProductPageNotFoundError,
)
from app.logging_config import get_logger
from app.parser.selectors import (
    CAPTCHA_INDICATORS,
    POPUP_CLOSE_SELECTORS,
    READY_INDICATORS,
)

logger = get_logger(__name__)


async def open_product_page(
    context: BrowserContext,
    url: str,
    job_dir: Optional[Path] = None,
) -> Page:
    """
    Open a 1688 product page using the given browser context.
    - Waits for DOMContentLoaded then key element.
    - Closes cookie/popup banners.
    - Scrolls smoothly to trigger lazy-load.
    - Detects login/captcha wall and raises the appropriate error.
    Returns the ready Page object.
    """
    page = await context.new_page()
    timeout_ms = settings.playwright_timeout_ms

    try:
        logger.debug("navigating_to_url", url=url)
        response = await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)

        if response and response.status == 404:
            raise ProductPageNotFoundError(f"404 for URL: {url}")

        # Brief pause to let initial JS run
        await page.wait_for_timeout(2000)

        # Detect captcha / login gate early
        await _check_for_auth_wall(page)

        # Wait for a meaningful element
        await _wait_for_content(page, timeout_ms=15_000)

        # Dismiss popups
        await _dismiss_popups(page)

        # Scroll to trigger lazy-loading
        await _scroll_page(page)

        # Re-check auth wall after scroll (occasionally appears after scroll)
        await _check_for_auth_wall(page)

        logger.debug("page_ready", url=url)
        return page

    except (AuthenticationRequiredError, CaptchaDetectedError, ProductPageNotFoundError):
        await _save_debug_artifacts(page, job_dir, "error")
        await page.close()
        raise

    except PWTimeout as exc:
        logger.warning("page_load_timeout", url=url)
        await _save_debug_artifacts(page, job_dir, "timeout")
        await page.close()
        raise ProductPageNotFoundError(f"Page load timeout: {url}") from exc

    except Exception as exc:
        logger.exception("page_open_failed", url=url)
        await _save_debug_artifacts(page, job_dir, "exception")
        await page.close()
        raise


async def _check_for_auth_wall(page: Page) -> None:
    for selector in CAPTCHA_INDICATORS:
        try:
            element = await page.query_selector(selector)
            if element:
                page_url = page.url.lower()
                if "login" in page_url or "passport" in page_url:
                    raise AuthenticationRequiredError(
                        f"Login page detected: {page.url}"
                    )
                raise CaptchaDetectedError(
                    f"Captcha/auth selector found: {selector}"
                )
        except (AuthenticationRequiredError, CaptchaDetectedError):
            raise
        except Exception:
            continue


async def _wait_for_content(page: Page, timeout_ms: int = 15_000) -> None:
    """Wait until at least one known content selector appears."""
    for selector in READY_INDICATORS:
        try:
            await page.wait_for_selector(selector, timeout=timeout_ms)
            logger.debug("content_ready", selector=selector)
            return
        except PWTimeout:
            continue
    logger.warning("content_readiness_not_confirmed")


async def _dismiss_popups(page: Page) -> None:
    for selector in POPUP_CLOSE_SELECTORS:
        try:
            btn = await page.query_selector(selector)
            if btn and await btn.is_visible():
                await btn.click(timeout=2000)
                await page.wait_for_timeout(500)
                logger.debug("popup_dismissed", selector=selector)
        except Exception:
            continue


async def _scroll_page(page: Page, max_scrolls: int = 8) -> None:
    """Smoothly scroll the page to trigger lazy-load images."""
    try:
        height = await page.evaluate("document.body.scrollHeight")
        for i in range(max_scrolls):
            scroll_to = int(height * (i + 1) / max_scrolls)
            await page.evaluate(f"window.scrollTo({{top: {scroll_to}, behavior: 'smooth'}})")
            await page.wait_for_timeout(600)
        # Scroll back to top
        await page.evaluate("window.scrollTo({top: 0, behavior: 'smooth'})")
        await page.wait_for_timeout(500)
    except Exception as exc:
        logger.debug("scroll_failed", error=str(exc))


async def _save_debug_artifacts(
    page: Page, job_dir: Optional[Path], label: str
) -> None:
    if not settings.debug_save_page or not job_dir:
        return
    try:
        job_dir.mkdir(parents=True, exist_ok=True)
        screenshot_path = job_dir / f"debug_{label}.png"
        html_path = job_dir / f"debug_{label}.html"
        await page.screenshot(path=str(screenshot_path), full_page=True)
        html = await page.content()
        html_path.write_text(html, encoding="utf-8")
        logger.debug("debug_artifacts_saved", dir=str(job_dir))
    except Exception as exc:
        logger.debug("debug_save_failed", error=str(exc))
