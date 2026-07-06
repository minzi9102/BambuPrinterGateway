# Bambu Printer Gateway

当前只实现 `Plan.md` 的 Iteration 0：真实打印链路技术 Gate。

```powershell
uv sync
$env:PRINTER_HOST = "192.168.1.101"
$env:PRINTER_ACCESS_CODE = "..."
$env:PRINTER_SERIAL = "..."
uv run bambu-phase0 .\tiny.gcode.3mf
```

命令会校验文件、监听 MQTT、上传并确认远程文件。只有操作员输入命令显示的完整
`START phase0_....gcode.3mf` 后才会启动打印。

打印机物理开始和完成时分别输入 `STARTED`、`COMPLETED`。脱敏状态记录及
`phase0-results.md` 保存在 `phase0-artifacts/`，该目录不会提交到 Git。

运行前必须确认打印板已清理。中断程序不会停止已经开始的打印；测试结束后请从打印机界面删除测试文件。
