chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message?.type === "PARSE_PRODUCTS") {
    try {
      const products = parseProductsOnPage();
      sendResponse({ ok: true, products, pageUrl: window.location.href });
    } catch (error) {
      sendResponse({ ok: false, error: error.message || String(error) });
    }
    return true;
  }
  return false;
});
