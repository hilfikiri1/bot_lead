# Prompt — Build a Chrome Extension that scrapes 1688 product lists and turns them into a branded PDF catalog

> This file is a **self-contained implementation prompt**. Paste it to a coding agent (or use it yourself)
> to build the feature. It is written specifically for this repository
> (Buy & Bring / **Babrik Solutions** — FastAPI + Celery + Playwright 1688→PDF pipeline).
> Read the "Repository context" section first; it tells you what already exists so you reuse it instead of
> rebuilding it.

---

## Role & objective

You are a senior full-stack engineer. Build a **Manifest V3 Google Chrome extension** plus a small
**backend addition** to this repo so that a user browsing `1688.com` can:

1. **Collect a list of products** directly from the pages they are viewing (search results, a supplier/shop
   page, a category, or their "favorites"/收藏 list), capturing for each product at minimum:
   - **photo(s)** (main image + any thumbnails available on the card),
   - **price** (raw text and, when parseable, a min/max numeric range in CNY),
   - **factory / supplier name** (店铺/公司名),
   - plus: product **title (zh)**, **product URL**, **MOQ** if present, and **offer id**.
2. **Review & edit** the collected list in the extension UI (remove items, fix a price, etc.).
3. **Send the list to the AI + PDF backend**, which translates/normalizes the data (OpenAI) and renders a
   **multi-product branded PDF catalog in the Babrik Solutions house style** ("our type and store"),
   then returns the PDF for download.

The key reason for a browser extension (instead of only the existing server-side Playwright parser): the
extension runs **inside the user's already-authenticated 1688 session**, so it avoids the login walls,
captchas, and anti-bot blocks that headless server scraping hits, and it can capture **many products at once**
from list/search pages rather than one `offer` URL at a time.

---

## Repository context (reuse, don't reinvent)

The backend already contains a complete single-product 1688 → PDF pipeline. Study and reuse these:

| Area | File(s) | What it gives you |
|------|---------|-------------------|
| Parsed product model | `app/parser/models.py` (`ParsedProduct`, `PriceTier`, `ProductVariant`, `ProductSpecification`) | The canonical shape of one product. The extension payload must map cleanly onto this. |
| 1688 selectors/heuristics | `app/parser/selectors.py`, `app/parser/parser_1688.py` | CSS selectors, JSON-blob keys (`window.__INIT_DATA__`, `subject`/`companyName`/`price`/`image` field names) and price/MOQ parsing logic. **Port the JS-equivalent extraction logic into the content script.** |
| AI structuring | `app/ai/openai_client.py`, `app/ai/prompts.py`, `app/ai/schemas.py` (`CatalogContent`) | OpenAI call that turns raw zh product data into structured **Russian** catalog content (prices stay in CNY, no invented specs). |
| PDF rendering | `app/catalog/renderer.py`, `app/catalog/templates/catalog.html`, `app/catalog/static/catalog.css`, `app/catalog/models.py` (`CatalogRenderContext`) | Jinja2 + headless-Chromium PDF. Brand colors/logo come from settings. |
| Job orchestration | `app/services/catalog_service.py`, `app/models/catalog_job.py` | Status lifecycle (`received→validating→parsing→downloading_images→generating_content→rendering_pdf→completed/failed`), image download, cleanup, retention. |
| Config | `app/config.py`, `.env.example` (`CATALOG_*`, `BRAND_*`, `PLAYWRIGHT_*`) | Brand name/colors/logo, model, limits (`CATALOG_MAX_IMAGES`, retention hours, rate limit). |
| Existing entrypoint | Telegram `/catalog` command (`app/services/catalog_telegram_handler.py`, `app/api/telegram.py`) | Reference for how a job is created and the PDF is delivered. |

**Brand defaults** (`.env.example`): `BRAND_NAME=Babrik Solutions`, `BRAND_PRIMARY_COLOR=#0B1F3A`,
`BRAND_ACCENT_COLOR=#D8A34A`, `BRAND_TEXT_COLOR=#20242A`, logo at `app/catalog/static/logo.png`.
The PDF must keep this identity.

