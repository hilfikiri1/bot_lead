"""Playwright browser lifecycle and page-opening helpers.

This module owns the low-level browser concerns so the parser can focus on data
extraction:
* launching Chromium (headless configurable);
* creating a context that re-uses the saved 1688 session;
* opening a page with a hard timeout and ``domcontentloaded`` wait;
* dismissing cookie/login popups (best effort);
* detecting CAPTCHA / login walls;
* smoothly scrolling to trigger lazy-loaded images (bounded);
* capturing XHR/fetch JSON responses seen while loading;
* saving a screenshot + HTML on error when debug mode is on;
* always closing the page and context in ``finally``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from types import TracebackType

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    Response,
    async_playwright,
)
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from app.config import Settings
from app.exceptions import CaptchaDetectedError, ProductPageNotFoundError
from app.logging_config import get_logger
from app.parser import selectors
from app.parser.session_manager import SessionManager

logger = get_logger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


@dataclass
class ReadyPage:
    """A fully-loaded product page handed to the parser."""

    page: Page
    captured_json: list[dict] = field(default_factory=list)
    cookies: list[dict] = field(default_factory=list)


class BrowserManager:
    """Async context manager that owns a single Chromium instance."""

    def __init__(self, settings: Settings):
        self._settings = settings
        self._session = SessionManager(settings.storage_state_path)
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None

    async def __aenter__(self) -> BrowserManager:
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=self._settings.playwright_headless,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
            ],
        )
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._browser is not None:
            await self._browser.close()
        if self._playwright is not None:
            await self._playwright.stop()

    async def _new_context(self) -> BrowserContext:
        assert self._browser is not None
        storage_state = self._session.storage_state_arg()
        context = await self._browser.new_context(
            user_agent=USER_AGENT,
            locale="zh-CN",
            viewport={"width": 1440, "height": 900},
            storage_state=storage_state,
        )
        context.set_default_timeout(self._settings.playwright_timeout_seconds * 1000)
        return context

    @staticmethod
    def _looks_like_captcha(url: str, html: str) -> bool:
        lowered_url = url.lower()
        if any(hint in lowered_url for hint in selectors.CAPTCHA_URL_HINTS):
            return True
        lowered_html = html.lower()
        markers = ("nc_1_wrapper", "sm-login", "punish", "滑动验证", "验证码")
        return any(marker.lower() in lowered_html for marker in markers)

    async def _dismiss_popups(self, page: Page) -> None:
        for selector in selectors.DISMISS_SELECTORS:
            try:
                locator = page.locator(selector).first
                if await locator.count() > 0 and await locator.is_visible():
                    await locator.click(timeout=1500)
            except Exception:  # noqa: BLE001 - popups are best-effort
                continue

    async def _smooth_scroll(self, page: Page) -> None:
        """Scroll down in bounded steps to trigger lazy-loaded images."""
        max_scrolls = max(1, self._settings.playwright_max_scrolls)
        for _ in range(max_scrolls):
            try:
                at_bottom = await page.evaluate(
                    """() => {
                        window.scrollBy(0, window.innerHeight * 0.9);
                        return (window.innerHeight + window.scrollY) >=
                               (document.body.scrollHeight - 5);
                    }"""
                )
            except Exception:  # noqa: BLE001
                break
            await page.wait_for_timeout(400)
            if at_bottom:
                break
        try:
            await page.evaluate("() => window.scrollTo(0, 0)")
        except Exception:  # noqa: BLE001
            pass

    async def _captcha_in_dom(self, page: Page) -> bool:
        for selector in selectors.CAPTCHA_DOM_SELECTORS:
            try:
                if await page.locator(selector).count() > 0:
                    return True
            except Exception:  # noqa: BLE001
                continue
        return False

    async def _save_debug(self, page: Page, debug_dir: Path | None) -> None:
        if not self._settings.debug_save_page or debug_dir is None:
            return
        try:
            debug_dir.mkdir(parents=True, exist_ok=True)
            await page.screenshot(path=str(debug_dir / "error_screenshot.png"), full_page=True)
            html = await page.content()
            (debug_dir / "error_page.html").write_text(html, encoding="utf-8")
            (debug_dir / "error_url.txt").write_text(page.url, encoding="utf-8")
        except Exception as exc:  # noqa: BLE001 - debug capture must never crash
            logger.warning("Failed to save debug artifacts", error=str(exc))

    @asynccontextmanager
    async def product_page(
        self, url: str, *, debug_dir: Path | None = None
    ) -> AsyncIterator[ReadyPage]:
        """Open a product page and yield a :class:`ReadyPage`.

        The page and its browser context are always closed in ``finally``.
        Raises :class:`CaptchaDetectedError` or :class:`ProductPageNotFoundError`.
        """
        context = await self._new_context()
        page = await context.new_page()
        captured_json: list[dict] = []

        async def _on_response(response: Response) -> None:
            content_type = response.headers.get("content-type", "")
            if "application/json" not in content_type:
                return
            if response.request.resource_type not in ("xhr", "fetch"):
                return
            try:
                payload = await response.json()
            except Exception:  # noqa: BLE001 - many responses are not JSON
                return
            if isinstance(payload, dict):
                captured_json.append(payload)

        page.on("response", _on_response)

        try:
            try:
                response = await page.goto(
                    url,
                    wait_until="domcontentloaded",
                    timeout=self._settings.playwright_timeout_seconds * 1000,
                )
            except PlaywrightTimeoutError as exc:
                raise ProductPageNotFoundError("Timed out opening the page") from exc

            if response is not None and response.status == 404:
                raise ProductPageNotFoundError()

            await self._dismiss_popups(page)

            # Bounded wait for any key element (never use infinite networkidle).
            for ready_selector in selectors.READY_SELECTORS:
                try:
                    await page.wait_for_selector(ready_selector, timeout=3000)
                    break
                except PlaywrightTimeoutError:
                    continue

            await self._smooth_scroll(page)
            await self._dismiss_popups(page)

            html = await page.content()
            if self._looks_like_captcha(page.url, html) or await self._captcha_in_dom(page):
                raise CaptchaDetectedError()

            cookies = await context.cookies()
            yield ReadyPage(page=page, captured_json=captured_json, cookies=cookies)
        except BaseException:
            await self._save_debug(page, debug_dir)
            raise
        finally:
            await page.close()
            await context.close()
