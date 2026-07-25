/**
 * pdf_generator.js — Builds a branded product catalog PDF using jsPDF.
 *
 * Layout: A4 portrait, 2-column product grid.
 * Each cell: product thumbnail + name + price + factory name.
 * Header (every page): store name [+ logo] + divider rule.
 * Footer (every page): contact text + page N / M.
 */

// jsPDF is loaded as a UMD bundle at runtime via importScripts-style import.
// In an ES module context inside a Chrome extension popup we use a dynamic import.
let _jsPDF = null;

async function getJsPDF() {
  if (_jsPDF) return _jsPDF;

  // Import the UMD bundle — it attaches jsPDF to the global scope
  const src = chrome.runtime.getURL("lib/jspdf.umd.min.js");

  // We must use a classic script tag because UMD bundles assign to `window`
  await new Promise((resolve, reject) => {
    const existing = document.querySelector(`script[src="${src}"]`);
    if (existing) { resolve(); return; }
    const s = document.createElement("script");
    s.src   = src;
    s.onload  = resolve;
    s.onerror = () => reject(new Error("Failed to load jsPDF library."));
    document.head.appendChild(s);
  });

  // The UMD build exposes window.jspdf.jsPDF
  const ctor = window?.jspdf?.jsPDF;
  if (!ctor) throw new Error("jsPDF constructor not found after loading.");
  _jsPDF = ctor;
  return _jsPDF;
}

// ============================================================
// Constants / layout
// ============================================================
const PAGE_W   = 210;   // A4 width  (mm)
const PAGE_H   = 297;   // A4 height (mm)
const MARGIN   = 12;    // page margin (mm)
const HEADER_H = 28;    // header block height (mm)
const FOOTER_H = 10;    // footer block height (mm)
const GAP      = 6;     // gap between cells (mm)
const COLS     = 2;     // columns per row

const CELL_W   = (PAGE_W - MARGIN * 2 - GAP * (COLS - 1)) / COLS;
const THUMB_H  = 48;    // thumbnail height (mm)
const TEXT_ROWS_H = 22; // height reserved for text under image
const CELL_H   = THUMB_H + TEXT_ROWS_H;

const BODY_TOP  = MARGIN + HEADER_H + 4;
const BODY_BOT  = PAGE_H - MARGIN - FOOTER_H - 4;

// Brand colours
const COLOR_PRIMARY  = [232, 52, 10];    // #e8340a
const COLOR_TEXT     = [26, 26, 46];     // #1a1a2e
const COLOR_MUTED    = [108, 117, 125];  // #6c757d
const COLOR_BORDER   = [226, 228, 232];  // #e2e4e8

// ============================================================
// Entry point
// ============================================================

/**
 * Build and return a PDF Blob.
 *
 * @param {Object[]} products  — enriched product array
 * @param {Object}   config    — { storeName, logoUrl, contact, currency }
 * @returns {Promise<Blob>}
 */
export async function buildCatalogPDF(products, config) {
  const JsPDF = await getJsPDF();

  const doc = new JsPDF({ unit: "mm", format: "a4", orientation: "portrait" });
  const totalPages = Math.ceil(products.length / (COLS * rowsPerPage()));

  // We need 2 passes: first to know total pages, then to draw.
  // jsPDF doesn't support auto total-page yet in a simple way,
  // so we count pages ahead of time.
  const itemsPerPage = COLS * rowsPerPage();
  const total = Math.max(1, Math.ceil(products.length / itemsPerPage));

  for (let pageIdx = 0; pageIdx < total; pageIdx++) {
    if (pageIdx > 0) doc.addPage();

    const pageProducts = products.slice(
      pageIdx * itemsPerPage,
      (pageIdx + 1) * itemsPerPage
    );

    await drawHeader(doc, config);
    drawFooter(doc, config, pageIdx + 1, total);
    await drawProductGrid(doc, pageProducts);
  }

  return doc.output("blob");
}

// ============================================================
// How many product rows fit on one page?
// ============================================================
function rowsPerPage() {
  const availableH = BODY_BOT - BODY_TOP;
  return Math.max(1, Math.floor((availableH + GAP) / (CELL_H + GAP)));
}

// ============================================================
// Draw header
// ============================================================
async function drawHeader(doc, config) {
  const { storeName = "Product Catalog", logoUrl = "" } = config;

  let logoDrawn = false;
  if (logoUrl) {
    try {
      const imgData = await loadImageAsBase64(logoUrl);
      // Draw logo on left, constrained to HEADER_H - 4 mm tall
      const maxLogoH = HEADER_H - 6;
      const maxLogoW = 40;
      doc.addImage(imgData, "JPEG", MARGIN, MARGIN + 2, maxLogoW, maxLogoH, "", "FAST");
      logoDrawn = true;
    } catch (_) {
      // If logo fails to load, fall through to text-only header
    }
  }

  const textX = logoDrawn ? MARGIN + 44 : MARGIN;

  // Store name
  doc.setFontSize(18);
  doc.setFont("helvetica", "bold");
  doc.setTextColor(...COLOR_PRIMARY);
  doc.text(storeName, textX, MARGIN + 10);

  // Subtitle
  doc.setFontSize(9);
  doc.setFont("helvetica", "normal");
  doc.setTextColor(...COLOR_MUTED);
  const now = new Date().toLocaleDateString("en-US", {
    year: "numeric", month: "long", day: "numeric",
  });
  doc.text(`Product Catalog  ·  Generated ${now}`, textX, MARGIN + 16);

  // Divider rule
  doc.setDrawColor(...COLOR_BORDER);
  doc.setLineWidth(0.4);
  doc.line(MARGIN, MARGIN + HEADER_H, PAGE_W - MARGIN, MARGIN + HEADER_H);
}

