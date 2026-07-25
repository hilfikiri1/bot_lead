from __future__ import annotations

import json
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Optional

from playwright.async_api import Page

from app.exceptions import ProductDataNotFoundError
from app.logging_config import get_logger
from app.parser.models import (
    ParsedProduct,
    PriceTier,
    ProductSpecification,
    ProductVariant,
)
from app.parser.selectors import (
    DETAIL_IMG_SELECTORS,
    GALLERY_MAIN_IMG_SELECTORS,
    GALLERY_THUMB_SELECTORS,
    MOQ_SELECTORS,
    PRICE_SELECTORS,
    PRICE_TIER_PRICE_CELL,
    PRICE_TIER_QTY_CELL,
    PRICE_TIER_SELECTORS,
    SPEC_NAME_SELECTORS,
    SPEC_ROW_SELECTORS,
    SPEC_VALUE_SELECTORS,
    SUPPLIER_SELECTORS,
    TITLE_SELECTORS,
    VARIANT_GROUP_SELECTORS,
    VARIANT_VALUE_SELECTORS,
)

logger = get_logger(__name__)


# ── Utility helpers ───────────────────────────────────────────────────────────

def _clean_text(text: str | None) -> str:
    if not text:
        return ""
    return " ".join(text.split()).strip()


def _to_decimal(raw: str) -> Optional[Decimal]:
    # Extract numeric part; handle "¥12.5 - ¥30.0", "12.5~30", etc.
    nums = re.findall(r"\d+(?:[.,]\d+)?", raw.replace(",", ""))
    for n in nums:
        try:
            return Decimal(n)
        except InvalidOperation:
            continue
    return None


def _parse_price_range(raw: str) -> tuple[Optional[Decimal], Optional[Decimal]]:
    # Remove currency symbols and separators
    cleaned = re.sub(r"[¥￥$€£\s]", "", raw)
    # Try to find two values e.g. "12.5~30.0" or "12.5-30.0"
    parts = re.split(r"[~\-–—/]", cleaned)
    decimals = []
    for p in parts:
        d = _to_decimal(p)
        if d is not None:
            decimals.append(d)
    if len(decimals) >= 2:
        return min(decimals), max(decimals)
    if len(decimals) == 1:
        return decimals[0], None
    return None, None


def _safe_int(raw: str) -> Optional[int]:
    m = re.search(r"\d+", raw)
    return int(m.group()) if m else None


# ── Level 1: Extract embedded JSON from the page ─────────────────────────────

async def _extract_from_page_json(page: Page) -> dict[str, Any]:
    """
    Attempt to extract structured product data from embedded JavaScript objects.
    Returns a partial dict; keys may be absent if not found.
    """
    result: dict[str, Any] = {}

    # Strategy A: look for window.__INIT_DATA__ or similar globals
    for var_name in [
        "__INIT_DATA__",
        "window.__data__",
        "initData",
        "__GLOBAL_DATA__",
        "PAGE_DATA",
        "detailData",
    ]:
        try:
            val = await page.evaluate(f"window['{var_name}']")
            if val and isinstance(val, dict):
                result.update(_extract_fields_from_dict(val))
                logger.debug("json_data_extracted", source=var_name)
                break
        except Exception:
            continue

    # Strategy B: parse inline <script> tags for JSON blobs
    if not result.get("title_zh"):
        scripts = await page.query_selector_all("script:not([src])")
        for script in scripts:
            try:
                content = await script.inner_text()
                if not content or len(content) > 500_000:
                    continue
                # Look for JSON assignments
                match = re.search(
                    r"(?:window\.__INIT_DATA__|initData|detailData)\s*=\s*(\{.+\})\s*;",
                    content,
                    re.DOTALL,
                )
                if match:
                    data = json.loads(match.group(1))
                    fields = _extract_fields_from_dict(data)
                    if fields.get("title_zh"):
                        result.update(fields)
                        logger.debug("json_extracted_from_script_tag")
                        break
            except Exception:
                continue

    # Strategy C: JSON-LD
    if not result.get("title_zh"):
        ld_scripts = await page.query_selector_all('script[type="application/ld+json"]')
        for ld in ld_scripts:
            try:
                data = json.loads(await ld.inner_text())
                if isinstance(data, dict) and data.get("name"):
                    result.setdefault("title_zh", data.get("name", ""))
                    if data.get("image"):
                        imgs = data["image"]
                        if isinstance(imgs, str):
                            imgs = [imgs]
                        result.setdefault("gallery_urls", []).extend(
                            [i for i in imgs if isinstance(i, str)]
                        )
            except Exception:
                continue

    return result


