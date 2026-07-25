from __future__ import annotations

import json
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from playwright.async_api import Page

from app.config import get_settings
from app.exceptions import (
    AuthenticationRequiredError,
    CaptchaDetectedError,
    ProductDataNotFoundError,
    ProductPageNotFoundError,
)
from app.parser.browser import create_page
from app.parser.models import ParsedProduct, PriceTier, ProductSpecification, ProductVariant
from app.parser.selectors import (
    COOKIE_CLOSE_SELECTORS,
    DETAIL_IMAGE_SELECTORS,
    GALLERY_IMAGE_SELECTORS,
    MOQ_SELECTORS,
    PRICE_SELECTORS,
    SPEC_ROW_SELECTORS,
    SUPPLIER_SELECTORS,
    TITLE_SELECTORS,
    VARIANT_SELECTORS,
)
from app.utils.retry import retry_async


def _parse_decimal(text: str | None) -> Decimal | None:
    if not text:
        return None
    m = re.findall(r"\d+(?:\.\d+)?", text.replace(",", ""))
    if not m:
        return None
    try:
        return Decimal(m[0])
    except InvalidOperation:
        return None


def _extract_json_objects(page_html: str) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for block in re.findall(r"<script[^>]*>(.*?)</script>", page_html, flags=re.DOTALL | re.IGNORECASE):
        block = block.strip()
        if not block:
            continue
        if block.startswith("{") and block.endswith("}"):
            try:
                obj = json.loads(block)
                if isinstance(obj, dict):
                    candidates.append(obj)
            except json.JSONDecodeError:
                continue
        for match in re.findall(r"(\{(?:.|\n)*?\})", block):
            if '"price"' not in match and '"title"' not in match and '"offer"' not in match:
                continue
            try:
                obj = json.loads(match)
                if isinstance(obj, dict):
                    candidates.append(obj)
            except json.JSONDecodeError:
                continue
    return candidates


async def _first_text(page: Page, selectors: list[str]) -> str | None:
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            if await locator.count() > 0:
                text = (await locator.inner_text()).strip()
                if text:
                    return text
        except Exception:
            continue
    return None


async def _collect_image_urls(page: Page, selectors: list[str]) -> list[str]:
    urls: list[str] = []
    for selector in selectors:
        try:
            elements = page.locator(selector)
            count = min(await elements.count(), 40)
            for idx in range(count):
                src = await elements.nth(idx).get_attribute("src")
                data_src = await elements.nth(idx).get_attribute("data-src")
                url = src or data_src
                if url and url.startswith(("http://", "https://", "//")):
                    if url.startswith("//"):
                        url = f"https:{url}"
                    urls.append(url)
        except Exception:
            continue
    return list(dict.fromkeys(urls))


async def _close_popups(page: Page) -> None:
    for selector in COOKIE_CLOSE_SELECTORS:
        try:
            locator = page.locator(selector).first
            if await locator.count() > 0:
                await locator.click(timeout=500)
        except Exception:
            continue


async def _smooth_scroll(page: Page, max_scrolls: int = 8) -> None:
    for _ in range(max_scrolls):
        await page.evaluate("window.scrollBy(0, document.body.scrollHeight * 0.35);")
        await page.wait_for_timeout(350)


def _from_json_candidates(candidates: list[dict[str, Any]]) -> dict[str, Any]:
    data: dict[str, Any] = {}
    for item in candidates:
        title = item.get("name") or item.get("title")
        if isinstance(title, str) and not data.get("title_zh"):
            data["title_zh"] = title.strip()
        offers = item.get("offers") or item.get("offer") or {}
        if isinstance(offers, dict):
            low = offers.get("lowPrice") or offers.get("price")
            high = offers.get("highPrice")
            if low and not data.get("price_min_cny"):
                data["price_min_cny"] = _parse_decimal(str(low))
            if high and not data.get("price_max_cny"):
                data["price_max_cny"] = _parse_decimal(str(high))
    return data


