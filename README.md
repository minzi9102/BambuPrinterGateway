# Bambu Printer Gateway

当前只实现 `Plan.md` 的 Iteration 0：真实打印链路技术 Gate。
Iteration 1 增加了长期运行的打印机状态监控命令，但仍不包含 Web、Queue 或数据库。

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