def _extract_fields_from_dict(data: dict[str, Any]) -> dict[str, Any]:
    """
    Recursively walk a nested dict looking for well-known field names
    used by 1688's internal APIs.
    """
    result: dict[str, Any] = {}

    # Title
    for key in ("title", "subject", "name", "offerTitle", "productTitle"):
        val = _deep_get(data, key)
        if isinstance(val, str) and val.strip():
            result["title_zh"] = val.strip()
            break

    # Supplier
    for key in ("companyName", "supplierName", "sellerName", "shopName", "storeName"):
        val = _deep_get(data, key)
        if isinstance(val, str) and val.strip():
            result["supplier_name_zh"] = val.strip()
            break

    # Price
    for key in ("price", "priceInfo", "salePrice"):
        val = _deep_get(data, key)
        if val is not None:
            result["price_raw"] = str(val)
            break

    # Images
    for key in ("images", "mainImgUrl", "imageList", "galleryImages"):
        val = _deep_get(data, key)
        if isinstance(val, list):
            result.setdefault("gallery_urls", []).extend(
                [str(i) for i in val if isinstance(i, str)]
            )
        elif isinstance(val, str):
            result.setdefault("gallery_urls", []).append(val)

    return result


def _deep_get(data: Any, key: str) -> Any:
    """Search for a key in a nested dict/list structure."""
    if isinstance(data, dict):
        if key in data:
            return data[key]
        for v in data.values():
            result = _deep_get(v, key)
            if result is not None:
                return result
    elif isinstance(data, list):
        for item in data:
            result = _deep_get(item, key)
            if result is not None:
                return result
    return None


# ── Level 2: DOM scraping fallbacks ──────────────────────────────────────────

async def _get_text(page: Page, selectors: list[str]) -> str:
    for sel in selectors:
        try:
            el = await page.query_selector(sel)
            if el:
                text = await el.inner_text()
                cleaned = _clean_text(text)
                if cleaned:
                    return cleaned
        except Exception:
            continue
    return ""


async def _get_attribute(page: Page, selectors: list[str], attr: str) -> str:
    for sel in selectors:
        try:
            el = await page.query_selector(sel)
            if el:
                val = await el.get_attribute(attr)
                if val:
                    return val.strip()
        except Exception:
            continue
    return ""


async def _scrape_title(page: Page) -> str:
    return await _get_text(page, list(TITLE_SELECTORS))


async def _scrape_price(page: Page) -> str:
    return await _get_text(page, list(PRICE_SELECTORS))


async def _scrape_moq(page: Page) -> str:
    return await _get_text(page, list(MOQ_SELECTORS))


async def _scrape_supplier(page: Page) -> str:
    return await _get_text(page, list(SUPPLIER_SELECTORS))


async def _scrape_gallery_urls(page: Page) -> list[str]:
    urls: list[str] = []

    # Try thumbnail list
    for sel in GALLERY_THUMB_SELECTORS:
        try:
            imgs = await page.query_selector_all(sel)
            for img in imgs:
                # Prefer data-src (lazy-loaded) over src
                src = (
                    await img.get_attribute("data-src")
                    or await img.get_attribute("data-lazy-src")
                    or await img.get_attribute("src")
                    or ""
                )
                src = _normalise_image_url(src)
                if src and src not in urls:
                    urls.append(src)
            if urls:
                break
        except Exception:
            continue

    # Fallback: main image
    if not urls:
        for sel in GALLERY_MAIN_IMG_SELECTORS:
            try:
                img = await page.query_selector(sel)
                if img:
                    src = (
                        await img.get_attribute("data-src")
                        or await img.get_attribute("src")
                        or ""
                    )
                    src = _normalise_image_url(src)
                    if src:
                        urls.append(src)
                        break
            except Exception:
                continue

    return urls


async def _scrape_detail_urls(page: Page) -> list[str]:
    urls: list[str] = []
    for sel in DETAIL_IMG_SELECTORS:
        try:
            imgs = await page.query_selector_all(sel)
            for img in imgs:
                src = (
                    await img.get_attribute("data-src")
                    or await img.get_attribute("data-lazy-src")
                    or await img.get_attribute("src")
                    or ""
                )
                src = _normalise_image_url(src)
                if src and src not in urls:
                    urls.append(src)
            if urls:
                break
        except Exception:
            continue
    return urls


async def _scrape_price_tiers(page: Page) -> list[PriceTier]:
    tiers: list[PriceTier] = []
    for row_sel in PRICE_TIER_SELECTORS:
        try:
            rows = await page.query_selector_all(row_sel)
            if len(rows) < 2:
                continue
            for row in rows[1:]:  # skip header row
                qty_text = ""
                price_text = ""
                for qty_sel in PRICE_TIER_QTY_CELL:
                    try:
                        el = await row.query_selector(qty_sel)
                        if el:
                            qty_text = _clean_text(await el.inner_text())
                            break
                    except Exception:
                        pass
                for price_sel in PRICE_TIER_PRICE_CELL:
                    try:
                        el = await row.query_selector(price_sel)
                        if el:
                            price_text = _clean_text(await el.inner_text())
                            break
                    except Exception:
                        pass
                if qty_text or price_text:
                    tiers.append(
                        PriceTier(
                            min_quantity=_safe_int(qty_text),
                            price_cny=_to_decimal(price_text),
                            raw_text=f"{qty_text} | {price_text}",
                        )
                    )
            if tiers:
                break
        except Exception:
            continue
    return tiers


