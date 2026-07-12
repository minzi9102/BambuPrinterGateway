const queueList = document.querySelector("#queue");
const historyList = document.querySelector("#history");
const message = document.querySelector("#message");
const form = document.querySelector("#job-form");
const offlineNotice = document.querySelector("#offline-notice");
const amsStatus = document.querySelector("#ams-status");

function present(value, unit = "") {
  return value === null || value === undefined || value === "" ? "—" : `${value}${unit}`;
}

function pair(current, target, unit) {
  if ([current, target].every((value) => value === null || value === undefined || value === "")) return "—";
  return `${present(current, unit)} / ${present(target, unit)}`;
}

function renderTelemetry(printer) {
  const telemetry = printer.telemetry || {};
  const temperatures = telemetry.temperatures || {};
  const fans = telemetry.fans || {};
  const values = {
    "printer-task": present(printer.current_task),
    "printer-progress": present(printer.progress, "%"),
    "printer-remaining": present(printer.remaining_minutes, " min"),
    "printer-layer": pair(printer.layer, printer.total_layers, ""),
    "printer-nozzle": pair(temperatures.nozzle?.current, temperatures.nozzle?.target, " °C"),
    "printer-bed": pair(temperatures.bed?.current, temperatures.bed?.target, " °C"),
    "printer-chamber": present(temperatures.chamber, " °C"),
    "printer-cooling-fan": present(fans.cooling),
    "printer-heatbreak-fan": present(fans.heatbreak),
    "printer-auxiliary-fan-1": present(fans.auxiliary_1),
    "printer-auxiliary-fan-2": present(fans.auxiliary_2),
    "printer-wifi": present(telemetry.wifi_signal),
  };
  for (const [id, value] of Object.entries(values)) document.querySelector(`#${id}`).textContent = value;
}

function renderAmsStatus(trays) {
  const bySlot = new Map(trays.map((tray) => [tray.slot, tray]));
  const options = [0, 1, 2, 3].map((slot) => bySlot.get(slot) || { slot, label: `AMS Slot ${slot + 1}` });
  amsStatus.replaceChildren(
    ...options.map((tray) => {
      const card = document.createElement("div");
      const swatch = document.createElement("span");
      const copy = document.createElement("span");
      const title = document.createElement("strong");
      const description = document.createElement("span");
      const color = String(tray.color || "");
      const cssColor = /^[0-9a-f]{6}([0-9a-f]{2})?$/i.test(color) ? `#${color.slice(0, 6)}` : "#94a3b8";
      const details = String(tray.label || `AMS Slot ${tray.slot + 1}`).replace(`AMS Slot ${tray.slot + 1}`, "").replace(/^ - /, "") || "No material data";
      card.className = "ams-status-card";
      swatch.className = "ams-color";
      swatch.style.background = cssColor;
      copy.className = "ams-slot-copy";
      title.textContent = `AMS Slot ${tray.slot + 1}`;
      description.textContent = details;
      copy.append(title, description);
      card.append(swatch, copy);
      return card;
    }),
  );
}

async function refreshStatus() {
  const response = await fetch("/api/status");
  const data = await response.json();
  document.querySelector("#printer-state").textContent = data.printer.state;
  document.querySelector("#printer-connected").textContent = String(data.printer.connected);
  renderTelemetry(data.printer);
  renderAmsStatus(data.printer.ams_trays || []);
  const job = data.printer.current_job;
  const phase = job?.status === "STARTING" && !data.printer.connected
    ? "Reconnecting and starting"
    : { UPLOADING: "Uploading", STARTING: "Starting", PRINTING: "Printing" }[job?.status];
  document.querySelector("#current-job").textContent = job
    ? `${job.display_name} - ${job.project_name} · ${phase}`
    : "No active job";
  offlineNotice.hidden = data.printer.connected && !["offline", "unknown"].includes(data.printer.state);
}

async function refreshQueue() {
  const response = await fetch("/api/queue");
  const data = await response.json();
  queueList.replaceChildren(
    ...data.jobs.map((job) => {
      const item = document.createElement("li");
      item.textContent = `${job.display_name} - ${job.project_name}`;
      return item;
    }),
  );
}

async function refreshHistory() {
  const response = await fetch("/api/history");
  const data = await response.json();
  historyList.replaceChildren(
    ...data.jobs.map((job) => {
      const item = document.createElement("li");
      const finished = job.finished_at ? new Date(job.finished_at).toLocaleString() : "Unknown time";
      item.textContent = `${job.display_name} - ${job.project_name} · ${job.status} · ${finished}`;
      return item;
    }),
  );
  if (!data.jobs.length) historyList.textContent = "No print history.";
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  message.textContent = "Uploading...";
  const response = await fetch("/api/jobs", {
    method: "POST",
    body: new FormData(form),
  });
  if (response.ok) {
    form.reset();
    message.textContent = "Queued.";
    await refreshQueue();
    return;
  }
  const error = await response.json();
  message.textContent = error.detail || "Upload failed.";
});

function connectSocket() {
  const socket = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`);
  socket.addEventListener("message", (event) => {
    const data = JSON.parse(event.data);
    if (data.type === "queue.changed") refreshQueue();
    if (data.type === "job.changed") {
      refreshStatus();
      refreshQueue();
      refreshHistory();
    }
  });
  socket.addEventListener("close", () => setTimeout(connectSocket, 1000));
}

refreshStatus();
refreshQueue();
refreshHistory();
connectSocket();
setInterval(refreshQueue, 5000);
setInterval(refreshStatus, 5000);
setInterval(refreshHistory, 5000);
window.addEventListener("focus", refreshQueue);
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) refreshQueue();
});
