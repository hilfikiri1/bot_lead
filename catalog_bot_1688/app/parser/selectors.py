"""Central registry of extraction strategies for 1688 product pages.

1688 changes its markup frequently and serves several different page layouts
(desktop ``detail.1688.com``, mobile ``m.1688.com``, "winport" shops, ...). To
stay resilient we DO NOT bind the parser to a single CSS selector.

>>> HOW TO ADD NEW SELECTORS <<<
When 1688 changes its DOM, append new CSS selectors to the relevant list below.
The parser tries every selector in order and uses the first non-empty match, so
new selectors can be added at the top (preferred) or bottom (fallback) without
touching the parser logic. Keep the most specific / most reliable selectors
first. The same idea applies to the JSON key hints used for the embedded-state
strategy.
"""

from __future__ import annotations

# --- Layer 1: embedded JSON / JS state -------------------------------------
# Left-hand-side markers of JS assignments that hold the page state as JSON. The
# parser locates each marker in the raw HTML and then extracts the following
# balanced ``{ ... }`` object (so nested braces are handled correctly).
#
# >>> Add new state variables here when 1688 changes its page bootstrap. <<<
JSON_STATE_MARKERS: list[str] = [
    "window.__INIT_DATA__",
    "window.__GLOBAL_DATA__",
    "window.__AISN_DATA__",
    "window.runParams",
    "window.__NEXT_DATA__",
    'id="__NEXT_DATA__"',
]

# JSON keys that commonly hold the pieces of data we care about. These are used
# as hints when walking an arbitrary nested JSON structure.
JSON_TITLE_KEYS = ("subject", "title", "offerTitle", "productName", "name")
JSON_PRICE_KEYS = ("price", "currentPrice", "showPrice", "priceInfo", "skuPriceRange")
JSON_SUPPLIER_KEYS = ("companyName", "shopName", "sellerNick", "memberName", "supplierName")
JSON_MOQ_KEYS = ("beginAmount", "minOrderQuantity", "moq", "startAmount")
JSON_IMAGE_KEYS = ("images", "imageList", "offerImgList", "pcDetailImages", "galleryImageList")

# --- Layer 2: DOM fallback selectors ---------------------------------------
TITLE_SELECTORS: list[str] = [
    "h1.title-text",
    "div.title-first-column h1",
    "div.od-pc-offer-title",
    "div.mod-detail-title h1",
    "h1[class*='title']",
    "div[class*='offer-title']",
    "title",
]

PRICE_SELECTORS: list[str] = [
    "div.price-original span.value",
    "div[class*='price'] span[class*='value']",
    "div.od-pc-offer-price span.price",
    "span.price-now",
    "div[class*='price-range']",
    "div[class*='offer-price']",
]

PRICE_RANGE_SELECTORS: list[str] = [
    "div[class*='price-range']",
    "div.price-module div[class*='price']",
    "div[class*='priceRange']",
]

PRICE_TIER_ROW_SELECTORS: list[str] = [
    "div.price-item",
    "div[class*='price-list'] div[class*='price-item']",
    "table.price-table tr",
    "div[class*='ladder'] div[class*='item']",
]

MOQ_SELECTORS: list[str] = [
    "div.mod-detail-purchasing div[class*='amount']",
    "div[class*='minOrder']",
    "div[class*='begin-amount']",
    "span[class*='moq']",
]

GALLERY_IMAGE_SELECTORS: list[str] = [
    "div[class*='detail-gallery'] img",
    "div[class*='od-gallery'] img",
    "ul[class*='thumbnails'] img",
    "div[class*='gallery'] img",
    "div[class*='preview'] img",
]

DETAIL_IMAGE_SELECTORS: list[str] = [
    "div[class*='detail-desc'] img",
    "div[class*='description'] img",
    "div#desc-lazyload-container img",
    "div[class*='offer-detail'] img",
]

SPEC_ROW_SELECTORS: list[str] = [
    "div.offer-attr-list div.offer-attr-item",
    "ul.obj-content li",
    "table.od-pc-attribute tr",
    "div[class*='attribute'] div[class*='item']",
    "div[class*='prop'] li",
]

VARIANT_SELECTORS: list[str] = [
    "div.sku-prop",
    "div[class*='sku-item']",
    "div[class*='prop-item']",
]

SUPPLIER_SELECTORS: list[str] = [
    "a.company-name",
    "div[class*='company-name']",
    "a[class*='shop-name']",
    "div[class*='supplier'] a",
]

# --- Cookie / popup dismissal ----------------------------------------------
# Buttons that close cookie banners or login/upgrade popups. Clicked
# best-effort; failures are ignored.
DISMISS_SELECTORS: list[str] = [
    "div.next-dialog-close",
    "a.close",
    "button[class*='close']",
    "div[class*='cookie'] button",
    "div[class*='consent'] button",
    "span.icon-close",
]

# --- CAPTCHA / login detection ---------------------------------------------
# Presence of any of these (in URL or DOM) signals we must ask the admin to
# refresh the session.
CAPTCHA_URL_HINTS: list[str] = [
    "login.1688.com",
    "login.taobao.com",
    "passport",
    "captcha",
    "punish",
    "_____tmd_____",
    "nc.1688.com",
]

CAPTCHA_DOM_SELECTORS: list[str] = [
    "div.nc-container",
    "div#nc_1_wrapper",
    "div[class*='captcha']",
    "div[class*='sm-login']",
    "iframe[src*='captcha']",
]

# Elements whose appearance signals the product detail is ready.
READY_SELECTORS: list[str] = [
    "h1",
    "div[class*='offer-title']",
    "div[class*='price']",
]