def parse_product_from_html(html: str, source_url: str) -> ParsedProduct:
    json_candidates = _extract_json_objects(html)
    json_data = _from_json_candidates(json_candidates)
    title = json_data.get("title_zh")
    if not title:
        title_match = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.IGNORECASE | re.DOTALL)
        title = title_match.group(1).strip() if title_match else None
    if not title:
        raise ProductDataNotFoundError("Unable to extract title from fixture HTML")

    image_urls: list[str] = []
    for src in re.findall(r"""<img[^>]+(?:src|data-src)=["']([^"']+)["']""", html, re.IGNORECASE):
        if src.startswith("//"):
            src = f"https:{src}"
        if src.startswith("http"):
            image_urls.append(src)
    image_urls = list(dict.fromkeys(image_urls))
    if not image_urls:
        raise ProductDataNotFoundError("No images found in fixture HTML")

    return ParsedProduct(
        source_url=source_url,
        title_zh=title,
        supplier_name_zh=None,
        price_min_cny=json_data.get("price_min_cny"),
        price_max_cny=json_data.get("price_max_cny"),
        price_raw_text=None,
        price_tiers=[],
        moq=None,
        moq_raw_text=None,
        variants=[],
        specifications=[],
        gallery_image_urls=image_urls[:8],
        detail_image_urls=image_urls[8:12],
        local_image_paths=[],
    )


@retry_async(max_attempts=2, retry_on=(TimeoutError,))
async def parse_1688_product(url: str, job_dir: Path) -> ParsedProduct:
    settings = get_settings()
    timeout_ms = settings.playwright_timeout_seconds * 1000
    job_dir.mkdir(parents=True, exist_ok=True)

    async with create_page() as (_, page):
        try:
            response = await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        except Exception as exc:
            raise ProductPageNotFoundError(str(exc)) from exc

        if response and response.status >= 400:
            raise ProductPageNotFoundError(f"Bad status {response.status}")

        await _close_popups(page)
        await _smooth_scroll(page)

        content = await page.content()
        current_url = page.url
        lowered = f"{current_url}\n{content}".lower()
        if "captcha" in lowered:
            raise CaptchaDetectedError("Captcha detected on page")
        if "login.1688.com" in lowered or "登录" in content:
            raise AuthenticationRequiredError("Authentication required")

        json_candidates = _extract_json_objects(content)
        json_data = _from_json_candidates(json_candidates)

        title = json_data.get("title_zh") or await _first_text(page, TITLE_SELECTORS)
        if not title:
            raise ProductDataNotFoundError("Unable to extract title")

        price_text = await _first_text(page, PRICE_SELECTORS)
        moq_text = await _first_text(page, MOQ_SELECTORS)
        supplier = await _first_text(page, SUPPLIER_SELECTORS)

        gallery_urls = await _collect_image_urls(page, GALLERY_IMAGE_SELECTORS)
        detail_urls = await _collect_image_urls(page, DETAIL_IMAGE_SELECTORS)

        specs: list[ProductSpecification] = []
        rows = page.locator(",".join(SPEC_ROW_SELECTORS))
        for idx in range(min(await rows.count(), 40)):
            try:
                txt = (await rows.nth(idx).inner_text()).strip()
                parts = [x.strip() for x in re.split(r"[:：\n]", txt) if x.strip()]
                if len(parts) >= 2:
                    specs.append(ProductSpecification(name_zh=parts[0], value_zh=parts[1]))
            except Exception:
                continue

        variants: list[ProductVariant] = []
        variant_blocks = page.locator(",".join(VARIANT_SELECTORS))
        for idx in range(min(await variant_blocks.count(), 10)):
            try:
                txt = (await variant_blocks.nth(idx).inner_text()).strip()
                lines = [line.strip() for line in txt.splitlines() if line.strip()]
                if len(lines) >= 2:
                    variants.append(ProductVariant(name=lines[0], values=lines[1:8]))
            except Exception:
                continue

        if settings.debug_save_page:
            (job_dir / "debug_page.html").write_text(content, encoding="utf-8")
            await page.screenshot(path=str(job_dir / "debug_page.png"), full_page=True)

        if not gallery_urls and not detail_urls:
            raise ProductDataNotFoundError("Unable to extract images")

        moq = None
        if moq_text:
            matches = re.findall(r"\d+", moq_text)
            if matches:
                moq = int(matches[0])

        product = ParsedProduct(
            source_url=current_url,
            title_zh=title,
            supplier_name_zh=supplier,
            price_min_cny=json_data.get("price_min_cny") or _parse_decimal(price_text),
            price_max_cny=json_data.get("price_max_cny"),
            price_raw_text=price_text,
            price_tiers=[PriceTier(raw_text=price_text)] if price_text else [],
            moq=moq,
            moq_raw_text=moq_text,
            variants=variants,
            specifications=specs,
            gallery_image_urls=gallery_urls,
            detail_image_urls=detail_urls,
            local_image_paths=[],
        )
        return product
