from __future__ import annotations

import json
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import structlog
from bs4 import BeautifulSoup
from playwright.async_api import Page, TimeoutError as PlaywrightTimeoutError

from app.config import Settings
from app.parser import selectors
from app.parser.browser import BrowserSession
from app.parser.errors import AuthenticationRequiredError, CaptchaDetectedError, ProductDataNotFoundError, ProductPageNotFoundError
from app.parser.models import ParsedProduct, PriceTier, ProductSpecification, ProductVariant
from app.utils.retry import async_retry

logger = structlog.get_logger(__name__)
PRICE_RE = re.compile(r"(?<!\d)(\d+(?:[.,]\d+)?)(?!\d)")
QTY_RE = re.compile(r"(\d+)")


def _text(value: Any) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def _decimal(value: str | None) -> Decimal | None:
    if not value:
        return None
    try:
        return Decimal(value.replace(",", "."))
    except (InvalidOperation, ValueError):
        return None


def _unique(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        clean = item.strip()
        if clean and clean not in seen:
            seen.add(clean)
            result.append(clean)
    return result


class Parser1688:
    def __init__(self, settings: Settings):
        self.settings = settings

    async def parse(self, url: str, job_dir: Path) -> ParsedProduct:
        return await async_retry(lambda: self._parse_once(url, job_dir), attempts=2, retry_exceptions=(ProductPageNotFoundError, PlaywrightTimeoutError))

    async def _parse_once(self, url: str, job_dir: Path) -> ParsedProduct:
        context = None
        page = None
        async with BrowserSession(self.settings) as browser:
            try:
                context, page = await browser.new_page()
                await page.goto(url, wait_until="domcontentloaded", timeout=self.settings.playwright_timeout_seconds * 1000)
                await self._close_popups(page)
                await self._detect_auth_or_captcha(page)
                await self._wait_for_product_signals(page)
                await self._scroll_for_lazy_images(page)
                html = await page.content()
                (job_dir / "page.html").write_text(html, encoding="utf-8")
                product = self.parse_html(html, url)
                if not product.has_minimum_data():
                    raise ProductDataNotFoundError("Minimum product fields were not found")
                (job_dir / "parsed_product.json").write_text(product.model_dump_json(indent=2), encoding="utf-8")
                return product
            except (AuthenticationRequiredError, CaptchaDetectedError, ProductDataNotFoundError):
                if page and self.settings.debug_save_page:
                    await self._save_debug(page, job_dir)
                raise
            except PlaywrightTimeoutError as exc:
                if page and self.settings.debug_save_page:
                    await self._save_debug(page, job_dir)
                raise ProductPageNotFoundError(str(exc)) from exc
            finally:
                if page:
                    await page.close()
                if context:
                    await context.close()

    async def _close_popups(self, page: Page) -> None:
        for selector in selectors.POPUP_CLOSE_SELECTORS:
            try:
                locator = page.locator(selector).first
                if await locator.count() and await locator.is_visible(timeout=500):
                    await locator.click(timeout=1000)
            except Exception:
                continue

    async def _detect_auth_or_captcha(self, page: Page) -> None:
        url = page.url.lower()
        content = (await page.content()).lower()
        if any(marker.lower() in url or marker.lower() in content for marker in selectors.CAPTCHA_MARKERS):
            raise CaptchaDetectedError("1688 captcha detected")
        if any(marker.lower() in url or marker.lower() in content for marker in selectors.AUTH_MARKERS):
            raise AuthenticationRequiredError("1688 login page detected")

    async def _wait_for_product_signals(self, page: Page) -> None:
        combined = ",".join(selectors.TITLE_SELECTORS + selectors.GALLERY_IMAGE_SELECTORS)
        try:
            await page.wait_for_selector(combined, timeout=min(10000, self.settings.playwright_timeout_seconds * 1000))
        except PlaywrightTimeoutError:
            logger.warning("product_signals_timeout", url=page.url)

    async def _scroll_for_lazy_images(self, page: Page) -> None:
        for _ in range(8):
            await page.mouse.wheel(0, 1300)
            await page.wait_for_timeout(350)

    async def _save_debug(self, page: Page, job_dir: Path) -> None:
        debug_dir = job_dir / "debug"
        debug_dir.mkdir(parents=True, exist_ok=True)
        (debug_dir / "error.html").write_text(await page.content(), encoding="utf-8")
        await page.screenshot(path=str(debug_dir / "error.png"), full_page=True)

    def parse_html(self, html: str, source_url: str) -> ParsedProduct:
        soup = BeautifulSoup(html, "html.parser")
        json_candidates = self._extract_json_candidates(soup)
        title = self._find_in_json(json_candidates, ("subject", "title", "offerTitle", "productTitle", "name")) or self._first_text(soup, selectors.TITLE_SELECTORS)
        price_text = self._find_price_text(json_candidates) or self._first_text(soup, selectors.PRICE_SELECTORS)
        price_values = [_decimal(match.group(1)) for match in PRICE_RE.finditer(price_text or "")]
        price_values = [value for value in price_values if value is not None]
        supplier = self._find_in_json(json_candidates, ("companyName", "supplierName", "sellerName")) or self._first_text(soup, selectors.SUPPLIER_SELECTORS)
        moq_text = self._find_in_json(json_candidates, ("minOrderQuantity", "beginAmount", "moq")) or self._first_text(soup, selectors.MOQ_SELECTORS)
        moq = self._extract_int(moq_text)
        gallery = self._extract_images(soup, selectors.GALLERY_IMAGE_SELECTORS, source_url)
        detail = self._extract_images(soup, selectors.DETAIL_IMAGE_SELECTORS, source_url)
        specs = self._extract_specifications(soup)
        variants = self._extract_variants(json_candidates)
        tiers = self._extract_price_tiers(json_candidates, price_text)
        return ParsedProduct(
            source_url=source_url,
            title_zh=title or "",
            supplier_name_zh=supplier or None,
            price_min_cny=min(price_values) if price_values else None,
            price_max_cny=max(price_values) if price_values else None,
            price_raw_text=price_text or None,
            price_tiers=tiers,
            moq=moq,
            moq_raw_text=moq_text or None,
            variants=variants,
            specifications=specs,
            gallery_image_urls=gallery[: self.settings.max_gallery_images],
            detail_image_urls=detail[: self.settings.max_detail_images],
        )

    def _extract_json_candidates(self, soup: BeautifulSoup) -> list[Any]:
        candidates: list[Any] = []
        for script in soup.find_all("script"):
            raw = script.string or script.get_text(" ")
            if not raw or len(raw) < 10:
                continue
            if script.get("type") == "application/ld+json":
                self._append_json(raw, candidates)
                continue
            for match in re.finditer(r"(\{[^<]{20,}\}|\[[^<]{20,}\])", raw):
                snippet = match.group(1)
                if any(key in snippet for key in ("price", "offer", "sku", "subject", "title", "image")):
                    self._append_json(snippet, candidates)
        return candidates

    def _append_json(self, raw: str, candidates: list[Any]) -> None:
        try:
            candidates.append(json.loads(raw))
        except json.JSONDecodeError:
            return

    def _walk_json(self, obj: Any):
        if isinstance(obj, dict):
            yield obj
            for value in obj.values():
                yield from self._walk_json(value)
        elif isinstance(obj, list):
            for item in obj:
                yield from self._walk_json(item)

    def _find_in_json(self, candidates: list[Any], keys: tuple[str, ...]) -> str:
        lowered = {key.lower() for key in keys}
        for candidate in candidates:
            for obj in self._walk_json(candidate):
                for key, value in obj.items():
                    if key.lower() in lowered:
                        clean = _text(value)
                        if clean:
                            return clean
        return ""

    def _find_price_text(self, candidates: list[Any]) -> str:
        pieces: list[str] = []
        for candidate in candidates:
            for obj in self._walk_json(candidate):
                for key, value in obj.items():
                    if "price" in key.lower() and not isinstance(value, (dict, list)):
                        text = _text(value)
                        if text:
                            pieces.append(text)
        return " / ".join(_unique(pieces[:6]))

    def _extract_price_tiers(self, candidates: list[Any], price_text: str | None) -> list[PriceTier]:
        tiers: list[PriceTier] = []
        for candidate in candidates:
            for obj in self._walk_json(candidate):
                keys = {str(k).lower() for k in obj.keys()}
                if not ({"price", "pricecny", "discountprice", "amount", "beginamount", "minquantity"} & keys):
                    continue
                price = None
                qty = None
                for key, value in obj.items():
                    lkey = key.lower()
                    if price is None and "price" in lkey:
                        price = _decimal(_text(value))
                    if qty is None and any(token in lkey for token in ("amount", "quantity", "begin")):
                        qty = self._extract_int(_text(value))
                if price or qty:
                    tiers.append(PriceTier(min_quantity=qty, price_cny=price, raw_text=_text(obj)))
        if tiers:
            return tiers[:8]
        if price_text:
            return [PriceTier(raw_text=price_text)]
        return []

    def _extract_specifications(self, soup: BeautifulSoup) -> list[ProductSpecification]:
        specs: list[ProductSpecification] = []
        for selector in selectors.SPECIFICATION_SELECTORS:
            for node in soup.select(selector):
                text = _text(node.get_text(" "))
                if not text or len(text) > 160:
                    continue
                if ":" in text:
                    name, value = text.split(":", 1)
                elif "：" in text:
                    name, value = text.split("：", 1)
                else:
                    cells = [_text(cell.get_text(" ")) for cell in node.select("td,th")]
                    if len(cells) >= 2:
                        name, value = cells[0], cells[1]
                    else:
                        continue
                if name and value:
                    specs.append(ProductSpecification(name_zh=name[:80], value_zh=value[:160]))
        unique: dict[tuple[str, str], ProductSpecification] = {(s.name_zh, s.value_zh): s for s in specs}
        return list(unique.values())[:30]

    def _extract_variants(self, candidates: list[Any]) -> list[ProductVariant]:
        variants: dict[str, set[str]] = {}
        for candidate in candidates:
            for obj in self._walk_json(candidate):
                name = _text(obj.get("prop") or obj.get("name") or obj.get("skuName"))
                value = _text(obj.get("value") or obj.get("valueName") or obj.get("skuValue"))
                if name and value and len(name) < 60 and len(value) < 120:
                    variants.setdefault(name, set()).add(value)
        return [ProductVariant(name=name, values=sorted(values)) for name, values in list(variants.items())[:12]]

    def _first_text(self, soup: BeautifulSoup, selector_list: list[str]) -> str:
        for selector in selector_list:
            for node in soup.select(selector):
                text = _text(node.get_text(" "))
                if text and len(text) > 1:
                    return text[:500]
        return ""

    def _extract_images(self, soup: BeautifulSoup, selector_list: list[str], source_url: str) -> list[str]:
        urls: list[str] = []
        for selector in selector_list:
            for image in soup.select(selector):
                raw = image.get("src") or image.get("data-src") or image.get("data-lazyload-src") or image.get("data-original")
                if not raw:
                    continue
                raw = raw.replace("//", "https://", 1) if raw.startswith("//") else raw
                url = urljoin(source_url, raw)
                if self._looks_like_product_image(url):
                    urls.append(url)
        return _unique(urls)

    def _looks_like_product_image(self, url: str) -> bool:
        lowered = url.lower()
        blocked = ("icon", "logo", "qr", "avatar", "sprite", "wangwang", "transparent")
        return lowered.startswith("https://") and any(ext in lowered for ext in (".jpg", ".jpeg", ".png", ".webp")) and not any(token in lowered for token in blocked)

    def _extract_int(self, text: str | None) -> int | None:
        match = QTY_RE.search(text or "")
        return int(match.group(1)) if match else None
