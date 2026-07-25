/**
 * popup.js — Main popup controller
 *
 * Responsibilities:
 *  - Tab navigation
 *  - Sending "scrapeProducts" to content.js via chrome.tabs.sendMessage
 *  - Rendering the product list with checkboxes
 *  - Persisting / loading settings via chrome.storage.sync
 *  - Orchestrating the AI → PDF pipeline
 */

import { buildCatalogPDF } from "./pdf_generator.js";

// ============================================================
// DOM references
// ============================================================
const badgeCount      = document.getElementById("badge-count");
const btnScan         = document.getElementById("btn-scan");
const btnGenerate     = document.getElementById("btn-generate");
const selectionInfo   = document.getElementById("selection-info");

const viewEmpty       = document.getElementById("view-empty");
const viewLoading     = document.getElementById("view-loading");
const viewList        = document.getElementById("view-list");
const viewError       = document.getElementById("view-error");
const errorDesc       = document.getElementById("error-desc");
const loadingLabel    = document.getElementById("loading-label");

const progressWrap    = document.getElementById("progress-wrap");
const progressLabel   = document.getElementById("progress-label");
const progressFill    = document.getElementById("progress-fill");

const toast           = document.getElementById("toast");

// Settings
const storeNameInput  = document.getElementById("store-name");
const storeLogoInput  = document.getElementById("store-logo");
const storeContactInput = document.getElementById("store-contact");
const storeCurrencyInput = document.getElementById("store-currency");
const openaiKeyInput  = document.getElementById("openai-key");
const openaiModelSelect = document.getElementById("openai-model");
const skipAiCheckbox  = document.getElementById("skip-ai");
const btnSaveSettings = document.getElementById("btn-save-settings");
const saveStatus      = document.getElementById("save-status");

// ============================================================
// App state
// ============================================================
let allProducts  = [];   // Product[] scraped from page
let selectedIds  = new Set();

// ============================================================
// Utilities
// ============================================================

function showToast(message, type = "info", durationMs = 2800) {
  toast.textContent = message;
  toast.className = `toast show ${type}`;
  clearTimeout(toast._timer);
  toast._timer = setTimeout(() => {
    toast.className = "toast";
  }, durationMs);
}

function setProgress(pct, label) {
  progressWrap.style.display = "block";
  progressLabel.textContent  = label;
  progressFill.style.width   = `${Math.min(100, pct)}%`;
}

function hideProgress() {
  progressWrap.style.display = "none";
  progressFill.style.width   = "0%";
}

function showView(name) {
  viewEmpty.style.display   = "none";
  viewLoading.style.display = "none";
  viewList.style.display    = "none";
  viewError.style.display   = "none";

  if (name === "empty")   viewEmpty.style.display   = "flex";
  if (name === "loading") viewLoading.style.display = "flex";
  if (name === "list")    viewList.style.display    = "block";
  if (name === "error")   viewError.style.display   = "flex";
}

function updateSelectionUI() {
  const count = selectedIds.size;
  selectionInfo.textContent = `${count} selected`;
  btnGenerate.disabled      = count === 0;
}

function updateBadge() {
  badgeCount.textContent = `${allProducts.length} items`;
}

// ============================================================
// Tab navigation
// ============================================================
document.querySelectorAll(".tab-btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tab-btn").forEach((b) => b.classList.remove("active"));
    document.querySelectorAll(".panel").forEach((p) => p.classList.remove("active"));

    btn.classList.add("active");
    const target = document.getElementById(`panel-${btn.dataset.tab}`);
    if (target) target.classList.add("active");
  });
});