async def _scrape_specifications(page: Page) -> list[ProductSpecification]:
    specs: list[ProductSpecification] = []
    for row_sel in SPEC_ROW_SELECTORS:
        try:
            rows = await page.query_selector_all(row_sel)
            if not rows:
                continue
            for row in rows:
                name = ""
                value = ""
                for n_sel in SPEC_NAME_SELECTORS:
                    try:
                        el = await row.query_selector(n_sel)
                        if el:
                            name = _clean_text(await el.inner_text())
                            break
                    except Exception:
                        pass
                for v_sel in SPEC_VALUE_SELECTORS:
                    try:
                        el = await row.query_selector(v_sel)
                        if el:
                            value = _clean_text(await el.inner_text())
                            break
                    except Exception:
                        pass
                if name and value and name != value:
                    specs.append(ProductSpecification(name_zh=name, value_zh=value))
            if specs:
                break
        except Exception:
            continue
    return specs


async def _scrape_variants(page: Page) -> list[ProductVariant]:
    variants: list[ProductVariant] = []
    try:
        groups = await page.query_selector_all(
            ",".join(VARIANT_GROUP_SELECTORS)
        )
        for group in groups:
            name = _clean_text(await group.inner_text())
            if not name:
                continue
            values: list[str] = []
            for val_sel in VARIANT_VALUE_SELECTORS:
                try:
                    els = await group.query_selector_all(val_sel)
                    for el in els:
                        v = _clean_text(await el.inner_text())
                        if v and v != name:
                            values.append(v)
                    if values:
                        break
                except Exception:
                    continue
            if values:
                variants.append(ProductVariant(name=name, values=values))
    except Exception as exc:
        logger.debug("variant_scrape_failed", error=str(exc))
    return variants


def _normalise_image_url(src: str) -> str:
    """Ensure image URL is absolute and uses HTTPS."""
    if not src:
        return ""
    src = src.strip()
    if src.startswith("//"):
        src = "https:" + src
    if not src.startswith("http"):
        return ""
    # Remove size suffixes often appended by 1688 CDN (e.g. _100x100.jpg)
    # and request the original/large version
    src = re.sub(r"_\d+x\d+(\.\w+)$", r"\1", src)
    return src


# ── Main parse function ───────────────────────────────────────────────────────

async def parse_product_page(
    page: Page,
    source_url: str,
    job_dir: Optional[Path] = None,
) -> ParsedProduct:
    """
    Multi-level product parser for 1688.com pages.
    Level 1: embedded JSON  →  Level 2: DOM selectors
    Returns ParsedProduct; raises ProductDataNotFoundError if minimum data
    (title + at least one image) cannot be obtained.
    """
    logger.info("parsing_product_page", url=source_url)

    # ── Level 1 ───────────────────────────────────────────────────────────────
    json_data = await _extract_from_page_json(page)

    # ── Level 2 (DOM fallbacks) ───────────────────────────────────────────────
    title_zh = json_data.get("title_zh") or await _scrape_title(page)
    supplier = json_data.get("supplier_name_zh") or await _scrape_supplier(page)
    price_raw = json_data.get("price_raw") or await _scrape_price(page)
    moq_raw = await _scrape_moq(page)
    gallery_urls = json_data.get("gallery_urls") or await _scrape_gallery_urls(page)
    detail_urls = await _scrape_detail_urls(page)
    price_tiers = await _scrape_price_tiers(page)
    specifications = await _scrape_specifications(page)
    variants = await _scrape_variants(page)

    if not title_zh:
        raise ProductDataNotFoundError(f"Could not extract product title from {source_url}")

    # Parse price range
    price_min, price_max = _parse_price_range(price_raw) if price_raw else (None, None)

    product = ParsedProduct(
        source_url=source_url,
        title_zh=title_zh,
        supplier_name_zh=supplier or None,
        price_min_cny=price_min,
        price_max_cny=price_max,
        price_raw_text=price_raw or None,
        price_tiers=price_tiers,
        moq=_safe_int(moq_raw) if moq_raw else None,
        moq_raw_text=moq_raw or None,
        variants=variants,
        specifications=specifications,
        gallery_image_urls=gallery_urls,
        detail_image_urls=detail_urls,
    )

    if not product.has_minimum_data():
        raise ProductDataNotFoundError(
            f"Minimum product data not available for {source_url}"
        )

    logger.info(
        "product_parsed",
        title=title_zh[:60],
        gallery_count=len(gallery_urls),
        detail_count=len(detail_urls),
        has_price=bool(price_raw),
    )
    return product
