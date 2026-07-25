# 1688 Product Catalog to PDF — Chrome Extension

A Google Chrome extension that:
1. **Scrapes** product listings from [1688.com](https://www.1688.com) — extracting product photos, prices, and factory names.
2. **Enriches** the data using OpenAI (translates Chinese titles to English, normalises prices).
3. **Generates** a branded, professional PDF product catalog ready for sharing with customers.

---

## Features

- One-click scraping of product cards from any 1688.com search/listing/category page
- AI-powered title translation and price normalisation (OpenAI GPT-4o)
- Fully offline PDF generation using [jsPDF](https://github.com/parallax/jsPDF) (no server needed)
- Customisable store branding: name, logo, contact information
- 2-column A4 PDF layout with product thumbnails, prices, and factory names
- Works without AI (skip-AI mode) — generates PDF from raw scraped data

---

## Installation

> The extension is not published to the Chrome Web Store. Install it in Developer Mode.

1. **Download / clone** this repository.
2. Open Chrome and navigate to `chrome://extensions/`.
3. Enable **Developer mode** (toggle in the top right).
4. Click **"Load unpacked"** and select the `chrome-extension/` folder.
5. The extension icon (📦) will appear in your toolbar.

---

## Usage

### 1 — Configure Settings

Click the extension icon → **⚙️ Settings** tab.

| Field | Description |
|---|---|
| **Store Name** | Appears as the catalog title in the PDF header |
| **Logo URL** | Optional: a public image URL for your store logo |
| **Contact / Footer** | Email, phone, or any text shown in PDF footers |
| **Currency Display** | Target currency for AI price conversion (default: USD) |
| **OpenAI API Key** | Your `sk-…` key — stored locally, never shared |
| **AI Model** | `gpt-4o` (best), `gpt-4o-mini` (faster/cheaper), or `gpt-4-turbo` |
| **Skip AI enrichment** | Generate PDF directly from raw scraped data |

Click **💾 Save Settings**.

### 2 — Scrape Products

1. Navigate to any 1688.com page with product cards, e.g.:
   - `https://s.1688.com/selloffer/offerlist.htm?keywords=…` (search results)
   - A shop's product listing page
   - A category browse page
2. Click the extension icon.
3. Click **🔍 Scan Page**.
4. Products will appear as a scrollable list with thumbnails, prices, and factory names.

### 3 — Select & Generate

1. Check/uncheck products to include in the catalog.
2. Click **✨ Generate PDF**.
3. The extension will:
   - Call OpenAI to translate and enrich product data (unless Skip AI is enabled).
   - Build an A4 PDF with your store branding.
   - Trigger an automatic download (`catalog-<timestamp>.pdf`).

---

## PDF Layout

```
┌────────────────────────────────┐
│  [Logo]  STORE NAME            │  ← Header (every page)
│          Product Catalog · Date│
├────────────────────────────────┤
│  ┌──────────┐  ┌──────────┐   │
│  │  [Image] │  │  [Image] │   │
│  │ Title    │  │ Title    │   │
│  │ $Price   │  │ $Price   │   │
│  │ 🏭 Factory│  │ 🏭 Factory│   │
│  └──────────┘  └──────────┘   │
│  ...more rows...               │
├────────────────────────────────┤
│  contact@store.com    Page 1/3 │  ← Footer (every page)
└────────────────────────────────┘
```

---

## Project Structure

```
chrome-extension/
├── manifest.json             MV3 manifest
├── PROMPT.md                 Development specification
├── README.md                 This file
├── icons/
│   ├── icon16.png
│   ├── icon48.png
│   └── icon128.png
├── lib/
│   └── jspdf.umd.min.js     Bundled jsPDF v2.5.1 (offline, no CDN)
└── src/
    ├── content.js            DOM scraper injected into 1688.com
    ├── background.js         Service worker — OpenAI API calls
    ├── popup.html            Extension popup UI
    ├── popup.js              Popup controller (tabs, scan, generate)
    ├── popup.css             Popup styles
    └── pdf_generator.js      jsPDF-based catalog builder
```

---

## Permissions

| Permission | Reason |
|---|---|
| `activeTab` | Read the current 1688.com tab to inject the content script |
| `scripting` | Inject content script on demand |
| `storage` | Persist store settings and API key locally |
| `downloads` | Save the generated PDF file |

---

## Privacy

- Your OpenAI API key is stored **only** in Chrome's local sync storage (`chrome.storage.sync`). It is never logged or sent anywhere except OpenAI's official API endpoint.
- Product data is sent to OpenAI only when you click "Generate PDF" with AI enrichment enabled.
- No analytics, telemetry, or third-party scripts are included.

---

## Troubleshooting

| Problem | Solution |
|---|---|
| "No products found on this page" | Make sure you are on a 1688 **search/listing** page (not a single product detail page). Scroll down to trigger lazy loading, then scan again. |
| Images not showing in PDF | 1688 images may have CORS restrictions. The PDF will show a grey placeholder — this is expected. |
| AI enrichment fails | Check that your OpenAI API key is valid and has available quota. Enable **Skip AI** as a fallback. |
| Extension not working on 1688 | 1688.com occasionally changes its page structure. Update `CARD_SELECTORS` and `FIELD_SELECTORS` in `src/content.js` to match the new DOM. |

---

## Development

No build step required. All code is plain ES2020 JavaScript.

To update selectors after a 1688 DOM change:
1. Open 1688.com in Chrome DevTools.
2. Inspect a product card to find the new class names.
3. Update `CARD_SELECTORS` and `FIELD_SELECTORS` in `src/content.js`.
4. Reload the extension at `chrome://extensions/`.

---

## License

MIT
