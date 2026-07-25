"""1688.com product page parser with multi-level extraction strategies."""

from __future__ import annotations

import json
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from playwright.async_api import Page, Response

from app.config import Settings, get_settings
from app.catalog_exceptions import ProductDataNotFoundError
from app.logging_config import get_logger
from app.parser import selectors
from app.parser.browser import (
    check_page_state,
    dismiss_popups,
    save_debug_artifacts,
    smooth_scroll,
    wait_for_key_elements,
)
from app.parser.models import (
    ParsedProduct,
    PriceTier,
    ProductSpecification,
    ProductVariant,
)
from app.parser.session_manager import BrowserSessionManager, get_browser_manager
from app.utils.retry import async_retry

logger = get_logger(__name__)

PRICE_RE = re.compile(r"[\d.,]+")
OFFER_ID_RE = re.compile(r"offer/(\d+)")


class Parser1688:
  """Extract product data from 1688 pages using JSON-first, DOM-fallback strategy."""

  def __init__(
      self,
      browser_manager: BrowserSessionManager | None = None,
      settings: Settings | None = None,
  ) -> None:
      self.browser_manager = browser_manager or get_browser_manager()
      self.settings = settings or get_settings()
      self._captured_json: list[dict[str, Any]] = []

  @async_retry(max_attempts=2, retryable=(Exception,))
  async def parse(self, url: str, debug_dir: Path | None = None) -> ParsedProduct:
      self._captured_json = []

      async with self.browser_manager.new_page() as page:
          page.on("response", self._on_response)

          try:
              await page.goto(url, wait_until="domcontentloaded", timeout=self.settings.playwright_timeout_ms)
              await dismiss_popups(page)
              await wait_for_key_elements(page)
              await smooth_scroll(page)

              content = await page.content()
              check_page_state(page.url, content)

              product = await self._extract(page, url)

              if not product.has_minimum_data():
                  if self.settings.debug_save_page and debug_dir:
                      await save_debug_artifacts(page, debug_dir)
                  raise ProductDataNotFoundError()

              return product
          except ProductDataNotFoundError:
              raise
          except Exception:
              if self.settings.debug_save_page and debug_dir:
                  await save_debug_artifacts(page, debug_dir)
              raise
          finally:
              page.remove_listener("response", self._on_response)

  async def _on_response(self, response: Response) -> None:
      try:
          content_type = response.headers.get("content-type", "")
          if "json" not in content_type:
              return
          if "1688.com" not in response.url:
              return
          body = await response.text()
          data = json.loads(body)
          if isinstance(data, dict):
              self._captured_json.append(data)
      except Exception:
          pass

  async def _extract(self, page: Page, url: str) -> ParsedProduct:
      title_zh = ""
      supplier_name_zh: str | None = None
      price_min: Decimal | None = None
      price_max: Decimal | None = None
      price_raw: str | None = None
      price_tiers: list[PriceTier] = []
      moq: int | None = None
      moq_raw: str | None = None
      variants: list[ProductVariant] = []
      specifications: list[ProductSpecification] = []
      gallery_urls: list[str] = []
      detail_urls: list[str] = []

      # Level 1: JSON from page scripts and XHR
      json_data = await self._extract_json_from_page(page)
      if json_data:
          parsed = self._parse_json_data(json_data)
          title_zh = parsed.get("title_zh", "") or title_zh
          supplier_name_zh = parsed.get("supplier_name_zh") or supplier_name_zh
          price_min = parsed.get("price_min_cny") or price_min
          price_max = parsed.get("price_max_cny") or price_max
          price_raw = parsed.get("price_raw_text") or price_raw
          price_tiers = parsed.get("price_tiers") or price_tiers
          moq = parsed.get("moq") or moq
          moq_raw = parsed.get("moq_raw_text") or moq_raw
          variants = parsed.get("variants") or variants
          specifications = parsed.get("specifications") or specifications
          gallery_urls = parsed.get("gallery_image_urls") or gallery_urls
          detail_urls = parsed.get("detail_image_urls") or detail_urls

      # Level 2: DOM fallbacks
      if not title_zh:
          title_zh = await self._extract_text(page, selectors.TITLE_SELECTORS)
      if not price_raw and price_min is None:
          price_raw = await self._extract_text(page, selectors.PRICE_SELECTORS)
          price_min, price_max = self._parse_price_range(price_raw)
      if not moq_raw and moq is None:
          moq_raw = await self._extract_text(page, selectors.MOQ_SELECTORS)
          moq = self._parse_moq(moq_raw)
      if not supplier_name_zh:
          supplier_name_zh = await self._extract_text(page, selectors.SUPPLIER_SELECTORS) or None
      if not gallery_urls:
          gallery_urls = await self._extract_images(page, selectors.GALLERY_SELECTORS)
      if not detail_urls:
          detail_urls = await self._extract_images(page, selectors.DETAIL_IMAGE_SELECTORS)
      if not specifications:
          specifications = await self._extract_specs_from_dom(page)
      if not variants:
          variants = await self._extract_variants_from_dom(page)
      if not price_tiers:
          price_tiers = await self._extract_price_tiers_from_dom(page)

      gallery_urls = self._dedupe_urls(gallery_urls)
      detail_urls = self._dedupe_urls(detail_urls)

      return ParsedProduct(
          source_url=url,
          title_zh=title_zh.strip() or "商品",
          supplier_name_zh=supplier_name_zh,
          price_min_cny=price_min,
          price_max_cny=price_max,
          price_raw_text=price_raw,
          price_tiers=price_tiers,
          moq=moq,
          moq_raw_text=moq_raw,
          variants=variants,
          specifications=specifications,
          gallery_image_urls=gallery_urls,
          detail_image_urls=detail_urls,
      )

  async def _extract_json_from_page(self, page: Page) -> dict[str, Any] | None:
      merged: dict[str, Any] = {}

      for pattern in selectors.JSON_SCRIPT_PATTERNS:
          try:
              data = await page.evaluate(
                  f"""() => {{
                      const val = {pattern};
                      return val ? JSON.stringify(val) : null;
                  }}"""
              )
              if data:
                  parsed = json.loads(data)
                  if isinstance(parsed, dict):
                      merged.update(parsed)
          except Exception:
              continue

      try:
          ld_scripts = await page.locator(selectors.JSON_LD_SELECTOR).all_text_contents()
          for script in ld_scripts:
              parsed = json.loads(script)
              if isinstance(parsed, dict):
                  merged["_json_ld"] = parsed
      except Exception:
          pass

      for captured in self._captured_json:
          merged.update(captured)

      return merged if merged else None

  def _parse_json_data(self, data: dict[str, Any]) -> dict[str, Any]:
      result: dict[str, Any] = {}
      flat = self._flatten_dict(data)

      title_keys = ("subject", "title", "offerTitle", "productTitle", "name")
      for key in title_keys:
          if key in flat and isinstance(flat[key], str) and flat[key].strip():
              result["title_zh"] = flat[key].strip()
              break

      supplier_keys = ("companyName", "shopName", "sellerName", "supplierName")
      for key in supplier_keys:
          if key in flat and isinstance(flat[key], str):
              result["supplier_name_zh"] = flat[key].strip()
              break

      price_keys = ("price", "priceRange", "referencePrice", "discountPrice", "unitPrice")
      for key in price_keys:
          val = flat.get(key)
          if val is not None:
              if isinstance(val, (int, float, str)):
                  result["price_raw_text"] = str(val)
                  pmin, pmax = self._parse_price_range(str(val))
                  result["price_min_cny"] = pmin
                  result["price_max_cny"] = pmax
                  break

      moq_keys = ("minOrderQuantity", "moq", "beginAmount", "minOrder")
      for key in moq_keys:
          val = flat.get(key)
          if val is not None:
              result["moq_raw_text"] = str(val)
              result["moq"] = self._parse_moq(str(val))
              break

      images: list[str] = []
      for key, val in flat.items():
          if any(k in key.lower() for k in ("image", "img", "photo", "pic")):
              images.extend(self._collect_image_urls(val))
      if images:
          result["gallery_image_urls"] = self._dedupe_urls(images)

      tiers = self._extract_tiers_from_flat(flat)
      if tiers:
          result["price_tiers"] = tiers

      specs = self._extract_specs_from_flat(flat)
      if specs:
          result["specifications"] = specs

      return result

  def _flatten_dict(self, data: Any, prefix: str = "") -> dict[str, Any]:
      items: dict[str, Any] = {}
      if isinstance(data, dict):
          for k, v in data.items():
              key = f"{prefix}.{k}" if prefix else k
              items.update(self._flatten_dict(v, key))
      elif isinstance(data, list):
          for i, v in enumerate(data):
              items.update(self._flatten_dict(v, f"{prefix}[{i}]"))
      else:
          items[prefix] = data
      return items

  def _collect_image_urls(self, val: Any) -> list[str]:
      urls: list[str] = []
      if isinstance(val, str) and val.startswith("http"):
          urls.append(val)
      elif isinstance(val, list):
          for item in val:
              urls.extend(self._collect_image_urls(item))
      elif isinstance(val, dict):
          for v in val.values():
              urls.extend(self._collect_image_urls(v))
      return urls

  def _extract_tiers_from_flat(self, flat: dict[str, Any]) -> list[PriceTier]:
      tiers: list[PriceTier] = []
      for key, val in flat.items():
          if "priceRange" in key.lower() or "priceStep" in key.lower() or "ladderPrice" in key.lower():
              if isinstance(val, list):
                  for item in val:
                      if isinstance(item, dict):
                          tier = self._parse_tier_dict(item)
                          if tier:
                              tiers.append(tier)
      return tiers

  def _parse_tier_dict(self, data: dict[str, Any]) -> PriceTier | None:
      min_q = data.get("min") or data.get("minQuantity") or data.get("beginAmount")
      max_q = data.get("max") or data.get("maxQuantity")
      price = data.get("price") or data.get("unitPrice")
      price_dec = self._to_decimal(str(price)) if price is not None else None
      if price_dec is None and min_q is None:
          return None
      return PriceTier(
          min_quantity=int(min_q) if min_q is not None else None,
          max_quantity=int(max_q) if max_q is not None else None,
          price_cny=price_dec,
          raw_text=json.dumps(data, ensure_ascii=False),
      )

  def _extract_specs_from_flat(self, flat: dict[str, Any]) -> list[ProductSpecification]:
      specs: list[ProductSpecification] = []
      for key, val in flat.items():
          if "attribute" in key.lower() or "spec" in key.lower():
              if isinstance(val, list):
                  for item in val:
                      if isinstance(item, dict):
                          name = item.get("name") or item.get("attributeName") or ""
                          value = item.get("value") or item.get("attributeValue") or ""
                          if name and value:
                              specs.append(ProductSpecification(name_zh=str(name), value_zh=str(value)))
      return specs

  async def _extract_text(self, page: Page, selector_list: list[str]) -> str:
      for selector in selector_list:
          try:
              locator = page.locator(selector).first
              if await locator.count() > 0:
                  text = (await locator.inner_text()).strip()
                  if text:
                      return text
          except Exception:
              continue
      return ""

  async def _extract_images(self, page: Page, selector_list: list[str]) -> list[str]:
      urls: list[str] = []
      for selector in selector_list:
          try:
              elements = page.locator(selector)
              count = await elements.count()
              for i in range(min(count, 20)):
                  src = await elements.nth(i).get_attribute("src")
                  data_src = await elements.nth(i).get_attribute("data-src")
                  data_lazy = await elements.nth(i).get_attribute("data-lazyload-src")
                  for candidate in (src, data_src, data_lazy):
                      if candidate and candidate.startswith("http"):
                          urls.append(candidate)
          except Exception:
              continue
      return self._dedupe_urls(urls)

  async def _extract_specs_from_dom(self, page: Page) -> list[ProductSpecification]:
      specs: list[ProductSpecification] = []
      for selector in selectors.SPEC_TABLE_SELECTORS:
          try:
              rows = page.locator(selector)
              count = await rows.count()
              for i in range(count):
                  row = rows.nth(i)
                  cells = row.locator("td, th, span")
                  if await cells.count() >= 2:
                      name = (await cells.nth(0).inner_text()).strip()
                      value = (await cells.nth(1).inner_text()).strip()
                      if name and value:
                          specs.append(ProductSpecification(name_zh=name, value_zh=value))
          except Exception:
              continue
      return specs

  async def _extract_variants_from_dom(self, page: Page) -> list[ProductVariant]:
      variants: list[ProductVariant] = []
      for selector in selectors.VARIANT_SELECTORS:
          try:
              blocks = page.locator(selector)
              count = await blocks.count()
              for i in range(count):
                  block = blocks.nth(i)
                  name_el = block.locator(".obj-title, .sku-title, label").first
                  name = ""
                  if await name_el.count() > 0:
                      name = (await name_el.inner_text()).strip()
                  values = [
                      v.strip()
                      for v in await block.locator("li, .sku-item, span").all_inner_texts()
                      if v.strip()
                  ]
                  if name and values:
                      variants.append(ProductVariant(name=name, values=values))
          except Exception:
              continue
      return variants

  async def _extract_price_tiers_from_dom(self, page: Page) -> list[PriceTier]:
      tiers: list[PriceTier] = []
      try:
          rows = page.locator("[class*='ladder'] tr, [class*='price-range'] li")
          count = await rows.count()
          for i in range(count):
              text = (await rows.nth(i).inner_text()).strip()
              if text:
                  prices = PRICE_RE.findall(text)
                  nums = [int(n) for n in re.findall(r"\d+", text) if int(n) < 100000]
                  price_dec = self._to_decimal(prices[0]) if prices else None
                  tiers.append(
                      PriceTier(
                          min_quantity=nums[0] if nums else None,
                          max_quantity=nums[1] if len(nums) > 1 else None,
                          price_cny=price_dec,
                          raw_text=text,
                      )
                  )
      except Exception:
          pass
      return tiers

  def _parse_price_range(self, text: str | None) -> tuple[Decimal | None, Decimal | None]:
      if not text:
          return None, None
      numbers = [self._to_decimal(n) for n in PRICE_RE.findall(text)]
      numbers = [n for n in numbers if n is not None]
      if not numbers:
          return None, None
      if len(numbers) == 1:
          return numbers[0], numbers[0]
      return min(numbers), max(numbers)

  def _parse_moq(self, text: str | None) -> int | None:
      if not text:
          return None
      match = re.search(r"(\d+)", text)
      return int(match.group(1)) if match else None

  def _to_decimal(self, value: str) -> Decimal | None:
      try:
          cleaned = value.replace(",", "").strip()
          return Decimal(cleaned)
      except (InvalidOperation, ValueError):
          return None

  def _dedupe_urls(self, urls: list[str]) -> list[str]:
      seen: set[str] = set()
      result: list[str] = []
      for url in urls:
          normalized = url.split("?")[0]
          if normalized not in seen:
              seen.add(normalized)
              result.append(url)
      return result


def parse_html_fixture(html: str, url: str = "https://detail.1688.com/offer/123.html") -> ParsedProduct:
      """Parse saved HTML fixture without browser (for tests)."""
      import re as _re

      title_match = _re.search(r"<h1[^>]*>(.*?)</h1>", html, _re.S)
      title = _re.sub(r"<[^>]+>", "", title_match.group(1)).strip() if title_match else "测试商品"

      img_urls = _re.findall(r'src="(https?://[^"]+\.(?:jpg|jpeg|png|webp)[^"]*)"', html, _re.I)

      price_match = _re.search(r"¥\s*([\d.]+)", html)
      price_min = Decimal(price_match.group(1)) if price_match else None

      return ParsedProduct(
          source_url=url,
          title_zh=title,
          price_min_cny=price_min,
          price_max_cny=price_min,
          price_raw_text=price_match.group(0) if price_match else None,
          gallery_image_urls=img_urls[:5],
      )