// ============================================================
// Render product list
// ============================================================
function renderProducts(products) {
  viewList.innerHTML = "";

  if (products.length === 0) {
    showView("empty");
    return;
  }

  products.forEach((p) => {
    const item = document.createElement("div");
    item.className = "product-item" + (selectedIds.has(p.id) ? " selected" : "");
    item.dataset.id = p.id;

    const cb = document.createElement("input");
    cb.type      = "checkbox";
    cb.className = "product-checkbox";
    cb.checked   = selectedIds.has(p.id);

    // Thumbnail
    let thumbEl;
    if (p.imageUrl) {
      thumbEl = document.createElement("img");
      thumbEl.src       = p.imageUrl;
      thumbEl.className = "product-thumb";
      thumbEl.alt       = p.title;
      thumbEl.onerror   = () => {
        const ph = document.createElement("div");
        ph.className = "product-thumb-placeholder";
        ph.textContent = "🖼";
        thumbEl.replaceWith(ph);
      };
    } else {
      thumbEl = document.createElement("div");
      thumbEl.className = "product-thumb-placeholder";
      thumbEl.textContent = "🖼";
    }

    const info = document.createElement("div");
    info.className = "product-info";

    const title = document.createElement("div");
    title.className   = "product-title";
    title.textContent = p.title || "(no title)";

    const meta = document.createElement("div");
    meta.className = "product-meta";

    if (p.price) {
      const price = document.createElement("span");
      price.className   = "product-price";
      price.textContent = p.price;
      meta.appendChild(price);
    }

    if (p.factoryName) {
      const factory = document.createElement("span");
      factory.className   = "product-factory";
      factory.textContent = "🏭 " + p.factoryName;
      meta.appendChild(factory);
    }

    info.appendChild(title);
    info.appendChild(meta);

    item.appendChild(cb);
    item.appendChild(thumbEl);
    item.appendChild(info);

    // Toggle selection on click
    item.addEventListener("click", (e) => {
      if (e.target === cb) return; // handled below
      cb.checked = !cb.checked;
      toggleSelection(p.id, cb.checked);
      item.classList.toggle("selected", cb.checked);
    });

    cb.addEventListener("change", () => {
      toggleSelection(p.id, cb.checked);
      item.classList.toggle("selected", cb.checked);
    });

    viewList.appendChild(item);
  });

  showView("list");
}

function toggleSelection(id, isSelected) {
  if (isSelected) selectedIds.add(id);
  else            selectedIds.delete(id);
  updateSelectionUI();
}

// ============================================================
// Scan page
// ============================================================
btnScan.addEventListener("click", async () => {
  showView("loading");
  loadingLabel.textContent = "Scanning page…";
  btnScan.disabled = true;

  try {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });

    if (!tab || !tab.url || !tab.url.includes("1688.com")) {
      showView("error");
      errorDesc.textContent = "Please navigate to a 1688.com product listing page first.";
      return;
    }

    // Ensure content script is injected (fallback for pages loaded before extension)
    try {
      await chrome.scripting.executeScript({
        target: { tabId: tab.id },
        files: ["src/content.js"],
      });
    } catch (_) {
      // Already injected — ignore
    }

    const response = await new Promise((resolve, reject) => {
      chrome.tabs.sendMessage(tab.id, { action: "scrapeProducts" }, (res) => {
        if (chrome.runtime.lastError) {
          reject(new Error(chrome.runtime.lastError.message));
        } else {
          resolve(res);
        }
      });
    });

    if (!response || !response.success) {
      throw new Error(response?.error || "Content script returned no data.");
    }

    allProducts = response.products || [];
    // Default: select all
    selectedIds = new Set(allProducts.map((p) => p.id));

    updateBadge();
    updateSelectionUI();
    renderProducts(allProducts);

    if (allProducts.length === 0) {
      showToast("No products found on this page.", "info");
    } else {
      showToast(`Found ${allProducts.length} product(s)!`, "success");
    }
  } catch (err) {
    showView("error");
    errorDesc.textContent = err.message;
    showToast("Scan failed: " + err.message, "error");
  } finally {
    btnScan.disabled = false;
  }
});

