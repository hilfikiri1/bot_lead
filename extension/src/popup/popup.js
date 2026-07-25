let products = [];
let pageUrl = "";
let currentJobId = null;

const statusEl = document.getElementById("status");
const productListEl = document.getElementById("productList");
const generateBtn = document.getElementById("generateBtn");
const downloadLink = document.getElementById("downloadLink");

function setStatus(text) {
  statusEl.textContent = text;
}

function getSelectedProducts() {
  const checkboxes = productListEl.querySelectorAll("input[type='checkbox']");
  return Array.from(checkboxes)
    .filter((box) => box.checked)
    .map((box) => products[Number(box.dataset.index)]);
}

function renderProducts() {
  productListEl.innerHTML = "";
  products.forEach((product, index) => {
    const item = document.createElement("div");
    item.className = "product-item";
    item.innerHTML = `
      <input type="checkbox" data-index="${index}" checked>
      <img src="${product.thumbnail_url || ""}" alt="">
      <div>
        <p class="product-title">${escapeHtml(product.title_zh)}</p>
        <div class="product-meta">
          <div>${escapeHtml(product.price_raw_text || "Цена не указана")}</div>
          <div>${escapeHtml(product.supplier_name_zh || "Фабрика не указана")}</div>
        </div>
      </div>
    `;
    productListEl.appendChild(item);
  });
  generateBtn.disabled = products.length === 0;
}

function escapeHtml(value) {
  return String(value || "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

async function refreshProducts() {
  setStatus("Сканирую страницу…");
  generateBtn.disabled = true;
  downloadLink.classList.add("hidden");

  const response = await chrome.runtime.sendMessage({ type: "PARSE_ACTIVE_TAB" });
  if (!response?.ok) {
    setStatus(response?.error || "Ошибка парсинга");
    products = [];
    renderProducts();
    return;
  }

  products = response.products || [];
  pageUrl = response.pageUrl || "";
  setStatus(`Найдено товаров: ${products.length}`);
  renderProducts();
}

async function generatePdf() {
  const selected = getSelectedProducts();
  if (selected.length === 0) {
    setStatus("Выберите хотя бы один товар");
    return;
  }

  generateBtn.disabled = true;
  setStatus(`Отправляю ${selected.length} товаров на сервер…`);

  const submitResponse = await chrome.runtime.sendMessage({
    type: "SUBMIT_BATCH",
    products: selected,
    pageUrl,
  });

  if (!submitResponse?.ok) {
    setStatus(submitResponse?.error || "Ошибка отправки");
    generateBtn.disabled = false;
    return;
  }

  currentJobId = submitResponse.created.job_id;
  setStatus("Формирую PDF…");

  const pollResponse = await chrome.runtime.sendMessage({
    type: "POLL_JOB",
    jobId: currentJobId,
  });

  if (!pollResponse?.ok) {
    setStatus(pollResponse?.error || "Ошибка ожидания");
    generateBtn.disabled = false;
    return;
  }

  const job = pollResponse.status;
  if (job.status === "failed") {
    setStatus(job.error_message || "Не удалось сформировать PDF");
    generateBtn.disabled = false;
    return;
  }

  setStatus("Каталог готов");
  downloadLink.classList.remove("hidden");
  downloadLink.textContent = "Скачать PDF";
  downloadLink.onclick = async (event) => {
    event.preventDefault();
    await chrome.runtime.sendMessage({
      type: "DOWNLOAD_PDF",
      jobId: currentJobId,
      filename: "babrik_1688_catalog.pdf",
    });
  };
  generateBtn.disabled = false;
}

document.getElementById("refreshBtn").addEventListener("click", refreshProducts);
document.getElementById("selectAllBtn").addEventListener("click", () => {
  productListEl.querySelectorAll("input[type='checkbox']").forEach((box) => {
    box.checked = true;
  });
});
document.getElementById("clearAllBtn").addEventListener("click", () => {
  productListEl.querySelectorAll("input[type='checkbox']").forEach((box) => {
    box.checked = false;
  });
});
generateBtn.addEventListener("click", generatePdf);

refreshProducts();
