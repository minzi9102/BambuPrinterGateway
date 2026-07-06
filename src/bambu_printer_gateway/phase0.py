"""Interactive Iteration 0 hardware gate."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import threading
import time
import uuid
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable

from bambu_connect import BambuClient

START_GCODE = "Metadata/plate_1.gcode"
STATUS_FIELDS = (
    "gcode_state",
    "subtask_name",
    "gcode_file",
    "mc_percent",
    "mc_remaining_time",
    "layer_num",
    "total_layer_num",
)


class Phase0Error(RuntimeError):
    """Expected validation or device-probe failure."""


@dataclass(frozen=True)
class PrinterConfig:
    host: str
    access_code: str
    serial: str

    @classmethod
    def from_env(cls) -> PrinterConfig:
        names = ("PRINTER_HOST", "PRINTER_ACCESS_CODE", "PRINTER_SERIAL")
        values = {name: os.environ.get(name, "").strip() for name in names}
        missing = [name for name, value in values.items() if not value]
        if missing:
            raise Phase0Error(f"缺少环境变量：{', '.join(missing)}")
        return cls(values["PRINTER_HOST"], values["PRINTER_ACCESS_CODE"], values["PRINTER_SERIAL"])


class StatusRecorder:
    def __init__(self, path: Path):
        self.path = path
        self.command_path = path.with_name(f"{path.stem}-commands.jsonl")
        self.received = threading.Event()
        self._last: dict[str, object] | None = None
        self._last_command: tuple[object, ...] | None = None
        self._command_failure: str | None = None
        self._lock = threading.Lock()
        self._changed = threading.Condition(self._lock)

    def record(self, status: object) -> None:
        state = {field: getattr(status, field, None) for field in STATUS_FIELDS}
        with self._changed:
            if state == self._last:
                return
            self._last = state
            entry = {"timestamp": now(), **state}
            with self.path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(entry, ensure_ascii=False) + "\n")
            self._changed.notify_all()
        self.received.set()
        print(
            f"[{entry['timestamp']}] state={state['gcode_state']} "
            f"progress={state['mc_percent']} layer={state['layer_num']}/{state['total_layer_num']}"
        )

    def record_command(self, values: dict[str, object]) -> None:
        if values.get("command") not in {"gcode_file", "project_file"}:
            return
        fields = ("command", "sequence_id", "result", "reason", "msg", "fail_reason")
        signature = tuple(values.get(field) for field in fields)
        with self._changed:
            if signature == self._last_command:
                return
            self._last_command = signature
            entry = {"timestamp": now(), **{field: values.get(field) for field in fields}}
            with self.command_path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(entry, ensure_ascii=False) + "\n")
            result = str(values.get("result") or "").lower()
            reason = str(values.get("reason") or values.get("fail_reason") or "")
            failed = result not in {"success", "ok"} if result else reason.lower() not in {"", "success", "ok"}
            if failed:
                self._command_failure = str(
                    reason or f"result={result or 'unknown'}, msg={values.get('msg')}"
                )
            self._changed.notify_all()
        print(
            f"MQTT 命令响应：result={entry['result']} reason={entry['reason']} msg={entry['msg']}"
        )

    def state_signature(self) -> object:
        with self._lock:
            return self._state_signature()

    def wait_for_state_change(
        self,
        previous: object,
        timeout: int,
    ) -> dict[str, object]:
        deadline = time.monotonic() + timeout
        with self._changed:
            while self._state_signature() == previous:
                if self._command_failure:
                    raise Phase0Error(f"打印机拒绝启动命令：{self._command_failure}")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise Phase0Error(
                        f"启动命令已发送，但 {timeout} 秒内未收到任务状态变化；"
                        "请检查打印机，确认未启动后再重试"
                    )
                self._changed.wait(remaining)
            return dict(self._last or {})

    def _state_signature(self) -> object:
        return None if self._last is None else self._last["gcode_state"]


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def validate_print_file(path: Path) -> str:
    if not path.is_file():
        raise Phase0Error(f"打印文件不存在：{path}")
    if not path.name.lower().endswith(".gcode.3mf"):
        raise Phase0Error("打印文件必须使用 .gcode.3mf 扩展名")
    try:
        with zipfile.ZipFile(path) as archive:
            bad_member = archive.testzip()
            if bad_member:
                raise Phase0Error(f"3MF 内部文件损坏：{bad_member}")
            entries = [
                name
                for name in archive.namelist()
                if Path(name).as_posix().startswith("Metadata/") and name.lower().endswith(".gcode")
            ]
    except (OSError, zipfile.BadZipFile) as error:
        raise Phase0Error("打印文件不是完整、可读的 3MF/ZIP") from error
    if not entries:
        raise Phase0Error("3MF 不包含 Metadata/*.gcode")
    if START_GCODE not in entries:
        raise Phase0Error(f"上游 start_print() 固定需要 {START_GCODE}")
    return START_GCODE


def find_curl(run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run) -> str:
    executable = shutil.which("curl")
    if not executable:
        raise Phase0Error("未找到 curl")
    try:
        result = run(
            [executable, "--version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise Phase0Error("无法执行 curl --version") from error
    if result.returncode or "ftps" not in result.stdout.lower().split("protocols:", 1)[-1].split():
        raise Phase0Error("当前 curl 不支持 FTPS")
    return executable


def redact(text: str, secret: str) -> str:
    return text.replace(secret, "***") if secret else text


def check_printer_port(
    host: str,
    timeout: int,
    connect: Callable[..., socket.socket] = socket.create_connection,
) -> None:
    try:
        with connect((host, 8883), timeout=timeout):
            pass
    except OSError as error:
        raise Phase0Error(
            f"无法连接打印机 {host}:8883；请检查 PRINTER_HOST、局域网、LAN Mode 和防火墙"
        ) from error


def publish_command(mqtt_client: object, topic: str, payload: str, timeout: int) -> None:
    mqtt_client.loop_start()
    try:
        deadline = time.monotonic() + timeout
        while not mqtt_client.is_connected():
            if time.monotonic() >= deadline:
                raise Phase0Error(f"MQTT 命令连接超过 {timeout} 秒")
            time.sleep(0.05)
        result = mqtt_client.publish(topic, payload)
        result.wait_for_publish(timeout)
        if not result.is_published():
            raise Phase0Error(f"MQTT 命令发送超过 {timeout} 秒")
    except (OSError, RuntimeError, ValueError) as error:
        raise Phase0Error(f"MQTT 命令发送失败：{error}") from error
    finally:
        mqtt_client.loop_stop()


def enable_confirmed_commands(client: BambuClient, timeout: int) -> None:
    execute = client.executeClient
    topic = f"device/{execute.serial}/request"
    execute.send_command = lambda payload: publish_command(execute.client, topic, payload, timeout)


def upload_file(
    curl: str,
    config: PrinterConfig,
    local_path: Path,
    remote_name: str,
    timeout: int,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> None:
    command = [
        curl,
        "--fail",
        "--silent",
        "--show-error",
        "--ftp-pasv",
        "--insecure",
        "--connect-timeout",
        "10",
        "--max-time",
        str(timeout),
        "--upload-file",
        str(local_path),
        f"ftps://{config.host}/{remote_name}",
        "--user",
        f"bblp:{config.access_code}",
    ]
    try:
        result = run(command, capture_output=True, text=True, timeout=timeout + 5)
    except subprocess.TimeoutExpired as error:
        raise Phase0Error(f"FTPS 上传超过 {timeout} 秒") from error
    except OSError as error:
        raise Phase0Error("无法启动 curl 上传") from error
    if result.returncode:
        detail = redact(result.stderr.strip(), config.access_code)
        raise Phase0Error(f"FTPS 上传失败（curl {result.returncode}）：{detail or '无错误详情'}")


def list_remote_files(
    curl: str,
    config: PrinterConfig,
    remote_dir: str,
    timeout: int,
    run: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run,
) -> list[str]:
    command = [
        curl,
        "--fail",
        "--silent",
        "--show-error",
        "--ftp-pasv",
        "--insecure",
        "--list-only",
        f"ftps://{config.host}/{remote_dir.strip('/')}/",
        "--user",
        f"bblp:{config.access_code}",
    ]
    try:
        result = run(command, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired as error:
        raise Phase0Error(f"FTPS 文件列表超过 {timeout} 秒") from error
    except OSError as error:
        raise Phase0Error("无法启动 curl 获取文件列表") from error
    if result.returncode:
        detail = redact(result.stderr.decode("utf-8", errors="replace").strip(), config.access_code)
        raise Phase0Error(f"FTPS 文件列表失败（curl {result.returncode}）：{detail or '无错误详情'}")
    return result.stdout.decode("utf-8", errors="replace").splitlines()


def upload_and_verify(
    curl: str,
    config: PrinterConfig,
    local_path: Path,
    remote_path: str,
    timeout: int,
) -> None:
    upload_file(curl, config, local_path, remote_path, timeout)
    remote = Path(remote_path)
    if remote.name not in list_remote_files(curl, config, remote.parent.as_posix(), timeout):
        raise Phase0Error(f"上传后未在打印机中找到 {remote_path}")


def start_after_confirmation(
    client: BambuClient,
    remote_name: str,
    remote_path: str,
    input_fn: Callable[[str], str] = input,
) -> None:
    expected = f"START {remote_name}"
    if input_fn(f"确认打印板已清理，输入“{expected}”启动打印：").strip() != expected:
        raise Phase0Error("操作员取消启动；文件已上传，但未发送打印命令")
    client.executeClient.send_command(
        json.dumps(
            {
                "print": {
                    "sequence_id": "0",
                    "command": "project_file",
                    "param": START_GCODE,
                    "project_id": "0",
                    "profile_id": "0",
                    "task_id": "0",
                    "subtask_id": "0",
                    "subtask_name": remote_name.removesuffix(".3mf"),
                    "file": "",
                    "url": f"file:///sdcard/{remote_path}",
                    "md5": "",
                    "timelapse": False,
                    "bed_type": "auto",
                    "bed_levelling": True,
                    "flow_cali": False,
                    "vibration_cali": True,
                    "layer_inspect": True,
                    "ams_mapping": [],
                    "use_ams": False,
                }
            }
        )
    )


def wait_for_operator(label: str, input_fn: Callable[[str], str] = input) -> str:
    while True:
        answer = input_fn(f"物理确认后输入 {label}（或 ABORT 停止记录）：").strip().upper()
        if answer == label:
            return now()
        if answer == "ABORT":
            raise Phase0Error("操作员中止硬件 Gate")


def write_report(
    report_path: Path,
    log_path: Path,
    local_name: str,
    remote_name: str,
    gcode_entry: str,
    started_at: str,
    completed_at: str,
) -> None:
    states = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    rows = [
        "| {timestamp} | {gcode_state} | {mc_percent} | {mc_remaining_time} | "
        "{layer_num}/{total_layer_num} |".format_map(
            {key: "" if value is None else str(value).replace("|", "\\|") for key, value in state.items()}
        )
        for state in states
    ]
    report_path.write_text(
        "\n".join(
            [
                "# Phase 0 Hardware Gate",
                "",
                f"- Generated: {now()}",
                f"- Local file: `{local_name}`",
                f"- Remote file: `{remote_name}`",
                f"- G-code entry: `{gcode_entry}`",
                "- Upload verified: yes",
                f"- Physical print started: {started_at}",
                f"- Physical print completed: {completed_at}",
                "",
                "| Timestamp (UTC) | State | Progress | Remaining (min) | Layer |",
                "| --- | --- | ---: | ---: | ---: |",
                *rows,
                "",
                "Result: PASS — operator confirmed upload, physical start, and physical completion.",
                "",
                "Cleanup: delete the uploaded test file from the printer UI.",
            ]
        ),
        encoding="utf-8",
    )


def run_gate(args: argparse.Namespace, input_fn: Callable[[str], str] = input) -> Path:
    local_path = Path(args.file).expanduser().resolve()
    gcode_entry = validate_print_file(local_path)
    curl = find_curl()
    config = PrinterConfig.from_env()
    check_printer_port(config.host, args.connect_timeout)
    remote_name = f"phase0_{uuid.uuid4().hex[:8]}.gcode.3mf"
    remote_path = f"cache/{remote_name}"
    artifact_dir = Path(args.artifacts_dir)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    log_path = artifact_dir / f"{datetime.now():%Y%m%d-%H%M%S}-{remote_name}.jsonl"
    report_path = artifact_dir / "phase0-results.md"
    recorder = StatusRecorder(log_path)
    connected = threading.Event()
    client: BambuClient | None = None
    watch_started = False

    print(f"验证通过：{gcode_entry}")
    print(f"状态证据：{log_path}")
    print(f"命令响应证据：{recorder.command_path}")
    try:
        client = BambuClient(config.host, config.access_code, config.serial)
        enable_confirmed_commands(client, args.command_timeout)

        def record_status(status: object) -> None:
            recorder.record(status)
            recorder.record_command(client.watchClient.values)

        client.start_watch_client(record_status, connected.set)
        watch_started = True
        if not connected.wait(args.connect_timeout):
            raise Phase0Error("MQTT 连接超时")
        client.dump_info()
        if not recorder.received.wait(args.connect_timeout):
            raise Phase0Error("MQTT 已连接，但未收到打印机状态")
        upload_and_verify(curl, config, local_path, remote_path, args.upload_timeout)
        print(f"上传并确认成功：{remote_path}")
        previous_state = recorder.state_signature()
        start_after_confirmation(client, remote_name, remote_path, input_fn)
        confirmed = recorder.wait_for_state_change(previous_state, args.start_timeout)
        print(
            f"MQTT 已确认任务状态变化：state={confirmed['gcode_state']} "
            f"task={confirmed['subtask_name']}"
        )
        started_at = wait_for_operator("STARTED", input_fn)
        completed_at = wait_for_operator("COMPLETED", input_fn)
        write_report(
            report_path,
            log_path,
            local_path.name,
            remote_name,
            gcode_entry,
            started_at,
            completed_at,
        )
        return report_path
    finally:
        if client and watch_started:
            client.stop_watch_client()
        if client:
            client.executeClient.disconnect()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="运行 Bambu 打印机第一阶段真实硬件 Gate")
    parser.add_argument("file", help="小型、已切片的 .gcode.3mf 文件")
    parser.add_argument("--artifacts-dir", default="phase0-artifacts")
    parser.add_argument("--connect-timeout", type=int, default=30)
    parser.add_argument("--command-timeout", type=int, default=10)
    parser.add_argument("--start-timeout", type=int, default=120)
    parser.add_argument("--upload-timeout", type=int, default=600)
    return parser


def main() -> int:
    try:
        report = run_gate(build_parser().parse_args())
    except KeyboardInterrupt:
        print("\n已停止状态记录；打印机上的任务不会被自动取消。")
        return 130
    except Phase0Error as error:
        print(f"Gate 失败：{error}")
        return 1
    print(f"Gate 通过：{report}")
    return 0
