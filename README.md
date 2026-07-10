# Bambu Printer Gateway

当前实现了本地 Web 队列：公共页面上传 sliced `.gcode.3mf`，管理员页面手动启动队首任务。
队列使用 SQLite 持久化，服务重启后 `QUEUED` 和 `PRINTING` 任务会保留；重启时遗留的
`UPLOADING` / `STARTING` 任务会标记为 `FAILED`。首页和管理员页显示最近 100 条打印历史，
公共首页隐藏失败详情，管理员认证后可查看具体错误。

## Web Queue

复制 `.env.example` 为 `.env`，填入打印机和管理员配置后启动：

```powershell
uv sync
uv run bambu-queue-server
```

默认地址：

```text
http://127.0.0.1:8000/
http://127.0.0.1:8000/admin.html
```

可用环境变量：

```text
BAMBU_QUEUE_HOST=127.0.0.1
BAMBU_QUEUE_PORT=8000
DATABASE_PATH=data/queue.db
UPLOAD_DIR=uploads
MAX_UPLOAD_MB=500
START_CONFIRM_TIMEOUT=120
UPLOAD_TIMEOUT=600
```

公共页面在打印机离线或状态未知时会显示：

```text
PRINTER OFFLINE
Printing is temporarily unavailable. Existing queue has been preserved.
```

## Linux systemd

示例文件在：

```text
deploy/printer-queue.service.example
```

安装时把其中 `/opt/bambu-printer-gateway` 改成实际项目路径，然后：

```bash
sudo cp deploy/printer-queue.service.example /etc/systemd/system/printer-queue.service
sudo systemctl daemon-reload
sudo systemctl enable --now printer-queue.service
sudo systemctl status printer-queue.service
journalctl -u printer-queue.service -f
```

重启服务：

```bash
sudo systemctl restart printer-queue.service
```

## Phase 0 / Gateway Monitor

```powershell
uv sync
$env:PRINTER_HOST = "192.168.1.101"
$env:PRINTER_ACCESS_CODE = "..."
$env:PRINTER_SERIAL = "..."
uv run bambu-phase0 .\tiny.gcode.3mf
```

命令会校验文件、监听 MQTT、上传并确认远程文件。只有操作员输入命令显示的完整
`START phase0_....gcode.3mf` 后才会启动打印。

程序会等待 MQTT 确认任务状态发生变化；默认 120 秒无变化即失败，且不会自动重试。
打印机物理开始和完成时分别输入 `STARTED`、`COMPLETED`。脱敏状态记录及
`phase0-results.md` 保存在 `phase0-artifacts/`，该目录不会提交到 Git。

运行前必须确认打印板已清理。中断程序不会停止已经开始的打印；测试结束后请从打印机界面删除测试文件。

## Iteration 1：Gateway 监控

```powershell
uv run bambu-gateway-monitor
```

启动后会连接打印机、调用 `dump_info()`，并持续输出状态、进度、剩余时间、层数、
温度、风扇、WiFi、AMS 原始状态和错误原始字段。完整状态写入：

```text
phase0-artifacts/gateway-monitor.jsonl
```

打印机需要开启“仅局域网模式”和“开发者模式”。按 `Ctrl+C` 会停止监听并断开连接。

如果测试文件需要从 AMS 送料，启动前设置：

```powershell
$env:PRINTER_USE_AMS = "true"
$env:PRINTER_AMS_MAPPING = "[0]"
```

`PRINTER_AMS_MAPPING` 是传给打印机的原始 JSON 数组；不同 AMS 槽位请按实际切片/设备映射调整。
