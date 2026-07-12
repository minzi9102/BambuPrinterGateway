# Bambu Printer Gateway

面向单台 Bambu Lab 打印机的局域网打印队列。系统由 FastAPI、Vanilla JS 和 SQLite
组成：用户在公共页面上传已切片的 `.gcode.3mf`，管理员可管理等待队列、选择 AMS 槽位并手动启动队首任务，
页面通过 WebSocket 和定时轮询同步打印机、队列、活动任务及最近 100 条历史记录。

## 当前能力

- SQLite FIFO 队列与上传文件持久化。
- 校验 `.gcode.3mf`/ZIP 完整性及 `Metadata/plate_1.gcode`。
- 公共页和管理页展示 MQTT 打印状态、任务、进度、剩余时间、层数、温度、风扇、Wi-Fi 和 AMS 槽位。
- MQTT 连接失败每 30 秒重试；运行中断线由 MQTT keepalive 自动以 30 秒间隔重连。
- 管理员 Basic Auth、等待任务上移/下移、移出队列、AMS 槽位选择和队首启动。
- 启动前刷新 MQTT 连接，确认远程文件存在，并等待打印机进入 `RUNNING`。
- 明确显示 Active Job、Next Queued Job 及上传、重连、启动、打印阶段。
- 自动对账打印完成/失败状态；服务重启后保留队列和正在打印的任务。
- 公共历史隐藏内部错误，管理员历史显示失败原因。
- 独立的真实硬件 Gate 和长期 Gateway 状态监控命令。

当前边界：单打印机、无用户账户；仅管理员可管理 `QUEUED` 任务，启动文件固定使用
`Metadata/plate_1.gcode`，其他 plate 路径不受支持。

## 环境要求