// ============================================================
// Generate PDF
// ============================================================
btnGenerate.addEventListener("click", async () => {
  const selected = allProducts.filter((p) => selectedIds.has(p.id));
  if (selected.length === 0) {
    showToast("Select at least one product.", "info");
    return;
  }

  const config = await loadSettings();
  btnGenerate.disabled = true;

  try {
    let products = selected;

    if (!config.skipAi && config.apiKey) {
      setProgress(10, "Sending to AI for enrichment…");
      try {
        products = await enrichWithAI(selected, config);
        setProgress(60, "AI enrichment complete.");
      } catch (aiErr) {
        showToast("AI enrichment failed — using raw data. " + aiErr.message, "info");
        setProgress(60, "Using raw product data…");
      }
    } else {
      setProgress(40, "Skipping AI enrichment…");
    }

    setProgress(75, "Building PDF…");

    const pdfBlob = await buildCatalogPDF(products, config);

    setProgress(95, "Downloading…");

    const url  = URL.createObjectURL(pdfBlob);
    const link = document.createElement("a");
    link.href     = url;
    link.download = `catalog-${Date.now()}.pdf`;
    link.click();
    URL.revokeObjectURL(url);

    setProgress(100, "Done!");
    setTimeout(hideProgress, 1500);

    showToast(`PDF generated with ${products.length} products!`, "success");
  } catch (err) {
    hideProgress();
    showToast("PDF generation failed: " + err.message, "error");
    console.error(err);
  } finally {
    btnGenerate.disabled = selectedIds.size === 0;
  }
});

// ============================================================
// AI Enrichment — calls background.js via chrome.runtime.sendMessage
// ============================================================
async function enrichWithAI(products, config) {
  return new Promise((resolve, reject) => {
    chrome.runtime.sendMessage(
      {
        action: "enrichProducts",
        products,
        apiKey: config.apiKey,
        model: config.model,
        currency: config.currency,
      },
      (response) => {
        if (chrome.runtime.lastError) {
          return reject(new Error(chrome.runtime.lastError.message));
        }
        if (!response || !response.success) {
          return reject(new Error(response?.error || "AI enrichment failed"));
        }
        resolve(response.products);
      }
    );
  });
}

// ============================================================
// Settings — persist to chrome.storage.sync
// ============================================================
async function loadSettings() {
  return new Promise((resolve) => {
    chrome.storage.sync.get(
      ["storeName", "storeLogo", "storeContact", "storeCurrency",
       "openaiKey", "openaiModel", "skipAi"],
      (data) => {
        storeNameInput.value      = data.storeName    || "";
        storeLogoInput.value      = data.storeLogo    || "";
        storeContactInput.value   = data.storeContact || "";
        storeCurrencyInput.value  = data.storeCurrency || "USD";
        openaiKeyInput.value      = data.openaiKey    || "";
        openaiModelSelect.value   = data.openaiModel  || "gpt-4o";
        skipAiCheckbox.checked    = !!data.skipAi;

        resolve({
          storeName: data.storeName    || "My Store",
          logoUrl:   data.storeLogo    || "",
          contact:   data.storeContact || "",
          currency:  data.storeCurrency || "USD",
          apiKey:    data.openaiKey    || "",
          model:     data.openaiModel  || "gpt-4o",
          skipAi:    !!data.skipAi,
        });
      }
    );
  });
}

btnSaveSettings.addEventListener("click", () => {
  chrome.storage.sync.set({
    storeName:     storeNameInput.value.trim(),
    storeLogo:     storeLogoInput.value.trim(),
    storeContact:  storeContactInput.value.trim(),
    storeCurrency: storeCurrencyInput.value.trim() || "USD",
    openaiKey:     openaiKeyInput.value.trim(),
    openaiModel:   openaiModelSelect.value,
    skipAi:        skipAiCheckbox.checked,
  }, () => {
    saveStatus.style.display = "inline";
    setTimeout(() => { saveStatus.style.display = "none"; }, 2000);
    showToast("Settings saved!", "success");
  });
});

// ============================================================
// Init
// ============================================================
(async () => {
  await loadSettings();
})();
