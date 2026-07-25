"""Multi-layer extraction of product data from a 1688 product page.

Strategy (in priority order):

1. **Embedded JSON / captured XHR** — the most reliable source. We search the
   page HTML for known state variables and also inspect JSON payloads captured
   from XHR/fetch responses while the page loaded.
2. **DOM fallback** — if JSON is missing we query a prioritized list of CSS
   selectors (see :mod:`app.parser.selectors`).
3. **Partial result** — we never abort the whole job for a missing field. As
   long as we obtain a title plus at least one image plus the source URL, the
   pipeline continues.

All selectors and JSON key hints live in :mod:`app.parser.selectors` so new
strategies can be added there without touching this module.
"""

from __future__ import annotations

import json
import re
from decimal import Decimal, InvalidOperation
from urllib.parse import urljoin

from playwright.async_api import Page

from app.config import Settings
from app.exceptions import ProductDataNotFoundError
from app.logging_config import get_logger
from app.parser import selectors
from app.parser.browser import ReadyPage
from app.parser.models import (
    ParsedProduct,
    PriceTier,
    ProductSpecification,
    ProductVariant,
)

logger = get_logger(__name__)

_PRICE_NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)?")
_INT_RE = re.compile(r"\d+")
_ICON_HINTS = ("logo", "icon", "avatar", "qrcode", "qr_code", "sprite", ".gif", "placeholder")


# --------------------------------------------------------------------------- #
# Generic JSON walking helpers
# --------------------------------------------------------------------------- #
def _iter_nodes(obj: object):
    """Yield every dict/list node in a nested JSON structure."""
    stack = [obj]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            yield node
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)


def _deep_find_str(obj: object, keys: tuple[str, ...]) -> str | None:
    """Return the first non-empty string value stored under any of ``keys``."""
    for node in _iter_nodes(obj):
        if not isinstance(node, dict):
            continue
        for key in keys:
            value = node.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
            if isinstance(value, (int, float)):
                return str(value)
    return None


def _collect_image_urls(obj: object) -> list[str]:
    """Collect plausible image URLs from anywhere in a nested JSON structure."""
    found: list[str] = []
    stack: list[object] = [obj]
    while stack:
        node = stack.pop()
        if isinstance(node, str):
            if _is_probable_image_url(node):
                found.append(node)
        elif isinstance(node, dict):
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return found


def _is_probable_image_url(value: str) -> bool:
    lowered = value.lower()
    if not lowered.startswith(("http://", "https://", "//")):
        return False
    return any(ext in lowered for ext in (".jpg", ".jpeg", ".png", ".webp"))


def _normalize_image_url(url: str) -> str:
    if url.startswith("//"):
        return "https:" + url
    return url


def _parse_decimal(text: str) -> Decimal | None:
    match = _PRICE_NUMBER_RE.search(text)
    if not match:
        return None
    raw = match.group(0).replace(",", ".")
    try:
        return Decimal(raw)
    except InvalidOperation:
        return None


def _parse_price_range(text: str) -> tuple[Decimal | None, Decimal | None]:
    numbers = _PRICE_NUMBER_RE.findall(text)
    decimals: list[Decimal] = []
    for raw in numbers:
        try:
            decimals.append(Decimal(raw.replace(",", ".")))
        except InvalidOperation:
            continue
    if not decimals:
        return None, None
    return min(decimals), max(decimals)


def _parse_int(text: str) -> int | None:
    match = _INT_RE.search(text.replace(",", ""))
    if not match:
        return None
    try:
        return int(match.group(0))
    except ValueError:
        return None


