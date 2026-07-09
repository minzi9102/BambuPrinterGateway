"""Long-running Iteration 1 printer gateway monitor."""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

from bambu_connect import BambuClient

from .phase0 import (
    Phase0Error,
    PrinterConfig,
    build_project_file_payload,
    check_printer_port,
    enable_confirmed_commands,
    find_curl,
    list_remote_files,
    redact,
    upload_file,
)

MONITORED_FIELDS = (
    "gcode_state",
    "mc_print_stage",
    "mc_print_sub_stage",
    "mc_percent",
    "mc_remaining_time",
    "layer_num",
    "total_layer_num",
    "gcode_file_prepare_percent",
    "mc_print_line_number",
    "project_id",
    "profile_id",
    "task_id",
    "subtask_id",
    "subtask_name",
    "gcode_file",
    "print_type",
    "nozzle_temper",
    "nozzle_target_temper",
    "bed_temper",
    "bed_target_temper",
    "chamber_temper",
    "heatbreak_fan_speed",
    "cooling_fan_speed",
    "big_fan1_speed",
    "big_fan2_speed",
    "fan_gear",
    "spd_mag",
    "spd_lvl",
    "wifi_signal",
    "print_error",
    "lifecycle",
    "sdcard",
    "home_flag",
    "hw_switch_state",
    "hms",
    "ams_status",
    "ams_rfid_status",
    "ams",
    "vt_tray",
    "lights_report",
    "online",
    "ipcam",
    "upgrade_state",
    "force_upgrade",
)

STATE_MAP = {
    "FAILED": "failed",
    "FINISH": "finished",
    "IDLE": "idle",
    "PAUSE": "paused",
    "PREPARE": "starting",
    "RUNNING": "printing",
}


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def normalize_printer_state(status: object | None) -> str:
    raw = str(getattr(status, "gcode_state", "") or "").upper()
    return STATE_MAP.get(raw, "unknown")


def jsonable(value: Any) -> Any:
    if dataclasses.is_dataclass(value):
        return {key: jsonable(item) for key, item in dataclasses.asdict(value).items()}
    if isinstance(value, dict):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(item) for item in value]
    return value


def status_snapshot(status: object) -> dict[str, Any]:
    return {
        "timestamp": now(),
        "normalized_state": normalize_printer_state(status),
        **{field: jsonable(getattr(status, field, None)) for field in MONITORED_FIELDS},
    }


def has_ams_trays(snapshot: dict[str, Any]) -> bool:
    ams_units = ((snapshot.get("ams") or {}).get("ams") or [])
    return bool(ams_units and (ams_units[0].get("tray") or []))


class BambuAdapter:
    def __init__(
        self,
        config: PrinterConfig,
        *,
        curl: str | None = None,
        command_timeout: int = 10,
        connect_timeout: int = 30,
    ):
        self.config = config
        self.curl = curl
        self.command_timeout = command_timeout
        self.connect_timeout = connect_timeout
        self.client: BambuClient | None = None
        self._watching = False

    def connect(self) -> None:
        check_printer_port(self.config.host, self.connect_timeout)
        self.client = BambuClient(self.config.host, self.config.access_code, self.config.serial)
        enable_confirmed_commands(self.client, self.command_timeout)

    def start_watch(
        self,
        message_callback: Callable[[object], None],
        on_connect_callback: Callable[[], None],
    ) -> None:
        if not self.client:
            raise Phase0Error("打印机客户端尚未连接")
        self.client.start_watch_client(message_callback, on_connect_callback)
        self._watching = True

    def dump_info(self) -> None:
        if not self.client:
            raise Phase0Error("打印机客户端尚未连接")
        self.client.dump_info()

    def upload_file(self, local_path: Path, remote_path: str, timeout: int) -> None:
        upload_file(self.curl or find_curl(), self.config, local_path, remote_path, timeout)

    def file_exists(self, remote_path: str, timeout: int) -> bool:
        remote = Path(remote_path)
        files = list_remote_files(self.curl or find_curl(), self.config, remote.parent.as_posix(), timeout)
        return remote.name in files

    def start_print(self, remote_name: str, remote_path: str, *, ams_slot: int | None = None) -> None:
        if not self.client:
            raise Phase0Error("打印机客户端尚未连接")
        kwargs = {} if ams_slot is None else {"use_ams": True, "ams_mapping": [ams_slot]}
        self.client.executeClient.send_command(build_project_file_payload(remote_name, remote_path, **kwargs))

    def disconnect(self) -> None:
        if not self.client:
            return
        if self._watching:
            self.client.stop_watch_client()
            self._watching = False
        self.client.executeClient.disconnect()
        self.client = None