---

## Part A — Backend: multi-product catalog endpoint

Add an HTTP API that accepts a pre-parsed product list from the extension and returns a branded catalog PDF.
This bypasses the server-side Playwright parse (the extension already parsed the page) but **reuses the AI +
renderer**.

### A1. New route: `app/api/catalog.py` (mount in `app/main.py`)

- `POST /api/catalog/extension` — accepts the extension payload (see schema below).
  - Behavior modes (config-driven, default async to match `CATALOG_PROCESSING_MODE`):
    - **async**: create a `CatalogJob` (extend the model/enum for a `web_extension` source and a multi-product
      job type; add an Alembic migration in `migrations/versions/`), enqueue a Celery task, return
      `202 Accepted` with `{ "job_id": ... , "status_url": "/api/catalog/{job_id}" }`.
    - **sync** (small lists / dev): render inline and stream back the PDF.
  - Validate: item count within a new `CATALOG_MAX_PRODUCTS_PER_CATALOG` limit; images per product capped by
    existing `CATALOG_MAX_IMAGES`; total download budget honored (`CATALOG_MAX_TOTAL_DOWNLOAD_MB`).
- `GET /api/catalog/{job_id}` — job status JSON (mirror the lifecycle statuses).
- `GET /api/catalog/{job_id}/download` — returns the finished PDF (respect `CATALOG_PDF_RETENTION_HOURS`).
- `POST /api/catalog/extension/preview` (optional) — returns the rendered **HTML** (reuse
  `CatalogRenderer.render_html_string`) so the extension can show a preview without generating a full PDF.

### A2. Request schema (new Pydantic models, e.g. `app/ai/schemas.py` or `app/parser/models.py`)

```jsonc
{
  "catalog_title": "string (optional, e.g. 'Осенняя подборка')",
  "source_context": { "page_url": "string", "page_type": "search|shop|category|favorites|manual" },
  "products": [
    {
      "source_url": "https://detail.1688.com/offer/123.html",
      "offer_id": "123",
      "title_zh": "string",
      "supplier_name_zh": "string|null",      // factory / shop name
      "price_raw_text": "string|null",         // e.g. '¥12.50-15.00'
      "price_min_cny": "number|null",
      "price_max_cny": "number|null",
      "moq_raw_text": "string|null",
      "image_urls": ["https://cbu01.alicdn.com/....jpg"]  // absolute; extension resolves lazy-load srcs
    }
  ]
}
```

Map each item onto a `ParsedProduct`. Because the AI/renderer today are single-product, do **one of**:
- iterate products through the existing `OpenAICatalogClient.generate_catalog_content` + build a
  **multi-product render context** and a new/extended template that lays out one product per section/card; or
- add a batch method that structures all products in fewer OpenAI calls.
Prefer minimal, well-tested changes; keep the single-product path working.

### A3. Multi-product PDF

- Extend `CatalogRenderContext` (or add `MultiCatalogRenderContext`) to hold `list[ProductBlock]` where each
  block carries its `CatalogContent` + local image paths.
- Add/extend a Jinja2 template (e.g. `catalog_multi.html`) reusing `catalog.css` variables and the logo/cover.
  Layout: **cover page** (brand logo, `catalog_title`, date) → **grid/list of product cards** (photo, RU name,
  original zh name, factory/supplier, price in CNY, MOQ) → footer with the existing auto-generated disclaimer.
- Download images with the existing `ImageDownloader` (referer = product `source_url`), honoring limits.

### A4. Security / config

- Add `CATALOG_EXTENSION_API_ENABLED` (default false) and an **API token** setting
  `CATALOG_EXTENSION_API_TOKEN`; require it via an `Authorization: Bearer` (or `X-API-Key`) header.
- Add **CORS** for the extension origin (`chrome-extension://<id>`), configurable via
  `CATALOG_EXTENSION_ALLOWED_ORIGINS`. Do **not** use `*` when a token is required.
