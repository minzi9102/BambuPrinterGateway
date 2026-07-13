const queueList = document.querySelector("#queue");
const historyList = document.querySelector("#history");
const message = document.querySelector("#message");
const form = document.querySelector("#job-form");
const offlineNotice = document.querySelector("#offline-notice");
const amsStatus = document.querySelector("#ams-status");
const printerState = document.querySelector("#printer-state");
const printerConnected = document.querySelector("#printer-connected");
const printerStateBadge = document.querySelector("#printer-state-badge");
const printerConnectedBadge = document.querySelector("#printer-connected-badge");
const currentJob = document.querySelector("#current-job");
const currentPhase = document.querySelector("#current-phase");
const printerProgress = document.querySelector("#printer-progress");
const progressBar = document.querySelector("#printer-progress-bar");
const queueCount = document.querySelector("#queue-count");
const fanStatus = document.querySelector("#printer-fan-status");
const wifiStatus = document.querySelector("#printer-wifi-status");
const printerPreview = document.querySelector("#printer-preview");
const printerPreviewEmpty = document.querySelector("#printer-preview-empty");

const printerStateLabels = {
  failed: "故障",
  finished: "已完成",
  idle: "空闲",
  offline: "离线",
  paused: "已暂停",
  printing: "打印中",
  reconnecting: "重连中",
  starting: "启动中",
  unknown: "未知",
};

const jobStatusLabels = {
  CANCELLED: "已取消",
  COMPLETED: "已完成",
  FAILED: "失败",
  PRINTING: "打印中",
  QUEUED: "排队中",
  STARTING: "启动中",
  UPLOADING: "上传中",
};

function missing(value) {
  return value === null || value === undefined || value === "";
}

function present(value, unit = "") {
  return missing(value) ? "—" : `${value}${unit}`;
}

function pair(current, target, unit) {
  if ([current, target].every(missing)) return "—";
  return `${present(current, unit)} / ${present(target, unit)}`;
}

function numberValue(value) {
  const number = Number(value);
  return missing(value) || !Number.isFinite(number) ? null : number;
}

function temperaturePair(current, target) {
  const values = [current, target].map(numberValue);
  if (values.every((value) => value === null)) return "—";
  return values
    .map((value) => value === null ? "—" : `${(Math.trunc(value * 10) / 10).toFixed(1)} °C`)
    .join(" / ");
}

function setIconLabel(element, label) {
  element.title = label;
  element.setAttribute("aria-label", label);
}

function renderStatusIcons(fans, wifiSignal) {
  const readings = [fans.cooling, fans.heatbreak, fans.auxiliary_1, fans.auxiliary_2]
    .map(numberValue)
    .filter((value) => value !== null);
  const fanState = readings.length ? (readings.some((value) => value > 0) ? "active" : "inactive") : "unknown";
  const fanLabel = { active: "风扇运行中", inactive: "风扇已停止", unknown: "风扇状态未知" }[fanState];
  fanStatus.dataset.state = fanState;
  setIconLabel(fanStatus, fanLabel);

  const signal = Number.parseFloat(String(wifiSignal));
  const hasSignal = !missing(wifiSignal) && Number.isFinite(signal);
  const level = !hasSignal ? 0 : signal >= -55 ? 3 : signal >= -67 ? 2 : signal >= -80 ? 1 : 0;
  const quality = ["无信号", "弱", "中", "强"][level];
  wifiStatus.dataset.level = String(level);
  setIconLabel(wifiStatus, hasSignal ? `Wi-Fi 信号：${wifiSignal}（${quality}）` : "Wi-Fi 信号未知");
}

function renderTelemetry(printer) {
  const telemetry = printer.telemetry || {};
  const temperatures = telemetry.temperatures || {};
  const fans = telemetry.fans || {};
  const values = {
    "printer-task": present(printer.current_task),
    "printer-remaining": present(printer.remaining_minutes, " 分钟"),
    "printer-layer": pair(printer.layer, printer.total_layers, ""),
    "printer-nozzle": temperaturePair(temperatures.nozzle?.current, temperatures.nozzle?.target),
    "printer-bed": temperaturePair(temperatures.bed?.current, temperatures.bed?.target),
  };
  for (const [id, value] of Object.entries(values)) document.querySelector(`#${id}`).textContent = value;
  renderStatusIcons(fans, telemetry.wifi_signal);

  const rawProgress = Number(printer.progress);
  const hasProgress = !missing(printer.progress) && Number.isFinite(rawProgress);
  const progress = hasProgress ? Math.min(100, Math.max(0, rawProgress)) : 0;
  printerProgress.textContent = hasProgress ? `${progress}%` : "—";
  progressBar.value = progress;
  progressBar.setAttribute("aria-valuetext", hasProgress ? `${progress}%` : "暂无进度数据");
}