- Python `3.12`（项目声明范围为 `>=3.12,<3.13`）。
- [uv](https://docs.astral.sh/uv/) 管理项目虚拟环境和命令。
- 支持 FTPS 的 `curl`；程序会执行 `curl --version` 检查。
- 打印机与服务器处于同一可达局域网。
- 打印机已开启 LAN Mode（仅局域网模式）和 Developer Mode（开发者模式）。
- 已取得打印机 IP、Access Code 和序列号。

## 快速启动 Web Queue

在仓库根目录执行：

```powershell
uv sync
Copy-Item .env.example .env
```

编辑 `.env`，至少替换：

```dotenv
PRINTER_HOST=192.168.1.101
PRINTER_ACCESS_CODE=replace-me
PRINTER_SERIAL=replace-me
ADMIN_USERNAME=admin
ADMIN_PASSWORD=CHANGE_ME
```

不要提交 `.env`；它已加入 `.gitignore`。启动服务：

```powershell
uv run bambu-queue-server
```

默认页面：

- 公共队列：<http://127.0.0.1:8000/>
- 管理员页面：<http://127.0.0.1:8000/admin.html>
- FastAPI OpenAPI：<http://127.0.0.1:8000/docs>

Web 命令会读取仓库根目录的 `.env`；环境中已存在的同名变量优先。

## 使用流程

1. 用户在公共页面填写姓名、项目名并上传已切片的 `.gcode.3mf`。
2. 文件通过格式和大小校验后进入 SQLite FIFO 队列。
3. 管理员打开 `/admin.html`，输入 Basic Auth 凭据并确认打印板已清理。
4. 管理员选择 AMS Slot 1–4，点击 **Start Next Job**。
5. 系统依次上传文件、确认打印机端文件、刷新 MQTT 连接、发送打印命令并等待状态确认。
6. 页面显示 `Uploading`、`Reconnecting and starting`、`Starting`、`Printing`；启动期间按钮禁用。
7. 打印机回到 `IDLE`/`FINISH` 时任务记为 `COMPLETED`，报告 `FAILED` 时记为 `FAILED`。

公共页面显示打印机连接、当前活动任务、队列和历史。打印机离线或状态未知时会提示：

```text
PRINTER OFFLINE
Printing is temporarily unavailable. Existing queue has been preserved.
```

管理员页面额外显示 Active Job、Next Queued Job、AMS 材料/颜色/余量、失败详情和 Debug 面板。

## 状态模型

任务状态转换：

```text
QUEUED -> UPLOADING -> STARTING -> PRINTING -> COMPLETED
           |             |           |
           +-> FAILED    +-> FAILED  +-> FAILED

QUEUED -> CANCELLED
```

`UPLOADING`、`STARTING`、`PRINTING` 属于 Active Job；只有 `QUEUED` 属于等待队列。
打印机原始状态会归一化为 `idle`、`starting`、`printing`、`paused`、`finished`、`failed`
或 `unknown`。任务处于 `STARTING` 且系统正在主动重连时，页面显示 `reconnecting`，不会误报为普通离线；
MQTT 重连成功但尚未收到新状态时显示 `unknown`。

## 配置

| 变量 | 默认值 | 用途 |
| --- | --- | --- |
| `PRINTER_HOST` | 无 | 打印机 IP；Web 连接打印机时必填 |
| `PRINTER_ACCESS_CODE` | 无 | 打印机 LAN Access Code；敏感信息 |
| `PRINTER_SERIAL` | 无 | 打印机序列号 |
| `ADMIN_USERNAME` | `admin` | 管理员 Basic Auth 用户名 |
| `ADMIN_PASSWORD` | `CHANGE_ME` | 管理员 Basic Auth 密码，部署前必须修改 |
| `DATABASE_PATH` | `data/queue.db` | SQLite 数据库路径 |
| `UPLOAD_DIR` | `uploads` | 已校验上传文件目录 |
| `MAX_UPLOAD_MB` | `500` | 单文件大小上限，单位 MB |
| `START_CONFIRM_TIMEOUT` | `120` | 等待打印机进入打印状态的秒数 |
| `UPLOAD_TIMEOUT` | `600` | FTPS 上传和远程文件查询超时秒数 |
| `BAMBU_QUEUE_HOST` | `127.0.0.1` | Web 监听地址；局域网访问可设为 `0.0.0.0` |
| `BAMBU_QUEUE_PORT` | `8000` | Web 监听端口 |
| `PRINTER_USE_AMS` | `false` | Phase 0 命令是否使用 AMS |
| `PRINTER_AMS_MAPPING` | `[]` | Phase 0 原始 AMS JSON 映射，如 `[0]` |

如果三项 `PRINTER_*` 连接信息不完整，Web 服务仍可启动并管理本地队列，但打印机状态为
`unknown` 且管理员无法启动任务。

## HTTP / WebSocket 接口

| 方法 | 路径 | 认证 | 说明 |
| --- | --- | --- | --- |
| `GET` | `/api/status` | 无 | 打印机、Active Job、进度、温度、风扇、Wi-Fi 和 AMS 状态 |
| `GET` | `/api/queue` | 无 | 当前 `QUEUED` 任务和 FIFO 位置 |
| `GET` | `/api/history` | 无 | 最近 100 条终态任务，不返回错误详情 |
| `POST` | `/api/jobs` | 无 | `multipart/form-data` 上传任务 |
| `GET` | `/api/admin/history` | Basic Auth | 最近 100 条终态任务及 `error_message` |
| `GET` | `/api/admin/debug` | Basic Auth | 运行时、打印机原始字段、AMS 和队列诊断信息 |
| `POST` | `/api/admin/start-next` | Basic Auth | 启动队首任务，JSON 为 `{"ams_slot": 0}`，有效值 `0..3` |
| `POST` | `/api/admin/jobs/{job_id}/move` | Basic Auth | 将等待任务上移或下移一位，JSON 为 `{"direction":"up"}` 或 `{"direction":"down"}` |
| `POST` | `/api/admin/jobs/{job_id}/cancel` | Basic Auth | 将等待任务标为 `CANCELLED`，保留历史和上传文件 |
| WebSocket | `/ws` | 无 | 推送 `queue.changed` 和 `job.changed`，客户端收到后重新拉取状态 |

上传接口字段：

```text
display_name   必填，提交者名称
project_name   必填，项目名称
file           必填，完整 sliced 3MF，且包含 Metadata/plate_1.gcode
```

Basic Auth 在默认 HTTP 连接上不加密。仅在可信局域网使用，或在公网部署时放到提供 HTTPS
的反向代理后方。

## 持久化与重启行为

- SQLite 表和上传目录在首次使用时自动创建。
- 服务启动时保留 `QUEUED` 和 `PRINTING`。
- 遗留的 `UPLOADING`/`STARTING` 会标为 `FAILED`，错误为
  `Server restarted during job startup`。
- `/api/status` 会根据打印机状态对账：`PRINTING + IDLE/FINISH` 变为 `COMPLETED`，
  `PRINTING + FAILED` 变为 `FAILED`。
- 重启 Web 服务不会向打印机发送停止命令，实体打印会继续，但网页和状态监控会短暂中断。
- 避免在 `UPLOADING` 或 `STARTING` 阶段重启；稳定 `PRINTING` 时可重启并在恢复后核对 Current Job。

## Linux systemd 部署

先在目标目录安装依赖并配置 `.env`：

```bash
cd /opt/bambu-printer-gateway
uv sync
cp .env.example .env
```

修改 `deploy/printer-queue.service.example` 中的 `WorkingDirectory` 和 `EnvironmentFile`，然后安装：

```bash
sudo cp deploy/printer-queue.service.example /etc/systemd/system/printer-queue.service
sudo systemctl daemon-reload
sudo systemctl enable --now printer-queue.service
sudo systemctl status printer-queue.service
```

查看日志和重启：

```bash
journalctl -u printer-queue.service -f
sudo systemctl restart printer-queue.service
```

服务使用 `uv run --no-sync`，因此更新依赖后应先在项目目录执行 `uv sync`。

## 测试

所有 Python 命令必须通过项目内的 uv 环境执行：

```powershell
uv run python -m unittest discover -s tests -v
node --check src/bambu_printer_gateway/static/app.js
node --check src/bambu_printer_gateway/static/admin.js
```

测试覆盖文件校验、MQTT/FTPS 安全路径、FIFO 和状态机、服务重启恢复、管理员启动、AMS、
实时广播、历史权限及静态页面行为。

## Phase 0：真实硬件 Gate

该命令用于在进入 Web 队列前验证一份小型 sliced 3MF 的真实上传和打印链路。它不自动读取
`.env`，请先在当前终端设置连接信息：

```powershell
$env:PRINTER_HOST = "192.168.1.101"
$env:PRINTER_ACCESS_CODE = "..."
$env:PRINTER_SERIAL = "..."
uv run bambu-phase0 .\tiny.gcode.3mf
```

程序会校验文件、监听 MQTT、通过 FTPS 上传并确认远程文件。只有操作员输入命令显示的完整
`START phase0_....gcode.3mf` 后才会发送打印命令；随后还需在实体打印开始和结束时分别输入
`STARTED`、`COMPLETED`。默认不会自动重试。

可选参数：

```text
--artifacts-dir phase0-artifacts
--connect-timeout 30
--command-timeout 10
--start-timeout 120
--upload-timeout 600
```

需要 AMS 时：

```powershell
$env:PRINTER_USE_AMS = "true"
$env:PRINTER_AMS_MAPPING = "[0]"
```

脱敏状态、命令响应和 `phase0-results.md` 写入 `phase0-artifacts/`。中断程序只停止本地监听，
不会停止已经开始的打印；执行前必须清理打印板，结束后按需从打印机删除测试文件。

## Gateway 长期监控

Gateway Monitor 持续记录打印机状态，不创建 Web 服务：

```powershell
$env:PRINTER_HOST = "192.168.1.101"
$env:PRINTER_ACCESS_CODE = "..."
$env:PRINTER_SERIAL = "..."
uv run bambu-gateway-monitor
```

它会输出状态、进度、剩余时间、层数、温度、风扇、WiFi、AMS 和错误字段，并将完整快照写入：

```text
phase0-artifacts/gateway-monitor.jsonl
```

可用 `--artifacts-dir`、`--connect-timeout` 和 `--command-timeout` 调整输出目录及超时。
按 `Ctrl+C` 会停止监听并断开本地 MQTT/执行连接，不会停止打印机上的任务。
