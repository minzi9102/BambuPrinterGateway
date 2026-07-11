const queueList = document.querySelector("#queue");
const historyList = document.querySelector("#history");
const message = document.querySelector("#message");
const form = document.querySelector("#job-form");
const offlineNotice = document.querySelector("#offline-notice");

async function refreshStatus() {
  const response = await fetch("/api/status");
  const data = await response.json();
  document.querySelector("#printer-state").textContent = data.printer.state;
  document.querySelector("#printer-connected").textContent = String(data.printer.connected);
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