function renderPreview(job) {
  const jobId = job?.id || "";
  if (printerPreview.dataset.jobId === jobId) return;
  printerPreview.dataset.jobId = jobId;
  printerPreview.hidden = true;
  printerPreviewEmpty.hidden = false;
  printerPreview.alt = job ? `${job.project_name} 打印预览` : "当前打印预览";
  if (job) printerPreview.src = `/api/jobs/${encodeURIComponent(job.id)}/preview`;
  else printerPreview.removeAttribute("src");
}

printerPreview.addEventListener("load", () => {
  printerPreview.hidden = false;
  printerPreviewEmpty.hidden = true;
});
printerPreview.addEventListener("error", () => {
  printerPreview.hidden = true;
  printerPreviewEmpty.hidden = false;
});

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
      const slotName = `AMS Slot ${tray.slot + 1}`;
      const details = String(tray.label || slotName).replace(slotName, "").replace(/^ - /, "") || "暂无材料数据";
      card.className = "ams-status-card";
      swatch.className = "ams-color";
      swatch.style.background = cssColor;
      copy.className = "ams-slot-copy";
      title.textContent = `AMS 槽位 ${tray.slot + 1}`;
      description.textContent = details;
      copy.append(title, description);
      card.append(swatch, copy);
      return card;
    }),
  );
}

function emptyItem(text) {
  const item = document.createElement("li");
  item.className = "empty-list";
  item.textContent = text;
  return item;
}

async function refreshStatus() {
  const response = await fetch("/api/status");
  const data = await response.json();
  const state = data.printer.state || "unknown";
  const connected = Boolean(data.printer.connected);
  printerState.textContent = printerStateLabels[state] || state;
  printerStateBadge.dataset.state = state;
  printerConnected.textContent = connected ? "已连接" : "未连接";
  printerConnectedBadge.dataset.connected = String(connected);
  renderTelemetry(data.printer);
  renderAmsStatus(data.printer.ams_trays || []);

  const job = data.printer.current_job;
  const phase = job?.status === "STARTING" && !connected
    ? "重连并启动中"
    : jobStatusLabels[job?.status] || "处理中";
  currentJob.textContent = job
    ? `${job.display_name} · ${job.project_name}`
    : "暂无活动任务";
  currentPhase.textContent = job ? phase : "无任务";
  currentPhase.dataset.status = job?.status || "NONE";
  renderPreview(job);
  offlineNotice.hidden = connected && !["offline", "unknown"].includes(state);
}

async function refreshQueue() {
  const response = await fetch("/api/queue");
  const data = await response.json();
  queueCount.textContent = `${data.jobs.length} 个任务`;
  if (!data.jobs.length) {
    queueList.replaceChildren(emptyItem("当前没有等待任务"));
    return;
  }
  queueList.replaceChildren(
    ...data.jobs.map((job, index) => {
      const item = document.createElement("li");
      const position = document.createElement("span");
      const details = document.createElement("span");
      const project = document.createElement("strong");
      const submitter = document.createElement("span");
      const status = document.createElement("span");
      item.className = "task-item";
      position.className = "task-position";
      details.className = "task-copy";
      status.className = "status-pill";
      status.dataset.status = job.status;
      position.textContent = String(job.position || index + 1);
      project.textContent = job.project_name;
      submitter.textContent = `提交人：${job.display_name}`;
      status.textContent = jobStatusLabels[job.status] || job.status;
      details.append(project, submitter);
      item.append(position, details, status);
      return item;
    }),
  );
}

function localTime(value) {
  if (!value) return "时间未知";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "时间未知" : date.toLocaleString("zh-CN");
}

async function refreshHistory() {
  const response = await fetch("/api/history");
  const data = await response.json();
  if (!data.jobs.length) {
    historyList.replaceChildren(emptyItem("暂无打印历史"));
    return;
  }
  historyList.replaceChildren(
    ...data.jobs.map((job) => {
      const item = document.createElement("li");
      const details = document.createElement("span");
      const project = document.createElement("strong");
      const meta = document.createElement("span");
      const status = document.createElement("span");
      item.className = "history-item";
      details.className = "task-copy";
      status.className = "status-pill";
      status.dataset.status = job.status;
      project.textContent = `${job.display_name} · ${job.project_name}`;
      meta.textContent = localTime(job.finished_at);
      status.textContent = jobStatusLabels[job.status] || job.status;
      details.append(project, meta);
      item.append(details, status);
      return item;
    }),
  );
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  message.textContent = "正在上传…";
  const response = await fetch("/api/jobs", {
    method: "POST",
    body: new FormData(form),
  });
  if (response.ok) {
    form.reset();
    message.textContent = "任务已加入队列。";
    await refreshQueue();
    return;
  }
  message.textContent = "提交失败，请检查文件后重试。";
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