- Enforce per-token/IP rate limiting (reuse `CATALOG_RATE_LIMIT_SECONDS` spirit).
- Update `.env.example`, `README.md`, and the config model. Never hardcode secrets.

---

## Part B — Chrome extension (Manifest V3)

Create the extension under a new top-level folder `extension/` (kept out of the Python package; add to
`.gitignore` build artifacts only, not source).

### B1. Files

```
extension/
  manifest.json          # MV3
  src/
    content/scrape1688.js # DOM + embedded-JSON extraction (port of parser_1688 heuristics)
    background/sw.js      # service worker: storage, API calls, download
    popup/popup.html
    popup/popup.js
    popup/popup.css
    options/options.html  # backend base URL + API token + brand-less settings
    options/options.js
    lib/extract.js        # shared extraction helpers (price/MOQ regex, image src resolution)
  icons/ (16/32/48/128)
  README.md
```

### B2. `manifest.json` essentials

- `"manifest_version": 3`.
- `host_permissions`: `https://*.1688.com/*` (and `https://*.alicdn.com/*` for images if needed) plus the
  backend origin.
- `permissions`: `storage`, `activeTab`, `scripting`, `downloads`.
- `content_scripts` matching `https://*.1688.com/*` OR inject on demand via `chrome.scripting.executeScript`
  from the popup (prefer on-demand to reduce noise).
- `action` (popup), `options_page`, `background.service_worker`.
- No remote code; MV3-compliant CSP.

### B3. Content script — list extraction (the core)

Implement robust, **multi-strategy** extraction, mirroring `parser_1688.py`:

1. **Embedded JSON first**: read `window.__INIT_DATA__` / `__INITIAL_STATE__` / `pageData` etc. and any
   `application/ld+json`. Flatten and pick fields by the same key names the Python parser uses
   (`subject`/`title`, `companyName`/`shopName`, `price`/`priceRange`, `minOrderQuantity`, image fields).
2. **DOM fallback**: iterate product cards on the current page. Support the main 1688 surfaces:
   - **search results** (`s.1688.com` / `search` pages) — one card per offer,
   - **shop / supplier pages** — offer grid,
   - **category listing pages**,
   - **favorites / 收藏夹** list.
   For each card extract: title, price text, supplier/shop name (falling back to the shop header on shop
   pages), the offer link (`offer/<id>`), and image `src`/`data-src`/`data-lazyload-src` (resolve lazy
   images; prefer higher-res `alicdn` variants, strip sizing suffixes when safe).
3. **Deduplicate** by `offer_id`/normalized URL (mirror `_dedupe_urls`).
4. Handle **infinite scroll / pagination**: provide a "scan this page" action, and a "scan more" that scrolls
   to load additional cards before re-scanning (bounded, similar to `MAX_SCROLL_ATTEMPTS`).
5. Make selectors data-driven in `lib/extract.js` so they're easy to update when 1688 changes markup (same
   philosophy as `selectors.py`). Fail soft per-card: skip a card that yields no title+image rather than
   aborting the whole scan.

Return an array of product objects matching the Part A payload item shape (image URLs absolute).

### B4. Popup UX

- Buttons: **Scan this page**, **Scan more (scroll)**, **Clear**.
- A scrollable **product list** with checkbox per item, thumbnail, title, price, supplier — inline-editable
  price/supplier, and per-row remove.
- A **catalog title** field.
- **Generate PDF** button → sends selected items to the backend, shows job progress (poll `GET
  /api/catalog/{job_id}`), then triggers `chrome.downloads.download` of the finished PDF (or opens the
  returned blob). Show clear error messages on 4xx/5xx, auth failure, or empty selection.
- Persist the working list in `chrome.storage.local` so it survives popup close and accumulates across pages.

### B5. Options page

- Backend **base URL**, **API token**, request timeout, max products, and a "test connection" button
  (calls a lightweight health/echo). Store in `chrome.storage.sync`.

### B6. Background service worker

- Owns network calls to the backend (attach auth header), job polling with backoff, and downloads.
- Never store the token in content scripts; keep it in the SW/options.

---

## Data flow (end to end)