class PrinterService:
    def __init__(
        self,
        adapter: BambuAdapter,
        *,
        log_path: Path | None = None,
        output: Callable[[str], None] = print,
    ):
        self.adapter = adapter
        self.log_path = log_path
        self.output = output
        self.connected = False
        self.last_seen_at: str | None = None
        self.raw_status: dict[str, Any] | None = None
        self.normalized_state = "offline"
        self.latest_status: object | None = None
        self._last_signature: str | None = None
        self._connected_event = threading.Event()

    def start(self, connect_timeout: int = 30) -> None:
        self.adapter.connect()
        self.adapter.start_watch(self.on_status, self.on_connected)
        if not self._connected_event.wait(connect_timeout):
            raise Phase0Error("MQTT 连接超时")
        self.adapter.dump_info()
        self.output("Printer connected")

    def stop(self) -> None:
        self.adapter.disconnect()
        self.connected = False
        self.normalized_state = "offline"

    def on_connected(self) -> None:
        self.connected = True
        self._connected_event.set()

    def on_status(self, status: object) -> None:
        snapshot = status_snapshot(status)
        if not has_ams_trays(snapshot) and self.raw_status and has_ams_trays(self.raw_status):
            for field in ("ams", "ams_status", "ams_rfid_status", "vt_tray"):
                snapshot[field] = self.raw_status.get(field)
        signature = json.dumps(
            {key: value for key, value in snapshot.items() if key != "timestamp"},
            ensure_ascii=False,
            sort_keys=True,
        )
        if signature == self._last_signature:
            return
        self._last_signature = signature
        self.latest_status = status
        self.last_seen_at = snapshot["timestamp"]
        self.raw_status = snapshot
        self.normalized_state = snapshot["normalized_state"]
        if self.log_path:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(snapshot, ensure_ascii=False, sort_keys=True) + "\n")
        self.output(format_status(snapshot))


def format_status(status: dict[str, Any]) -> str:
    return "\n".join(
        [
            "",
            f"State: {status['normalized_state']} (raw: {status.get('gcode_state')})",
            f"Progress: {status.get('mc_percent')}%",
            f"Remaining: {status.get('mc_remaining_time')} min",
            f"Layer: {status.get('layer_num')} / {status.get('total_layer_num')}",
            f"Task: {status.get('subtask_name') or status.get('gcode_file')}",
            f"Nozzle: {status.get('nozzle_temper')} / {status.get('nozzle_target_temper')} C",
            f"Bed: {status.get('bed_temper')} / {status.get('bed_target_temper')} C",
            f"Chamber: {status.get('chamber_temper')} C",
            "Fans: "
            f"cooling={status.get('cooling_fan_speed')} "
            f"heatbreak={status.get('heatbreak_fan_speed')} "
            f"aux={status.get('big_fan1_speed')}",
            f"WiFi: {status.get('wifi_signal')}",
            f"AMS: raw status={status.get('ams_status')}",
            f"Errors: print_error={status.get('print_error')} hms={status.get('hms')}",
        ]
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="持续监控 Bambu 打印机 Gateway 状态")
    parser.add_argument("--artifacts-dir", default="phase0-artifacts")
    parser.add_argument("--connect-timeout", type=int, default=30)
    parser.add_argument("--command-timeout", type=int, default=10)
    return parser


def run_monitor(args: argparse.Namespace) -> None:
    config = PrinterConfig.from_env()
    log_path = Path(args.artifacts_dir) / "gateway-monitor.jsonl"
    adapter = BambuAdapter(config, command_timeout=args.command_timeout, connect_timeout=args.connect_timeout)
    service = PrinterService(adapter, log_path=log_path)
    service.start(args.connect_timeout)
    print(f"状态记录：{log_path}")
    try:
        while True:
            time.sleep(1)
    finally:
        service.stop()


def main() -> int:
    try:
        run_monitor(build_parser().parse_args())
    except KeyboardInterrupt:
        print("\n已停止 Gateway 监控。")
        return 130
    except Phase0Error as error:
        print(f"Gateway 启动失败：{redact(str(error), os.environ.get('PRINTER_ACCESS_CODE', ''))}")
        return 1
    return 0
