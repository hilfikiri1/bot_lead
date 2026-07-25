const DEFAULT_POLL_INTERVAL_MS = 2000;
const DEFAULT_MAX_POLLS = 120;

async function getSettings() {
  const stored = await chrome.storage.sync.get({
    apiBaseUrl: "http://localhost:8000",
    apiKey: "",
    telegramUserId: "",
    maxProducts: 20,
  });
  return stored;
}

async function apiRequest(path, options = {}) {
  const settings = await getSettings();
  const baseUrl = settings.apiBaseUrl.replace(/\/$/, "");
  const headers = {
    "Content-Type": "application/json",
    ...(options.headers || {}),
  };
  if (settings.apiKey) {
    headers.Authorization = `Bearer ${settings.apiKey}`;
  }

  const response = await fetch(`${baseUrl}${path}`, {
    ...options,
    headers,
  });

  const text = await response.text();
  let data = null;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    data = { detail: text };
  }

  if (!response.ok) {
    const detail = data?.detail || data?.error || response.statusText;
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return data;
}

async function parseActiveTab() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab?.id) throw new Error("Активная вкладка не найдена");
  if (!tab.url || !tab.url.includes("1688.com")) {
    throw new Error("Откройте страницу 1688.com");
  }

  const response = await chrome.tabs.sendMessage(tab.id, { type: "PARSE_PRODUCTS" });
  if (!response?.ok) {
    throw new Error(response?.error || "Не удалось распарсить страницу");
  }
  return response;
}

async function submitBatch(products, pageUrl) {
  const settings = await getSettings();
  const payload = {
    products,
    options: {
      locale: "ru",
      source_page_url: pageUrl,
    },
  };

  if (settings.telegramUserId) {
    const userId = Number(settings.telegramUserId);
    if (!Number.isNaN(userId)) {
      payload.options.telegram_user_id = userId;
      payload.options.telegram_chat_id = userId;
    }
  }

  return apiRequest("/api/catalog/batch", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

async function pollJob(jobId) {
  for (let attempt = 0; attempt < DEFAULT_MAX_POLLS; attempt += 1) {
    const status = await apiRequest(`/api/catalog/jobs/${jobId}`);
    if (status.status === "completed" || status.status === "failed") {
      return status;
    }
    await new Promise((resolve) => setTimeout(resolve, DEFAULT_POLL_INTERVAL_MS));
  }
  throw new Error("Превышено время ожидания формирования PDF");
}

async function downloadPdf(jobId, filename) {
  const settings = await getSettings();
  const baseUrl = settings.apiBaseUrl.replace(/\/$/, "");
  const response = await fetch(`${baseUrl}/api/catalog/jobs/${jobId}/download`, {
    headers: settings.apiKey ? { Authorization: `Bearer ${settings.apiKey}` } : {},
  });
  if (!response.ok) {
    throw new Error("Не удалось скачать PDF");
  }
  const blob = await response.blob();
  const url = URL.createObjectURL(blob);
  await chrome.downloads.download({
    url,
    filename: filename || `babrik_catalog_${jobId}.pdf`,
    saveAs: true,
  });
  setTimeout(() => URL.revokeObjectURL(url), 60_000);
}

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  (async () => {
    try {
      if (message?.type === "PARSE_ACTIVE_TAB") {
        sendResponse({ ok: true, ...(await parseActiveTab()) });
        return;
      }
      if (message?.type === "SUBMIT_BATCH") {
        const created = await submitBatch(message.products, message.pageUrl);
        sendResponse({ ok: true, created });
        return;
      }
      if (message?.type === "POLL_JOB") {
        sendResponse({ ok: true, status: await pollJob(message.jobId) });
        return;
      }
      if (message?.type === "DOWNLOAD_PDF") {
        await downloadPdf(message.jobId, message.filename);
        sendResponse({ ok: true });
        return;
      }
      sendResponse({ ok: false, error: "Unknown message type" });
    } catch (error) {
      sendResponse({ ok: false, error: error.message || String(error) });
    }
  })();
  return true;
});