```
User on 1688 (logged in)
  │  clicks "Scan this page"
  ▼
content script  ──(product[])──►  popup (review/edit)  ──►  background SW
                                                              │  POST /api/catalog/extension  (+ Bearer token)
                                                              ▼
                                                    FastAPI catalog route
                                                              │  create CatalogJob (source=web_extension)
                                                              ▼
                                                    Celery task: per product →
                                                      download images → OpenAI structure (RU, CNY) →
                                                      render multi-product branded PDF
                                                              │
   background SW  ◄──(poll status)──  GET /api/catalog/{job_id}
        │  when completed
        ▼
   chrome.downloads.download( /api/catalog/{job_id}/download )  → Babrik Solutions catalog.pdf
```

---

## Constraints & non-goals

- **Reuse** the existing OpenAI prompt rules (`app/ai/prompts.py`): translate zh→ru faithfully, keep prices in
  **CNY (¥)**, do **not** invent specs/materials/certificates, do **not** add margin or convert currency, use
  "уточняется у поставщика" when data is missing. Keep the auto-disclaimer.
- Do **not** break the existing single-product Telegram `/catalog` flow or its tests.
- Respect all existing limits (`CATALOG_MAX_IMAGES`, download budget, retention, rate limit) and add
  `CATALOG_MAX_PRODUCTS_PER_CATALOG`.
- The extension must not embed any secret at build time; token is user-configured in Options.
- No scraping of pages the user isn't actively viewing; no background crawling. Only act on the current tab on
  explicit user action.
- Keep everything MV3-compliant (no remote/eval'd code).

---

## Deliverables

1. `extension/` — complete MV3 extension (source above), with its own `README.md` (load-unpacked
   instructions, options setup, usage).
2. Backend:
   - `app/api/catalog.py` router mounted in `app/main.py`.
   - New Pydantic request/response models + multi-product render context.
   - `catalog_multi.html` template (or extended `catalog.html`) + any CSS additions.
   - Extended `CatalogJob`/service for the `web_extension` multi-product path + Alembic migration.
   - Celery task for multi-product generation.
   - Config additions in `app/config.py` and `.env.example`
     (`CATALOG_EXTENSION_API_ENABLED`, `CATALOG_EXTENSION_API_TOKEN`,
     `CATALOG_EXTENSION_ALLOWED_ORIGINS`, `CATALOG_MAX_PRODUCTS_PER_CATALOG`).
   - CORS + auth middleware/dependency for the new routes.
3. Tests (`tests/`): endpoint validation/auth/limits, payload→`ParsedProduct` mapping, multi-product render
   smoke test (HTML string is enough to avoid heavy Chromium in CI), and a fixture-based extractor unit test
   for the shared JS extraction helpers if a JS test runner is added (otherwise document manual test steps).
4. Docs: update `README.md` with a "Chrome extension" section and env vars.

---

## Acceptance criteria

- Loading `extension/` unpacked in Chrome, navigating to a 1688 search/shop page, and clicking **Scan this
  page** populates the list with correct **photo, price, and factory/supplier name** for the visible offers.
- Editing/removing items works; the list persists across popup reopen and accumulates across pages.
- **Generate PDF** produces a downloadable **Babrik Solutions–branded** PDF containing one card per selected
  product (photo, RU name, factory, CNY price, MOQ), a branded cover with the catalog title/date, and the
  disclaimer footer.
- Backend rejects requests without a valid token, enforces CORS to the extension origin, and enforces the
  product/image/download limits.
- Existing tests still pass; new tests cover the new endpoint and mapping.

---

## Suggested build order

1. Backend schema + `POST /api/catalog/extension` (sync mode) reusing single-product AI/renderer for N=1 →
   prove the round-trip.
2. Multi-product context + template + Celery async path + status/download endpoints + migration.
3. Extension skeleton (manifest, options, SW, popup shell) hitting the backend with a hand-made payload.
4. Content-script extractor (JSON-first, DOM fallback) for search pages, then shop/category/favorites.
5. Polish UX, limits, errors, tests, docs.
