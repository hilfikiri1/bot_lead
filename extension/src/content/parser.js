/**
 * Parse product list from 1688 search/shop/category pages.
 */

function normalizeUrl(url) {
  if (!url) return "";
  const trimmed = url.trim();
  if (trimmed.startsWith("//")) return `https:${trimmed}`;
  if (trimmed.startsWith("/")) return `https://detail.1688.com${trimmed}`;
  return trimmed;
}

function extractOfferId(url) {
  const match = String(url).match(OFFER_LINK_PATTERN);
  return match ? match[1] : null;
}

function extractPrice(text) {
  if (!text) return { price_raw_text: null, price_min_cny: null, price_max_cny: null };
  const numbers = String(text).match(/[\d]+(?:[.,]\d+)?/g);
  if (!numbers || numbers.length === 0) {
    return { price_raw_text: text.trim(), price_min_cny: null, price_max_cny: null };
  }
  const parsed = numbers.map((n) => n.replace(",", "."));
  const min = parsed[0];
  const max = parsed.length > 1 ? parsed[parsed.length - 1] : parsed[0];
  return {
    price_raw_text: text.trim(),
    price_min_cny: min,
    price_max_cny: max,
  };
}

function queryText(root, selectors) {
  for (const selector of selectors) {
    const el = root.querySelector(selector);
    if (el) {
      const text = (el.getAttribute("title") || el.textContent || "").trim();
      if (text) return text;
    }
  }
  return "";
}

function queryImage(root) {
  for (const selector of CARD_IMAGE_SELECTORS) {
    const img = root.querySelector(selector);
    if (!img) continue;
    const src = img.getAttribute("src") || img.getAttribute("data-src") || img.getAttribute("data-lazyload-src");
    if (src && !/icon|logo|avatar|sprite/i.test(src)) {
      return normalizeUrl(src);
    }
  }
  return null;
}

function findOfferLink(root) {
  const links = root.querySelectorAll("a[href*='offer/']");
  for (const link of links) {
    const href = normalizeUrl(link.href || link.getAttribute("href"));
    if (OFFER_LINK_PATTERN.test(href)) return href;
  }
  return null;
}

function findCardRoot(linkEl) {
  let node = linkEl;
  for (let i = 0; i < 8 && node; i += 1) {
    if (node.matches && LIST_CARD_SELECTORS.some((sel) => {
      try { return node.matches(sel); } catch { return false; }
    })) {
      return node;
    }
    node = node.parentElement;
  }
  return linkEl.closest("div, li, article") || linkEl.parentElement;
}

function parseCardElement(card) {
  const sourceUrl = findOfferLink(card);
  if (!sourceUrl) return null;

  const title = queryText(card, CARD_TITLE_SELECTORS);
  if (!title) return null;

  const priceText = queryText(card, CARD_PRICE_SELECTORS);
  const price = extractPrice(priceText);
  const supplier = queryText(card, CARD_SUPPLIER_SELECTORS) || null;
  const thumbnail = queryImage(card);

  return {
    source_url: sourceUrl,
    title_zh: title,
    supplier_name_zh: supplier,
    thumbnail_url: thumbnail,
    offer_id: extractOfferId(sourceUrl),
    ...price,
  };
}

function parseFromOfferLinks() {
  const seen = new Set();
  const products = [];
  const links = document.querySelectorAll("a[href*='detail.1688.com/offer/'], a[href*='/offer/']");

  for (const link of links) {
    const href = normalizeUrl(link.href || link.getAttribute("href"));
    const offerId = extractOfferId(href);
    if (!offerId || seen.has(offerId)) continue;

    const card = findCardRoot(link);
    if (!card) continue;

    const product = parseCardElement(card);
    if (!product || !product.title_zh) continue;

    seen.add(offerId);
    products.push(product);
  }

  return products;
}

function collectImageUrls(value, urls = []) {
  if (!value) return urls;
  if (typeof value === "string" && value.startsWith("http")) {
    urls.push(value);
  } else if (Array.isArray(value)) {
    value.forEach((item) => collectImageUrls(item, urls));
  } else if (typeof value === "object") {
    Object.values(value).forEach((item) => collectImageUrls(item, urls));
  }
  return urls;
}

function flattenObject(data, prefix = "", result = {}) {
  if (Array.isArray(data)) {
    data.forEach((item, index) => flattenObject(item, `${prefix}[${index}]`, result));
    return result;
  }
  if (data && typeof data === "object") {
    Object.entries(data).forEach(([key, value]) => {
      const next = prefix ? `${prefix}.${key}` : key;
      flattenObject(value, next, result);
    });
    return result;
  }
  result[prefix] = data;
  return result;
}

function parseFromPageJson() {
  const products = [];
  const seen = new Set();

  for (const pattern of JSON_SCRIPT_PATTERNS) {
    try {
      const raw = eval(pattern);
      if (!raw) continue;
      const flat = flattenObject(raw);

      const offerEntries = Object.entries(flat).filter(([key, value]) => {
        if (typeof value !== "object" || value === null || Array.isArray(value)) return false;
        const url = value.detailUrl || value.offerUrl || value.url || value.linkUrl;
        return Boolean(url && String(url).includes("offer/"));
      });

      for (const [, value] of offerEntries) {
        const url = normalizeUrl(value.detailUrl || value.offerUrl || value.url || value.linkUrl);
        const offerId = extractOfferId(url);
        if (!offerId || seen.has(offerId)) continue;

        const title = value.subject || value.title || value.offerTitle || value.name;
        if (!title) continue;

        const priceText = String(value.price || value.referencePrice || value.priceRange || "");
        const price = extractPrice(priceText);
        const supplier = value.companyName || value.shopName || value.sellerName || null;
        const images = collectImageUrls(value.imageUrl || value.image || value.picUrl || value);

        products.push({
          source_url: url,
          title_zh: String(title).trim(),
          supplier_name_zh: supplier ? String(supplier).trim() : null,
          thumbnail_url: images[0] ? normalizeUrl(images[0]) : null,
          offer_id: offerId,
          ...price,
        });
        seen.add(offerId);
      }
    } catch {
      // ignore missing globals
    }
  }

  return products;
}

function parseSingleProductPage() {
  const url = window.location.href;
  if (!OFFER_LINK_PATTERN.test(url)) return [];

  const title = document.querySelector("h1")?.textContent?.trim()
    || document.querySelector(".title-text")?.textContent?.trim();
  if (!title) return [];

  const priceText = document.querySelector("[class*='price']")?.textContent?.trim() || "";
  const supplier = document.querySelector(".company-name, .shop-company-name, [class*='company-name']")?.textContent?.trim() || null;
  const img = document.querySelector(".detail-gallery img, [class*='gallery'] img");
  const thumbnail = img
    ? normalizeUrl(img.getAttribute("src") || img.getAttribute("data-src"))
    : null;

  return [{
    source_url: url.split("?")[0],
    title_zh: title,
    supplier_name_zh: supplier,
    thumbnail_url: thumbnail,
    offer_id: extractOfferId(url),
    ...extractPrice(priceText),
  }];
}

function parseProductsOnPage() {
  const fromJson = parseFromPageJson();
  const fromDom = parseFromOfferLinks();
  const merged = [...fromJson, ...fromDom];
  const seen = new Set();
  const unique = [];

  for (const product of merged) {
    const key = product.offer_id || product.source_url;
    if (!key || seen.has(key)) continue;
    if (!product.title_zh || !product.source_url) continue;
    seen.add(key);
    unique.push(product);
  }

  if (unique.length > 0) return unique;
  return parseSingleProductPage();
}
