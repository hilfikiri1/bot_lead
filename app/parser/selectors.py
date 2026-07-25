"""CSS selectors and patterns for 1688 product pages.

Add new selectors here when 1688 changes page structure.
Each list is tried in order until a match is found.
"""

from __future__ import annotations

# --- Page state indicators ---
LOGIN_INDICATORS = [
    "login.taobao.com",
    "passport.1688.com",
    "请登录",
    "登录",
]

CAPTCHA_INDICATORS = [
    "captcha",
    "verify",
    "验证",
    "滑块",
    "nc_",
    "baxia",
]

NOT_FOUND_INDICATORS = [
    "商品不存在",
    "页面不存在",
    "404",
    "已下架",
    "已删除",
]

# --- Dismiss popups / cookie banners ---
POPUP_CLOSE_SELECTORS = [
    ".close-btn",
    ".J_MIDDLEWARE_DIALOG_WIDGET .close",
    "[class*='close']",
    "button:has-text('关闭')",
    "button:has-text('知道了')",
    "button:has-text('同意')",
    ".next-dialog-close",
]

# --- Title ---
TITLE_SELECTORS = [
    "h1.title-text",
    ".title-text",
    ".mod-detail-title h1",
    "[class*='title'] h1",
    "h1",
]

# --- Price ---
PRICE_SELECTORS = [
    ".price-text",
    ".mod-detail-price .value",
    "[class*='price'] .value",
    ".price-original",
    ".price-range",
    "[class*='price']",
]

# --- MOQ ---
MOQ_SELECTORS = [
    "[class*='min-order']",
    "[class*='moq']",
    ".unit-detail-amount",
    "span:has-text('起订')",
    "span:has-text('最小起订')",
]

# --- Supplier ---
SUPPLIER_SELECTORS = [
    ".company-name",
    ".shop-company-name",
    "[class*='company-name']",
    ".mod-detail-company a",
    ".shop-name",
]

# --- Gallery images ---
GALLERY_SELECTORS = [
    ".detail-gallery img",
    ".mod-detail-gallery img",
    "[class*='gallery'] img",
    ".tab-content img",
    ".vertical-img img",
    ".box-img img",
]

# --- Detail/description images ---
DETAIL_IMAGE_SELECTORS = [
    ".detail-desc-module img",
    "#desc-lazyload-container img",
    ".mod-detail-desc img",
    "[class*='description'] img",
    "#detail img",
    ".content-detail img",
]

# --- Specifications ---
SPEC_TABLE_SELECTORS = [
    ".mod-detail-attributes table tr",
    ".offer-attr-list li",
    "[class*='attributes'] tr",
    ".obj-content table tr",
]

# --- Variants / SKU ---
VARIANT_SELECTORS = [
    ".obj-sku .obj-content",
    "[class*='sku'] .obj-content",
    ".mod-detail-sku",
]

# --- JSON extraction patterns ---
JSON_SCRIPT_PATTERNS = [
    "window.__INIT_DATA__",
    "window.__INITIAL_STATE__",
    "window.detailData",
    "window.g_config",
    "window.pageData",
]

JSON_LD_SELECTOR = 'script[type="application/ld+json"]'

# --- Key elements to wait for ---
KEY_ELEMENT_SELECTORS = [
    "h1",
    ".title-text",
    ".mod-detail-title",
    "[class*='gallery']",
]

# --- Scroll settings ---
MAX_SCROLL_ATTEMPTS = 8
SCROLL_PAUSE_MS = 500
