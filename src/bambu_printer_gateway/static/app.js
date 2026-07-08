const queueList = document.querySelector("#queue");
const message = document.querySelector("#message");
const form = document.querySelector("#job-form");

async function refreshStatus() {
  const response = await fetch("/api/status");
  const data = await response.json();
  document.querySelector("#printer-state").textContent = data.printer.state;
  document.querySelector("#printer-connected").textContent = String(data.printer.connected);
}

async function refreshQueue() {
  const response = await fetch("/api/queue");
  const data = await response.json();
  queueList.replaceChildren(
    ...data.jobs.map((job) => {
      const item = document.createElement("li");
      item.textContent = `${job.position}. ${job.display_name} - ${job.project_name}`;
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
  });
  socket.addEventListener("close", () => setTimeout(connectSocket, 1000));
}

refreshStatus();
refreshQueue();
connectSocket();
