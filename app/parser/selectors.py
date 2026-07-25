"""
CSS / XPath selectors for 1688 product pages.

All selectors are grouped by the data they extract.
When 1688 updates its page layout, add new selectors to the RELEVANT list
without removing existing ones – the parser tries them in order and uses the
first match. This makes the parser resilient to incremental redesigns.
"""
from __future__ import annotations

from typing import Sequence

# ── Product title ─────────────────────────────────────────────────────────────
TITLE_SELECTORS: Sequence[str] = [
    "h1.product-title",
    "h1.mod-detail-title",
    "h1[data-spm-anchor-id]",
    ".product-title-text",
    ".title-text",
    "h1",
    # Newer layout
    ".offer-title",
    "div[class*='Title'] h1",
    "div[class*='title'] h1",
]

# ── Price (single value or range) ─────────────────────────────────────────────
PRICE_SELECTORS: Sequence[str] = [
    ".price-common-price em.price-unit",
    ".price-common-price",
    ".detail-price .price",
    ".price",
    ".price-range",
    ".mod-price",
    "span[class*='Price']",
    "div[class*='price']",
]

# ── Tiered / ladder prices ────────────────────────────────────────────────────
PRICE_TIER_SELECTORS: Sequence[str] = [
    ".price-ladder tr",
    "table.price-step tr",
    ".ladder-price tr",
    ".mod-price-table tr",
    "table[class*='price'] tr",
    "table[class*='Price'] tr",
]

PRICE_TIER_QTY_CELL: Sequence[str] = ["td:first-child", "th:first-child"]
PRICE_TIER_PRICE_CELL: Sequence[str] = ["td:last-child", "th:last-child"]

# ── MOQ ───────────────────────────────────────────────────────────────────────
MOQ_SELECTORS: Sequence[str] = [
    ".moq .moq-value",
    ".min-order",
    ".order-limit",
    "span[class*='moq' i]",
    "span[class*='minOrder' i]",
    "div[class*='MinOrder']",
    ".mod-detail-desc .moq",
]

# ── Gallery images ────────────────────────────────────────────────────────────
GALLERY_THUMB_SELECTORS: Sequence[str] = [
    ".img-gallery .main-img img",
    ".detail-gallery .thumb-list img",
    ".gallery-list img",
    ".mod-gallery img",
    ".album-list img",
    "div[class*='Gallery'] img",
    "div[class*='gallery'] img",
    "ul.imgs-list img",
    ".main-img img",
]

GALLERY_MAIN_IMG_SELECTORS: Sequence[str] = [
    ".main-img-wrap .main-img img",
    ".main-pic img",
    "#main-img-wrap img",
    ".detail-img img",
]

# ── Detail / description images ───────────────────────────────────────────────
DETAIL_IMG_SELECTORS: Sequence[str] = [
    ".mod-detail-desc img",
    ".detail-desc img",
    ".description img",
    "div[class*='Description'] img",
    "div[class*='desc'] img",
    ".detail-content img",
    ".product-desc img",
]

# ── Specifications ────────────────────────────────────────────────────────────
SPEC_ROW_SELECTORS: Sequence[str] = [
    ".attributes-list li",
    ".mod-detail-attributes li",
    ".detail-props li",
    "ul[class*='attributes'] li",
    "ul[class*='Attribute'] li",
    "table.attributes tr",
    "table[class*='detail'] tr",
    ".props-list .item",
    "div[class*='Prop'] > div",
]

SPEC_NAME_SELECTORS: Sequence[str] = [
    "dt",
    ".attr-name",
    ".label",
    "span:first-child",
    "td:first-child",
]

SPEC_VALUE_SELECTORS: Sequence[str] = [
    "dd",
    ".attr-value",
    ".value",
    "span:last-child",
    "td:last-child",
]

# ── SKU / variants ────────────────────────────────────────────────────────────
VARIANT_GROUP_SELECTORS: Sequence[str] = [
    ".sku-item",
    ".mod-sku .sku-list .sku-title",
    ".sku-spec .sku-head",
    "div[class*='Sku'] div[class*='title']",
    ".attribute-item",
]

VARIANT_VALUE_SELECTORS: Sequence[str] = [
    ".sku-item .sku-name",
    ".sku-values span",
    ".sku-img-list span",
    "div[class*='SkuValue']",
]

# ── Supplier name ─────────────────────────────────────────────────────────────
SUPPLIER_SELECTORS: Sequence[str] = [
    ".company-name a",
    ".seller-name a",
    ".shop-name a",
    ".company-name-text",
    "a[class*='companyName']",
    "a[class*='company']",
    ".supplier-name",
    ".store-name",
    "div[class*='ShopName'] a",
    "div[class*='Company'] a",
]

# ── Captcha / login detection ────────────────────────────────────────────────
CAPTCHA_INDICATORS: Sequence[str] = [
    "#nocaptcha",
    "#J_cap_vc",
    ".bx-ua-login",
    "#login-form",
    "form[action*='login']",
    "form[action*='passport']",
    "#J_Form",
    "div[class*='captcha' i]",
    "div[class*='slider' i]",
]

# ── Cookie/popup close buttons ────────────────────────────────────────────────
POPUP_CLOSE_SELECTORS: Sequence[str] = [
    "button[aria-label='Close']",
    ".close-btn",
    ".modal-close",
    "[class*='close' i]:not(video)",
    ".cookie-close",
    "#J_CloseBtn",
    ".J_CloseBtn",
]

# ── Page-load readiness indicator ─────────────────────────────────────────────
# The parser waits for ANY of these to appear before scraping.
READY_INDICATORS: Sequence[str] = [
    "h1",
    ".product-title",
    ".offer-title",
    ".detail-gallery",
    ".mod-detail-title",
    "div[class*='Title'] h1",
]
