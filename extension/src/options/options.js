const fields = ["apiBaseUrl", "apiKey", "telegramUserId", "maxProducts"];

async function load() {
  const stored = await chrome.storage.sync.get({
    apiBaseUrl: "http://localhost:8000",
    apiKey: "",
    telegramUserId: "",
    maxProducts: 20,
  });
  fields.forEach((field) => {
    document.getElementById(field).value = stored[field];
  });
}

async function save() {
  const values = {};
  fields.forEach((field) => {
    values[field] = document.getElementById(field).value.trim();
  });
  await chrome.storage.sync.set(values);
  document.getElementById("saved").textContent = "Сохранено";
  setTimeout(() => {
    document.getElementById("saved").textContent = "";
  }, 2000);
}

document.getElementById("saveBtn").addEventListener("click", save);
load();
