TITLE_SELECTORS = [
    "h1.d-title",
    "h1[data-testid='product-title']",
    "h1",
    ".title-text",
]

PRICE_SELECTORS = [
    "[data-testid='price']",
    ".price",
    ".price-now",
    ".od-pc-offer-price",
]

MOQ_SELECTORS = [
    ".moq",
    "[data-testid='moq']",
    ".quantity-rule",
]

SUPPLIER_SELECTORS = [
    ".company-name",
    "[data-testid='supplier-name']",
    ".supplier-info .name",
]

GALLERY_IMAGE_SELECTORS = [
    ".sku-image img",
    ".detail-gallery img",
    ".main-image img",
]

DETAIL_IMAGE_SELECTORS = [
    ".desc-lazyload-container img",
    ".detail-desc img",
    "#mod-detail-description img",
]

SPEC_ROW_SELECTORS = [
    ".spec-item",
    ".offer-attr-item",
    "table tr",
]

VARIANT_SELECTORS = [
    ".sku-prop",
    ".sku-item-wrapper",
]

COOKIE_CLOSE_SELECTORS = [
    "button:has-text('Accept')",
    "button:has-text('同意')",
    ".cookie-btn-close",
    ".close-btn",
]
