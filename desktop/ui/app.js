const { invoke } = window.__TAURI__.core;
const { listen } = window.__TAURI__.event;

const logEl = document.getElementById("log");
const queueEl = document.getElementById("queue");
const dryRunEl = document.getElementById("dryRun");
const logLevelEl = document.getElementById("logLevel");
const logFilterEl = document.getElementById("logFilter");
const lines = [];
const ranks = { TRACE: 0, DEBUG: 1, INFO: 2, WARNING: 3, ERROR: 4 };
let catalog = [];
let campaign = { steps: [] };

function parseLine(raw) {
  try {
    return JSON.parse(raw);
  } catch {
    return { type: "log", event: "raw", level: "DEBUG", message: raw };
  }
}

function allowed(item) {
  const min = ranks[logLevelEl.value] ?? 1;
  const level = ranks[item.level || "INFO"] ?? 1;
  if (level < min) return false;
  const q = logFilterEl.value.trim().toLowerCase();
  if (!q) return true;
  return JSON.stringify(item).toLowerCase().includes(q);
}

function renderLog() {
  logEl.innerHTML = lines
    .filter(allowed)
    .slice(-500)
    .map((item) => {
      const cls = item.level === "ERROR" ? "error" : item.level === "WARNING" ? "warn" : "info";
      const extra = item.data ? " " + JSON.stringify(item.data) : "";
      const msg = item.message || "";
      return `<div class="${cls}">[${item.level || "INFO"}] ${item.event || "log"} ${msg}${extra}</div>`;
    })
    .join("");
  logEl.scrollTop = logEl.scrollHeight;
}

function renderQueue() {
  const steps = campaign.steps || [];
  queueEl.innerHTML = steps
    .map((step, index) => {
      const spec = catalog.find((item) => item.id === step.mode) || {};
      const status = step.status || spec.status || "stub";
      return `<label class="step ${status}">
        <div>
          <input type="checkbox" data-index="${index}" ${step.enabled ? "checked" : ""} />
          <strong>${step.title_ru || spec.title_ru || step.mode}</strong>
          <small>${step.official_name || spec.official_name || ""} · ${step.kingdom_ru || ""} · ${step.sent || 0}/${step.count || 0}</small>
        </div>
        <span class="badge ${status}">${status}</span>
        <input data-count="${index}" type="number" min="0" value="${step.count || 0}" />
      </label>`;
    })
    .join("");
  queueEl.querySelectorAll("input[data-index]").forEach((el) => el.addEventListener("change", pushCampaign));
  queueEl.querySelectorAll("input[data-count]").forEach((el) => el.addEventListener("change", pushCampaign));
}

function campaignPayload() {
  return [...queueEl.querySelectorAll(".step")].map((row, index) => {
    const source = (campaign.steps || [])[index] || {};
    return {
      mode: source.mode,
      enabled: row.querySelector("input[data-index]").checked,
      count: Number(row.querySelector("input[data-count]").value || 0),
    };
  });
}

async function cmd(command) {
  await invoke("send_engine_cmd", { command });
}

async function pushCampaign() {
  await cmd({ cmd: "set_campaign", queue: campaignPayload() });
}

function applyState(payload) {
  if (!payload) return;
  document.getElementById("activeMode").textContent = payload.active_mode || payload.campaign?.current_mode || "—";
  document.getElementById("botMode").textContent = payload.mode || "—";
  document.getElementById("inFlight").textContent = String(payload.in_flight ?? 0);
  document.getElementById("session").textContent = String(payload.session_attacks ?? 0);
  document.getElementById("lastError").textContent = payload.last_error || payload.stopped_reason || "нет";
  if (payload.catalog) catalog = payload.catalog;
  if (payload.campaign) campaign = payload.campaign;
  if (typeof payload.dry_run === "boolean") dryRunEl.checked = payload.dry_run;
  renderQueue();
}

document.getElementById("btnStart").onclick = () => cmd({ cmd: "start" });
document.getElementById("btnPause").onclick = () => cmd({ cmd: "pause" });
document.getElementById("btnStop").onclick = () => invoke("stop_engine");
document.getElementById("btnClear").onclick = () => {
  lines.length = 0;
  renderLog();
};
dryRunEl.onchange = () => cmd({ cmd: "set_dry_run", value: dryRunEl.checked });
logLevelEl.onchange = renderLog;
logFilterEl.oninput = renderLog;
window.addEventListener("beforeunload", () => invoke("stop_engine"));

await invoke("start_engine");
await listen("engine-line", (event) => {
  const item = parseLine(String(event.payload || ""));
  lines.push(item);
  if (item.type === "ready") {
    catalog = item.payload?.catalog || catalog;
    campaign = item.payload?.campaign || campaign;
    if (typeof item.payload?.dry_run === "boolean") dryRunEl.checked = item.payload.dry_run;
    renderQueue();
  }
  if (item.type === "state") applyState(item.payload);
  if (item.type === "ack") {
    if (item.payload?.campaign) campaign = item.payload.campaign;
    if (item.payload?.catalog) catalog = item.payload.catalog;
    renderQueue();
  }
  renderLog();
});
renderLog();
