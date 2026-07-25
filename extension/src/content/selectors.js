/**
 * CSS selectors for 1688 list/search/shop pages.
 * Tried in order until a match is found.
 */
const LIST_CARD_SELECTORS = [
  ".space-offer-card-box",
  ".offer-card",
  ".common-offer-card",
  ".sm-offer-item",
  "[data-offer-id]",
  ".gallery-offer-card",
  ".offer-wrapper",
  ".card-item",
];

const CARD_TITLE_SELECTORS = [
  ".title",
  ".offer-title",
  "[class*='title']",
  "a[title]",
  "h3",
  "h4",
];

const CARD_PRICE_SELECTORS = [
  ".price",
  "[class*='price']",
  ".mojar-element-price",
  ".price-range",
];

const CARD_SUPPLIER_SELECTORS = [
  ".company-name",
  ".shop-company-name",
  "[class*='company']",
  ".seller-name",
  ".shop-name",
];

const CARD_IMAGE_SELECTORS = [
  "img",
  "[class*='image'] img",
  ".img img",
];

const OFFER_LINK_PATTERN = /detail\.1688\.com\/offer\/(\d+)/i;

const JSON_SCRIPT_PATTERNS = [
  "window.__INIT_DATA__",
  "window.__INITIAL_STATE__",
  "window.pageData",
  "window.g_config",
];
