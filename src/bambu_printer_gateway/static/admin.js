const state = document.querySelector("#printer-state");
const connected = document.querySelector("#printer-connected");
const nextJob = document.querySelector("#next-job");
const message = document.querySelector("#message");
const button = document.querySelector("#start-next");
const amsSlots = document.querySelector("#ams-slots");
const debugToggle = document.querySelector("#debug-toggle");
const debugPanel = document.querySelector("#debug-panel");
const debugRefresh = document.querySelector("#debug-refresh");
const debugUpdated = document.querySelector("#debug-updated");
const debugOutput = document.querySelector("#debug-output");
let authHeader = null;
let selectedAmsSlot = 0;

function ensureAuth() {
  if (!authHeader) {
    const username = prompt("Admin username");
    const password = prompt("Admin password");
    authHeader = `Basic ${btoa(`${username}:${password}`)}`;
  }
}

function renderAmsSlots(trays) {
  const bySlot = new Map(trays.map((tray) => [tray.slot, tray]));
  const options = [0, 1, 2, 3].map((slot) => bySlot.get(slot) || { slot, label: `AMS Slot ${slot + 1}` });
  amsSlots.style.display = "grid";
  amsSlots.style.gridTemplateColumns = "repeat(4, minmax(0, 1fr))";
  amsSlots.style.gap = "10px";
  amsSlots.style.marginBottom = "16px";
  amsSlots.replaceChildren(
    ...options.map((tray) => {
      const card = document.createElement("button");
      const color = String(tray.color || "");
      const cssColor = /^[0-9a-f]{6}([0-9a-f]{2})?$/i.test(color) ? `#${color.slice(0, 6)}` : "#94a3b8";
      const details = tray.label.replace(`AMS Slot ${tray.slot + 1}`, "").replace(/^ - /, "") || "No material data";
      card.type = "button";
      card.className = "ams-slot-card";
      card.dataset.slot = String(tray.slot);
      card.setAttribute("role", "radio");
      card.setAttribute("aria-checked", String(tray.slot === selectedAmsSlot));
      card.style.display = "flex";
      card.style.gap = "10px";
      card.style.alignItems = "center";
      card.style.minHeight = "78px";
      card.style.padding = "10px";
      card.style.background = tray.slot === selectedAmsSlot ? "#eff6ff" : "#fff";
      card.style.color = "#1f2937";
      card.style.border = `2px solid ${tray.slot === selectedAmsSlot ? "#2563eb" : "#cbd5e1"}`;
      card.style.textAlign = "left";
      const swatch = document.createElement("span");
      const copy = document.createElement("span");
      const title = document.createElement("strong");
      const description = document.createElement("span");
      swatch.className = "ams-color";
      swatch.style.background = cssColor;
      swatch.style.display = "inline-block";
      swatch.style.flex = "0 0 28px";
      swatch.style.width = "28px";
      swatch.style.height = "28px";
      swatch.style.borderRadius = "999px";
      swatch.style.border = "1px solid rgb(15 23 42 / 22%)";
      copy.className = "ams-slot-copy";
      title.textContent = `AMS Slot ${tray.slot + 1}`;
      description.textContent = details;
      copy.append(title, description);
      card.append(swatch, copy);
      card.addEventListener("click", () => {
        selectedAmsSlot = tray.slot;
        renderAmsSlots(trays);
      });
      return card;
    }),
  );
}

async function refresh() {
  const [statusResponse, queueResponse] = await Promise.all([
    fetch("/api/status"),
    fetch("/api/queue"),
  ]);
  const status = await statusResponse.json();
  const queue = await queueResponse.json();
  state.textContent = status.printer.state;
  connected.textContent = String(status.printer.connected);
  renderAmsSlots(status.printer.ams_trays || []);
  nextJob.textContent = queue.jobs[0]
    ? `${queue.jobs[0].display_name} - ${queue.jobs[0].project_name}`
    : "None";
}

button.addEventListener("click", async () => {
  message.textContent = "Starting...";
  ensureAuth();
  const response = await fetch("/api/admin/start-next", {
    method: "POST",
    headers: { Authorization: authHeader, "Content-Type": "application/json" },
    body: JSON.stringify({ ams_slot: selectedAmsSlot }),
  });
  if (response.status === 401) authHeader = null;
  if (response.ok) {
    const data = await response.json();
    message.textContent = `Started ${data.job.display_name} - ${data.job.project_name} with AMS Slot ${selectedAmsSlot + 1}`;
    await refresh();
    return;
  }
  const error = await response.json();
  message.textContent = error.detail || "Start failed.";
});

async function refreshDebug() {
  ensureAuth();
  debugOutput.textContent = "Loading...";
  const [statusResponse, queueResponse, debugResponse] = await Promise.all([
    fetch("/api/status"),
    fetch("/api/queue"),
    fetch("/api/admin/debug", { headers: { Authorization: authHeader } }),
  ]);
  if (debugResponse.status === 401) authHeader = null;
  const payload = {
    status: await statusResponse.json(),
    queue: await queueResponse.json(),
    debug: await debugResponse.json(),
  };
  debugUpdated.textContent = new Date().toLocaleString();
  debugOutput.textContent = JSON.stringify(payload, null, 2);
}

debugToggle.addEventListener("click", async () => {
  debugPanel.hidden = !debugPanel.hidden;
  if (!debugPanel.hidden) await refreshDebug();
});

debugRefresh.addEventListener("click", refreshDebug);

function connectSocket() {
  const socket = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`);
  socket.addEventListener("message", refresh);
  socket.addEventListener("close", () => setTimeout(connectSocket, 1000));
}

refresh();
connectSocket();
setInterval(refresh, 5000);
