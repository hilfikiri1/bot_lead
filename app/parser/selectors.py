from __future__ import annotations

TITLE_SELECTORS = ["h1", "[class*='title']", "[data-testid*='title']", ".mod-detail-title", ".offer-title"]
PRICE_SELECTORS = ["[class*='price']", ".price-now", ".price-range", ".price-content", "[data-testid*='price']"]
MOQ_SELECTORS = ["[class*='moq']", "[class*='minimum']", "[class*='begin']", ".amount-on-sale"]
SUPPLIER_SELECTORS = ["[class*='company']", "[class*='supplier']", "[class*='seller']", ".company-name"]
GALLERY_IMAGE_SELECTORS = ["img[src*='alicdn.com']", "img[data-src*='alicdn.com']", "img[src*='cbu01.alicdn.com']", "img[data-lazyload-src]"]
DETAIL_IMAGE_SELECTORS = ["#desc img", "[class*='detail'] img", "[class*='description'] img", "[class*='desc'] img"]
SPECIFICATION_SELECTORS = ["[class*='prop'] li", "[class*='attribute'] li", "table tr", ".obj-content li"]
VARIANT_SELECTORS = ["[class*='sku']", "[class*='variant']", "[class*='spec']"]
POPUP_CLOSE_SELECTORS = ["button:has-text('关闭')", "button:has-text('知道了')", "button:has-text('我知道了')", ".next-dialog-close", ".close", "[aria-label='Close']"]
AUTH_MARKERS = ["login.taobao.com", "passport.1688.com", "请登录", "登录"]
CAPTCHA_MARKERS = ["captcha", "验证码", "滑块", "安全验证"]
