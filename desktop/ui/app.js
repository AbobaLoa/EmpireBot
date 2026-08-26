const statusEl = document.getElementById("status");
const PANEL = "http://127.0.0.1:8766/";

function setStatus(text) {
  if (statusEl) statusEl.textContent = text;
}

async function ensurePanel() {
  const tauri = window.__TAURI__;
  if (tauri?.core?.invoke) {
    return tauri.core.invoke("ensure_panel");
  }
  const res = await fetch("http://127.0.0.1:8766/health");
  if (!res.ok) {
    throw new Error("панель не отвечает");
  }
  return "attached";
}

try {
  const result = await ensurePanel();
  if (result === "attached") {
    setStatus("Подключено к уже запущенной панели");
  } else if (result === "waiting-existing-bot") {
    setStatus("Бот уже работает — открываю панель");
  } else {
    setStatus("Панель запущена, охота на паузе до Num1");
  }
  window.location.replace(PANEL);
} catch (err) {
  setStatus(String(err?.message || err || "не удалось открыть панель"));
}
