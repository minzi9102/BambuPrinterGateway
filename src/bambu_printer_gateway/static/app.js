const queueList = document.querySelector("#queue");
const message = document.querySelector("#message");
const form = document.querySelector("#job-form");
const offlineNotice = document.querySelector("#offline-notice");

async function refreshStatus() {
  const response = await fetch("/api/status");
  const data = await response.json();
  document.querySelector("#printer-state").textContent = data.printer.state;
  document.querySelector("#printer-connected").textContent = String(data.printer.connected);
  document.querySelector("#current-job").textContent = data.printer.current_job
    ? `${data.printer.current_job.display_name} - ${data.printer.current_job.project_name}`
    : "None";
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
    }
  });
  socket.addEventListener("close", () => setTimeout(connectSocket, 1000));
}

refreshStatus();
refreshQueue();
connectSocket();
setInterval(refreshQueue, 5000);
setInterval(refreshStatus, 5000);
window.addEventListener("focus", refreshQueue);
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) refreshQueue();
});