# --------------------------------------------------------------------------- #
# Main parser
# --------------------------------------------------------------------------- #
class Parser1688:
    """Extracts a :class:`ParsedProduct` from a loaded page."""

    def __init__(self, settings: Settings):
        self._settings = settings

    async def parse(self, ready: ReadyPage, source_url: str) -> ParsedProduct:
        page = ready.page
        html = await page.content()

        json_blobs = self._collect_json_blobs(html, ready.captured_json)

        title = self._extract_title(json_blobs) or await self._dom_title(page)
        supplier = self._extract_supplier(json_blobs) or await self._dom_supplier(page)

        price_min, price_max, price_raw = self._extract_price(json_blobs)
        if price_min is None and price_max is None:
            price_min, price_max, price_raw = await self._dom_price(page)

        tiers = self._extract_tiers(json_blobs)
        if not tiers:
            tiers = await self._dom_tiers(page)

        moq, moq_raw = self._extract_moq(json_blobs)
        if moq is None:
            moq, moq_raw = await self._dom_moq(page)

        specs = self._extract_specs(json_blobs)
        if not specs:
            specs = await self._dom_specs(page)

        variants = await self._dom_variants(page)

        gallery = self._extract_gallery(json_blobs, source_url)
        if not gallery:
            gallery = await self._dom_gallery(page, source_url)

        detail_images = await self._dom_detail_images(page, source_url)

        product = ParsedProduct(
            source_url=source_url,
            title_zh=(title or "").strip(),
            supplier_name_zh=supplier,
            price_min_cny=price_min,
            price_max_cny=price_max,
            price_raw_text=price_raw,
            price_tiers=tiers,
            moq=moq,
            moq_raw_text=moq_raw,
            variants=variants,
            specifications=specs,
            gallery_image_urls=self._dedupe(gallery)[: self._settings.max_gallery_images * 2],
            detail_image_urls=self._dedupe(detail_images)[: self._settings.max_detail_images * 3],
        )

        if not product.has_minimum_data():
            raise ProductDataNotFoundError()

        logger.info(
            "Parsed product",
            title=bool(product.title_zh),
            gallery=len(product.gallery_image_urls),
            details=len(product.detail_image_urls),
            tiers=len(product.price_tiers),
            specs=len(product.specifications),
        )
        return product

    # ---- Layer 1: JSON --------------------------------------------------- #
    def _collect_json_blobs(self, html: str, captured: list[dict]) -> list[dict]:
        blobs: list[dict] = [b for b in captured if isinstance(b, dict)]

        for marker in selectors.JSON_STATE_MARKERS:
            for candidate in self._extract_balanced_objects(html, marker):
                parsed = self._safe_json(candidate)
                if isinstance(parsed, dict):
                    blobs.append(parsed)

        # JSON-LD blocks.
        for match in re.finditer(
            r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
            html,
            re.DOTALL | re.IGNORECASE,
        ):
            parsed = self._safe_json(match.group(1))
            if isinstance(parsed, dict):
                blobs.append(parsed)
            elif isinstance(parsed, list):
                blobs.extend(item for item in parsed if isinstance(item, dict))

        return blobs

    @staticmethod
    def _extract_balanced_objects(html: str, marker: str) -> list[str]:
        """Return balanced ``{...}`` JSON strings that follow ``marker`` in HTML.

        For a ``<script id="__NEXT_DATA__">`` marker we take the first ``{`` after
        the marker; for assignment markers (``window.X =``) we also start at the
        first ``{`` after the marker. Brace matching respects strings/escapes.
        """
        results: list[str] = []
        search_start = 0
        while True:
            idx = html.find(marker, search_start)
            if idx == -1:
                break
            brace_start = html.find("{", idx)
            if brace_start == -1:
                break
            extracted = Parser1688._match_braces(html, brace_start)
            if extracted:
                results.append(extracted)
                search_start = brace_start + len(extracted)
            else:
                search_start = idx + len(marker)
            if len(results) >= 5:
                break
        return results

    @staticmethod
    def _match_braces(text: str, start: int) -> str | None:
        depth = 0
        in_string = False
        escape = False
        quote = ""
        for i in range(start, len(text)):
            ch = text[i]
            if in_string:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == quote:
                    in_string = False
                continue
            if ch in ('"', "'"):
                in_string = True
                quote = ch
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return text[start : i + 1]
        return None

    @staticmethod
    def _safe_json(candidate: str) -> dict | list | None:
        candidate = candidate.strip()
        try:
            return json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            # Try to trim to the outermost balanced braces.
            start = candidate.find("{")
            end = candidate.rfind("}")
            if start != -1 and end > start:
                try:
                    return json.loads(candidate[start : end + 1])
                except (json.JSONDecodeError, ValueError):
                    return None
            return None

    def _extract_title(self, blobs: list[dict]) -> str | None:
        for blob in blobs:
            title = _deep_find_str(blob, selectors.JSON_TITLE_KEYS)
            if title and len(title) > 3:
                return title
        return None

    def _extract_supplier(self, blobs: list[dict]) -> str | None:
        for blob in blobs:
            supplier = _deep_find_str(blob, selectors.JSON_SUPPLIER_KEYS)
            if supplier:
                return supplier
        return None

    def _extract_price(
        self, blobs: list[dict]
    ) -> tuple[Decimal | None, Decimal | None, str | None]:
        for blob in blobs:
            raw = _deep_find_str(blob, selectors.JSON_PRICE_KEYS)
            if raw:
                low, high = _parse_price_range(raw)
                if low is not None:
                    return low, high, raw
        return None, None, None

    def _extract_tiers(self, blobs: list[dict]) -> list[PriceTier]:
        tiers: list[PriceTier] = []
        for blob in blobs:
            for node in _iter_nodes(blob):
                if not isinstance(node, dict):
                    continue
                has_price = any(k in node for k in ("price", "priceText"))
                has_qty = any(k in node for k in ("beginAmount", "quantity", "startAmount"))
                if has_price and has_qty:
                    price_val = node.get("price") or node.get("priceText")
                    qty_val = node.get("beginAmount") or node.get("quantity") or node.get(
                        "startAmount"
                    )
                    price_dec = _parse_decimal(str(price_val)) if price_val is not None else None
                    qty_int = _parse_int(str(qty_val)) if qty_val is not None else None
                    if price_dec is not None:
                        tiers.append(
                            PriceTier(
                                min_quantity=qty_int,
                                max_quantity=None,
                                price_cny=price_dec,
                                raw_text=f"{qty_val}+ : {price_val}",
                            )
                        )
        return tiers[:6]

    def _extract_moq(self, blobs: list[dict]) -> tuple[int | None, str | None]:
        for blob in blobs:
            raw = _deep_find_str(blob, selectors.JSON_MOQ_KEYS)
            if raw:
                value = _parse_int(raw)
                if value is not None:
                    return value, raw
        return None, None

    def _extract_specs(self, blobs: list[dict]) -> list[ProductSpecification]:
        specs: list[ProductSpecification] = []
        for blob in blobs:
            for node in _iter_nodes(blob):
                if not isinstance(node, dict):
                    continue
                name = node.get("attributeName") or node.get("name")
                value = node.get("attributeValue") or node.get("value")
                if (
                    isinstance(name, str)
                    and isinstance(value, str)
                    and name.strip()
                    and value.strip()
                    and len(name) < 60
                    and len(value) < 200
                ):
                    specs.append(
                        ProductSpecification(name_zh=name.strip(), value_zh=value.strip())
                    )
        # De-duplicate by name.
        seen: set[str] = set()
        unique: list[ProductSpecification] = []
        for spec in specs:
            if spec.name_zh not in seen:
                seen.add(spec.name_zh)
                unique.append(spec)
        return unique[:30]

    def _extract_gallery(self, blobs: list[dict], base_url: str) -> list[str]:
        urls: list[str] = []
        for blob in blobs:
            urls.extend(_collect_image_urls(blob))
        return [
            _normalize_image_url(urljoin(base_url, u))
            for u in urls
            if not self._is_icon(u)
        ]

    # ---- Layer 2: DOM ---------------------------------------------------- #
    async def _dom_title(self, page: Page) -> str | None:
        for selector in selectors.TITLE_SELECTORS:
            text = await self._first_text(page, selector)
            if text and len(text) > 3:
                return text
        return None

    async def _dom_supplier(self, page: Page) -> str | None:
        for selector in selectors.SUPPLIER_SELECTORS:
            text = await self._first_text(page, selector)
            if text:
                return text
        return None

    async def _dom_price(self, page: Page):
        for selector in selectors.PRICE_RANGE_SELECTORS + selectors.PRICE_SELECTORS:
            text = await self._first_text(page, selector)
            if text and any(ch.isdigit() for ch in text):
                low, high = _parse_price_range(text)
                if low is not None:
                    return low, high, text
        return None, None, None

    async def _dom_tiers(self, page: Page) -> list[PriceTier]:
        tiers: list[PriceTier] = []
        for selector in selectors.PRICE_TIER_ROW_SELECTORS:
            try:
                rows = page.locator(selector)
                count = min(await rows.count(), 8)
            except Exception:  # noqa: BLE001
                continue
            for idx in range(count):
                try:
                    text = (await rows.nth(idx).inner_text()).strip()
                except Exception:  # noqa: BLE001
                    continue
                if not text or not any(ch.isdigit() for ch in text):
                    continue
                price = _parse_decimal(text)
                qty = _parse_int(text)
                if price is not None:
                    tiers.append(
                        PriceTier(
                            min_quantity=qty,
                            max_quantity=None,
                            price_cny=price,
                            raw_text=text[:120],
                        )
                    )
            if tiers:
                break
        return tiers[:6]

    async def _dom_moq(self, page: Page):
        for selector in selectors.MOQ_SELECTORS:
            text = await self._first_text(page, selector)
            if text and any(ch.isdigit() for ch in text):
                return _parse_int(text), text
        return None, None

    async def _dom_specs(self, page: Page) -> list[ProductSpecification]:
        specs: list[ProductSpecification] = []
        for selector in selectors.SPEC_ROW_SELECTORS:
            try:
                rows = page.locator(selector)
                count = min(await rows.count(), 40)
            except Exception:  # noqa: BLE001
                continue
            for idx in range(count):
                try:
                    text = (await rows.nth(idx).inner_text()).strip()
                except Exception:  # noqa: BLE001
                    continue
                pair = self._split_kv(text)
                if pair:
                    specs.append(ProductSpecification(name_zh=pair[0], value_zh=pair[1]))
            if specs:
                break
        return specs[:30]

    async def _dom_variants(self, page: Page) -> list[ProductVariant]:
        variants: list[ProductVariant] = []
        for selector in selectors.VARIANT_SELECTORS:
            try:
                rows = page.locator(selector)
                count = min(await rows.count(), 10)
            except Exception:  # noqa: BLE001
                continue
            for idx in range(count):
                try:
                    text = (await rows.nth(idx).inner_text()).strip()
                except Exception:  # noqa: BLE001
                    continue
                if not text:
                    continue
                lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
                if len(lines) >= 2:
                    variants.append(ProductVariant(name=lines[0][:50], values=lines[1:6]))
            if variants:
                break
        return variants[:6]

    async def _dom_gallery(self, page: Page, base_url: str) -> list[str]:
        return await self._collect_dom_images(
            page, selectors.GALLERY_IMAGE_SELECTORS, base_url
        )

    async def _dom_detail_images(self, page: Page, base_url: str) -> list[str]:
        return await self._collect_dom_images(
            page, selectors.DETAIL_IMAGE_SELECTORS, base_url
        )

    async def _collect_dom_images(
        self, page: Page, selector_list: list[str], base_url: str
    ) -> list[str]:
        urls: list[str] = []
        for selector in selector_list:
            try:
                imgs = page.locator(selector)
                count = min(await imgs.count(), 60)
            except Exception:  # noqa: BLE001
                continue
            for idx in range(count):
                node = imgs.nth(idx)
                for attr in ("src", "data-src", "data-lazy-src", "data-original"):
                    try:
                        value = await node.get_attribute(attr)
                    except Exception:  # noqa: BLE001
                        value = None
                    if value and not self._is_icon(value):
                        urls.append(_normalize_image_url(urljoin(base_url, value)))
                        break
            if urls:
                break
        return urls

    # ---- helpers --------------------------------------------------------- #
    async def _first_text(self, page: Page, selector: str) -> str | None:
        try:
            locator = page.locator(selector).first
            if await locator.count() == 0:
                return None
            text = (await locator.inner_text()).strip()
            return text or None
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def _split_kv(text: str) -> tuple[str, str] | None:
        for sep in ("：", ":", "\t", "\n"):
            if sep in text:
                left, _, right = text.partition(sep)
                left, right = left.strip(), right.strip()
                if left and right and len(left) < 60 and len(right) < 200:
                    return left, right
        return None

    @staticmethod
    def _is_icon(url: str) -> bool:
        lowered = url.lower()
        return any(hint in lowered for hint in _ICON_HINTS)

    @staticmethod
    def _dedupe(urls: list[str]) -> list[str]:
        seen: set[str] = set()
        result: list[str] = []
        for url in urls:
            key = url.split("?")[0]
            if key not in seen:
                seen.add(key)
                result.append(url)
        return result