// ============================================================
// Draw footer
// ============================================================
function drawFooter(doc, config, pageNum, totalPages) {
  const { contact = "" } = config;
  const y = PAGE_H - MARGIN - FOOTER_H + 4;

  // Divider rule
  doc.setDrawColor(...COLOR_BORDER);
  doc.setLineWidth(0.3);
  doc.line(MARGIN, y - 3, PAGE_W - MARGIN, y - 3);

  doc.setFontSize(8);
  doc.setFont("helvetica", "normal");

  if (contact) {
    doc.setTextColor(...COLOR_MUTED);
    doc.text(contact, MARGIN, y);
  }

  // Page number — right-aligned
  doc.setTextColor(...COLOR_MUTED);
  const pageStr = `Page ${pageNum} / ${totalPages}`;
  const pageStrW = doc.getTextWidth(pageStr);
  doc.text(pageStr, PAGE_W - MARGIN - pageStrW, y);
}

// ============================================================
// Draw product grid
// ============================================================
async function drawProductGrid(doc, products) {
  let col = 0;
  let row = 0;

  for (const product of products) {
    const x = MARGIN + col * (CELL_W + GAP);
    const y = BODY_TOP + row * (CELL_H + GAP);

    await drawProductCell(doc, product, x, y);

    col++;
    if (col >= COLS) {
      col = 0;
      row++;
    }
  }
}

// ============================================================
// Draw a single product cell
// ============================================================
async function drawProductCell(doc, product, x, y) {
  // Cell border
  doc.setDrawColor(...COLOR_BORDER);
  doc.setLineWidth(0.25);
  doc.setFillColor(255, 255, 255);
  doc.roundedRect(x, y, CELL_W, CELL_H, 2, 2, "FD");

  const innerX = x + 3;
  const innerW = CELL_W - 6;

  // Thumbnail image
  if (product.imageUrl) {
    try {
      const imgData = await loadImageAsBase64(product.imageUrl);
      doc.addImage(
        imgData, "JPEG",
        innerX, y + 3,
        innerW, THUMB_H,
        "", "FAST"
      );
    } catch (_) {
      // Draw placeholder box if image fails
      doc.setFillColor(...COLOR_BORDER);
      doc.rect(innerX, y + 3, innerW, THUMB_H, "F");
      doc.setFontSize(7);
      doc.setTextColor(...COLOR_MUTED);
      doc.text("No image", innerX + innerW / 2, y + 3 + THUMB_H / 2, { align: "center" });
    }
  } else {
    doc.setFillColor(...COLOR_BORDER);
    doc.rect(innerX, y + 3, innerW, THUMB_H, "F");
  }

  const textY = y + 3 + THUMB_H + 4;

  // Product title — up to 2 lines
  const title = product.title || "(No title)";
  doc.setFontSize(7.5);
  doc.setFont("helvetica", "bold");
  doc.setTextColor(...COLOR_TEXT);

  const titleLines = doc.splitTextToSize(title, innerW);
  const displayLines = titleLines.slice(0, 2);
  if (titleLines.length > 2) {
    displayLines[1] = displayLines[1].slice(0, -1) + "…";
  }
  doc.text(displayLines, innerX, textY);

  // Price
  const priceY = textY + displayLines.length * 4 + 1;
  if (product.price) {
    doc.setFontSize(8);
    doc.setFont("helvetica", "bold");
    doc.setTextColor(...COLOR_PRIMARY);
    doc.text(product.price, innerX, priceY);
  }

  // Factory name
  const factoryY = priceY + 4;
  if (product.factoryName) {
    doc.setFontSize(6.5);
    doc.setFont("helvetica", "normal");
    doc.setTextColor(...COLOR_MUTED);
    const factoryText = doc.splitTextToSize("🏭 " + product.factoryName, innerW);
    doc.text(factoryText[0], innerX, factoryY);
  }

  // AI-generated description (if available)
  if (product.description) {
    const descY = factoryY + 4;
    doc.setFontSize(6);
    doc.setFont("helvetica", "italic");
    doc.setTextColor(...COLOR_MUTED);
    const descLines = doc.splitTextToSize(product.description, innerW);
    doc.text(descLines[0], innerX, descY);
  }
}

// ============================================================
// Image loading utility — fetches an image and returns base64
// ============================================================
const imageCache = new Map();

async function loadImageAsBase64(url) {
  if (imageCache.has(url)) return imageCache.get(url);

  const response = await fetch(url, { mode: "no-cors" });
  const blob     = await response.blob();

  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onloadend = () => {
      const dataUrl = reader.result;
      imageCache.set(url, dataUrl);
      resolve(dataUrl);
    };
    reader.onerror = reject;
    reader.readAsDataURL(blob);
  });
}
