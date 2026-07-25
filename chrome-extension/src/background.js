/**
 * background.js — MV3 Service Worker
 *
 * Handles AI enrichment requests from popup.js.
 * Calls the OpenAI Chat Completions API and returns structured
 * EnrichedProduct[] data.
 */

const OPENAI_API_URL = "https://api.openai.com/v1/chat/completions";

// -----------------------------------------------------------------------
// System prompt for product enrichment
// -----------------------------------------------------------------------
const SYSTEM_PROMPT = `You are an expert product catalog formatter for an import/wholesale store.

You will receive a JSON array of raw product objects scraped from the Chinese B2B marketplace 1688.com.
Each object has: id, title, price, imageUrl, factoryName, productUrl.

Your task:
1. Translate the title to English if it is in Chinese (keep it concise, under 80 characters).
2. Clean up the price field:
   - If the price is in CNY (¥), convert it to USD using an approximate exchange rate of 1 USD = 7.2 CNY.
   - Format as "$X.XX – $Y.YY" for ranges or "$X.XX" for single prices.
   - If conversion is not possible, keep the original value.
3. Write a short one-sentence English product description (max 120 characters).
4. Keep all other fields exactly as provided (id, imageUrl, factoryName, productUrl).

Return ONLY a valid JSON array with the same number of elements as the input.
Each element must have: id, title, price, imageUrl, factoryName, productUrl, description.
Do not include any markdown formatting, commentary, or extra text — only the raw JSON array.`;

// -----------------------------------------------------------------------
// Message handler
// -----------------------------------------------------------------------
chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message.action === "enrichProducts") {
    handleEnrichment(message)
      .then((result) => sendResponse(result))
      .catch((err) => sendResponse({ success: false, error: err.message }));
    return true; // Keep channel open for async response
  }
});

// -----------------------------------------------------------------------
// Core enrichment function
// -----------------------------------------------------------------------
async function handleEnrichment({ products, apiKey, model, currency }) {
  if (!apiKey) {
    throw new Error("OpenAI API key not configured. Go to Settings.");
  }

  if (!products || products.length === 0) {
    return { success: true, products: [] };
  }

  const currencyNote = currency && currency.toUpperCase() !== "USD"
    ? ` Convert prices to ${currency} where possible.`
    : "";

  const userContent = JSON.stringify(products);

  const requestBody = {
    model: model || "gpt-4o",
    messages: [
      {
        role: "system",
        content: SYSTEM_PROMPT + currencyNote,
      },
      {
        role: "user",
        content: userContent,
      },
    ],
    temperature: 0.2,
    max_tokens: 4096,
    response_format: { type: "json_object" },
  };

  // OpenAI json_object mode requires the word "JSON" in system or user message
  // The system prompt already references "JSON array" — we add a wrapper key
  // for the json_object response format to parse reliably.
  const wrappedBody = {
    ...requestBody,
    messages: [
      requestBody.messages[0],
      {
        role: "user",
        content:
          'Return your answer as a JSON object with a single key "products" containing the array.\n\n' +
          userContent,
      },
    ],
  };

  let response;
  try {
    response = await fetch(OPENAI_API_URL, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${apiKey}`,
      },
      body: JSON.stringify(wrappedBody),
    });
  } catch (networkErr) {
    throw new Error("Network error contacting OpenAI: " + networkErr.message);
  }

  if (!response.ok) {
    let errMsg = `OpenAI API error: ${response.status}`;
    try {
      const errBody = await response.json();
      errMsg += " — " + (errBody?.error?.message || JSON.stringify(errBody));
    } catch (_) {}
    throw new Error(errMsg);
  }

  const data = await response.json();
  const rawContent = data?.choices?.[0]?.message?.content;

  if (!rawContent) {
    throw new Error("Empty response from OpenAI.");
  }

  let parsed;
  try {
    parsed = JSON.parse(rawContent);
  } catch (_) {
    throw new Error("OpenAI returned invalid JSON.");
  }

  // Unwrap if the model returned { products: [...] }
  const enrichedArray = Array.isArray(parsed)
    ? parsed
    : Array.isArray(parsed?.products)
    ? parsed.products
    : null;

  if (!enrichedArray) {
    throw new Error("Unexpected response shape from OpenAI.");
  }

  // Merge back any missing fields from originals (safety net)
  const originalMap = new Map(products.map((p) => [p.id, p]));
  const merged = enrichedArray.map((ep) => {
    const orig = originalMap.get(ep.id) || {};
    return {
      ...orig,
      ...ep,
      // Ensure imageUrl is never lost (AI doesn't modify images)
      imageUrl: ep.imageUrl || orig.imageUrl || "",
    };
  });

  return { success: true, products: merged };
}
