# Self-Prompt: 1688 Product Scraper → AI-Powered PDF Chrome Extension

## Task Definition

Build a Google Chrome Extension (Manifest V3) that:

1. **Parses** product listings from the Chinese B2B marketplace 1688.com, extracting for each product:
   - Product name / title
   - Primary product image (URL)
   - Price (range or unit price)
   - Factory / supplier name
   - Product URL

2. **Displays** the parsed list in a clean popup UI, allowing the user to:
   - Review and deselect individual products
   - Configure their store branding (store name, logo URL, contact info)
   - Enter their OpenAI API key (stored in `chrome.storage.sync`)

3. **Sends** the selected product data to the OpenAI Chat Completions API, which returns a structured JSON description and layout plan for the PDF (translated to English if necessary, formatted for a product catalog).

4. **Generates** a professionally formatted PDF catalog using jsPDF (loaded as a bundled library), embedding:
   - A custom header with the store name and logo
   - A product grid (2 columns) with photo, name, price, and supplier for each item
   - Page numbers and footer with store contact information

5. **Downloads** the PDF automatically via `chrome.downloads.download` or a data-URI anchor click.

---

## Architecture

```
chrome-extension/
├── manifest.json            # MV3 manifest
├── PROMPT.md                # This file
├── icons/
│   ├── icon16.png
│   ├── icon48.png
│   └── icon128.png
├── lib/
│   └── jspdf.umd.min.js     # Bundled jsPDF (no CDN in extensions)
└── src/
    ├── content.js           # Injected into 1688.com — scrapes DOM
    ├── background.js        # Service worker — OpenAI API calls
    ├── popup.html           # Extension popup shell
    ├── popup.js             # Popup logic (render list, trigger PDF)
    ├── popup.css            # Popup styles
    └── pdf_generator.js     # jsPDF catalog builder
```

### Data Flow

```
User clicks extension icon
        │
        ▼
popup.js ──sendMessage──► content.js (active 1688 tab)
                                │  scrapeProducts()
                                │  returns Product[]
        ◄──────────────────────┘
        │
popup.js renders product list (checkbox per item)
        │
User reviews, configures store info, clicks "Generate PDF"
        │
        ▼
popup.js ──sendMessage──► background.js
                                │  POST /v1/chat/completions (OpenAI)
                                │  model: gpt-4o
                                │  prompt: format products as catalog JSON
                                │  returns EnrichedProduct[]
        ◄──────────────────────┘
        │
popup.js calls pdf_generator.js
        │  builds PDF with jsPDF
        │  downloads file
        ▼
      Done
```

---

## Key Implementation Details

### 1. Content Script — `content.js`

Target selectors on 1688.com search/listing pages:

| Data Point   | CSS Selector (illustrative — may need updating per DOM) |
|---|---|
| Product card | `.card-container`, `.item-warper`, `[class*="card"]` |
| Title        | `.title`, `[class*="title"]` inside card |
| Price        | `.price`, `[class*="price"]` |
| Image        | `img[src]` first `<img>` inside card |
| Factory      | `.factory-name`, `.company-name`, `[class*="company"]` |

The scraper must handle pagination (current page only) and deduplicate by product URL. Returns an array of `Product` objects.

```ts
interface Product {
  id: string;           // hash of URL
  title: string;
  price: string;        // raw string e.g. "¥12.50-18.00"
  imageUrl: string;
  factoryName: string;
  productUrl: string;
}
```

### 2. Background Service Worker — `background.js`

Receives `{ action: "enrich", products, storeConfig, apiKey }` via `chrome.runtime.onMessage`.

Sends to OpenAI:
- System prompt: "You are a product catalog formatter. Given raw product data scraped from 1688.com, return a JSON array of enriched products with English titles, cleaned prices in USD equivalent, and a one-sentence product description. Preserve all original fields."
- User message: JSON.stringify(products)

Returns `EnrichedProduct[]` back to popup.

### 3. PDF Generator — `pdf_generator.js`

Uses `jsPDF` in UMD mode (pre-bundled, no network calls).

Layout:
- **Header** (every page): store logo (if URL provided, drawn as image), store name in large font, thin rule line
- **Body**: 2-column grid, each cell = image thumbnail + title + price + factory name
- **Footer** (every page): store contact, page N of M

PDF dimensions: A4 (210 × 297 mm).

---

## Constraints

- No external CDN requests at runtime (all libraries bundled in `lib/`).
- API key stored only in `chrome.storage.sync`, never hardcoded.
- Graceful fallback if AI enrichment fails: use raw scraped data for PDF.
- Content script runs only on `*://*.1688.com/*` URLs.
- The extension must request minimal permissions: `activeTab`, `storage`, `downloads`.

---

## Deliverables

All files listed in the Architecture section, fully implemented, with a `README.md` explaining installation, configuration, and usage.
