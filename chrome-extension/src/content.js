/**
 * Content script injected into 1688.com pages.
 * Scrapes the visible product listing and returns a structured array.
 *
 * 1688.com uses several different page layouts depending on the section
 * (search results, shop listing, category browse). This scraper tries
 * multiple selector strategies and falls back gracefully.
 */

(function () {
  "use strict";

  // --------------------------------------------------------------------------
  // Selector strategies — ordered from most specific to most generic
  // --------------------------------------------------------------------------
  const CARD_SELECTORS = [
    // Search results page (offer-list)
    ".offer-list-row .offer-list-row-item",
    ".card-list .card-container",
    // Category / discovery page
    "[class*='offerItem']",
    "[class*='card-item']",
    "[class*='offer-item']",
    // Generic product card fallback
    "[data-spm*='offerlist'] li",
    ".app-offer-card",
  ];

  const FIELD_SELECTORS = {
    title: [
      "[class*='title'] a",
      "[class*='offerTitle'] a",
      "h3 a",
      "a[title]",
      "[class*='subject'] a",
    ],
    price: [
      "[class*='price'] em",
      "[class*='price-text']",
      "[class*='priceText']",
      "[class*='price']",
      "em.price",
    ],
    image: [
      "img[data-src]",
      "img[src]",
      "[class*='img'] img",
      "[class*='image'] img",
    ],
    factory: [
      "[class*='company'] a",
      "[class*='factory'] a",
      "[class*='shopName'] a",
      "[class*='shop-name'] a",
      "[class*='companyName']",
      ".company a",
    ],
  };

  // --------------------------------------------------------------------------
  // Helpers
  // --------------------------------------------------------------------------

  /** Return the first element matching any selector in `selectors` within `root`. */
  function queryFirst(root, selectors) {
    for (const sel of selectors) {
      try {
        const el = root.querySelector(sel);
        if (el) return el;
      } catch (_) {
        // invalid selector — skip
      }
    }
    return null;
  }

  /** Normalise a price string: strip HTML entities, collapse whitespace. */
  function cleanPrice(raw) {
    return raw
      .replace(/&nbsp;/g, " ")
      .replace(/[\u00A0\u200B]/g, " ")
      .replace(/\s+/g, " ")
      .trim();
  }

  /** Resolve a possibly-relative or protocol-relative image URL. */
  function resolveImageUrl(src) {
    if (!src) return "";
    if (src.startsWith("//")) return "https:" + src;
    if (src.startsWith("http")) return src;
    try {
      return new URL(src, location.href).href;
    } catch (_) {
      return src;
    }
  }

  /** Simple hash for deduplication. */
  function shortHash(str) {
    let h = 0;
    for (let i = 0; i < str.length; i++) {
      h = (Math.imul(31, h) + str.charCodeAt(i)) | 0;
    }
    return (h >>> 0).toString(16);
  }

  // --------------------------------------------------------------------------
  // Core scraping logic
  // --------------------------------------------------------------------------

  function findCards() {
    for (const sel of CARD_SELECTORS) {
      try {
        const cards = Array.from(document.querySelectorAll(sel));
        if (cards.length > 0) return cards;
      } catch (_) {}
    }
    return [];
  }

  function scrapeCard(card) {
    // --- title ---
    const titleEl = queryFirst(card, FIELD_SELECTORS.title);
    const title = titleEl
      ? (titleEl.getAttribute("title") || titleEl.textContent || "").trim()
      : "";

    // --- product URL ---
    const linkEl = titleEl
      ? titleEl.closest("a") || titleEl
      : card.querySelector("a[href]");
    let productUrl = linkEl ? linkEl.href : "";
    if (productUrl && productUrl.startsWith("//")) {
      productUrl = "https:" + productUrl;
    }

    // --- price ---
    const priceEl = queryFirst(card, FIELD_SELECTORS.price);
    const price = priceEl ? cleanPrice(priceEl.textContent) : "";

    // --- image ---
    const imgEl = queryFirst(card, FIELD_SELECTORS.image);
    const rawSrc = imgEl
      ? imgEl.getAttribute("data-src") || imgEl.getAttribute("src") || ""
      : "";
    // 1688 lazy loads: src may be a tiny placeholder; data-src has the real URL
    const imageUrl = resolveImageUrl(rawSrc);

    // --- factory name ---
    const factoryEl = queryFirst(card, FIELD_SELECTORS.factory);
    const factoryName = factoryEl ? factoryEl.textContent.trim() : "";

    if (!title && !productUrl) return null;

    return {
      id: shortHash(productUrl || title),
      title,
      price,
      imageUrl,
      factoryName,
      productUrl,
    };
  }

  function scrapeProducts() {
    const cards = findCards();
    const seen = new Set();
    const products = [];

    for (const card of cards) {
      const product = scrapeCard(card);
      if (!product) continue;
      if (seen.has(product.id)) continue;
      seen.add(product.id);
      products.push(product);
    }

    return products;
  }

  // --------------------------------------------------------------------------
  // Message listener
  // --------------------------------------------------------------------------

  chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
    if (message.action === "scrapeProducts") {
      try {
        const products = scrapeProducts();
        sendResponse({ success: true, products });
      } catch (err) {
        sendResponse({ success: false, error: err.message });
      }
    }
    // Return true to keep the message channel open for async responses if needed
    return true;
  });
})();
