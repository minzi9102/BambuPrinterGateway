const state = document.querySelector("#printer-state");
const connected = document.querySelector("#printer-connected");
const nextJob = document.querySelector("#next-job");
const message = document.querySelector("#message");
const button = document.querySelector("#start-next");
const amsSlot = document.querySelector("#ams-slot");
let authHeader = null;

function renderAmsSlots(trays) {
  const selected = amsSlot.value || "0";
  const options = trays.length
    ? trays
    : [0, 1, 2, 3].map((slot) => ({ slot, label: `AMS Slot ${slot + 1}` }));
  amsSlot.replaceChildren(
    ...options.map((tray) => {
      const option = document.createElement("option");
      option.value = String(tray.slot);
      option.textContent = tray.label;
      return option;
    }),
  );
  amsSlot.value = options.some((tray) => String(tray.slot) === selected) ? selected : "0";
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
  if (!authHeader) {
    const username = prompt("Admin username");
    const password = prompt("Admin password");
    authHeader = `Basic ${btoa(`${username}:${password}`)}`;
  }
  const response = await fetch("/api/admin/start-next", {
    method: "POST",
    headers: { Authorization: authHeader, "Content-Type": "application/json" },
    body: JSON.stringify({ ams_slot: Number(amsSlot.value) }),
  });
  if (response.status === 401) authHeader = null;
  if (response.ok) {
    const data = await response.json();
    message.textContent = `Started ${data.job.display_name} - ${data.job.project_name} with AMS Slot ${Number(amsSlot.value) + 1}`;
    await refresh();
    return;
  }
  const error = await response.json();
  message.textContent = error.detail || "Start failed.";
});

function connectSocket() {
  const socket = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`);
  socket.addEventListener("message", refresh);
  socket.addEventListener("close", () => setTimeout(connectSocket, 1000));
}

refresh();
connectSocket();
setInterval(refresh, 5000);
