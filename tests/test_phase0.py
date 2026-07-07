import json
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from bambu_printer_gateway.phase0 import (
    Phase0Error,
    PrinterConfig,
    START_GCODE,
    StatusRecorder,
    build_project_file_payload,
    check_printer_port,
    list_remote_files,
    publish_command,
    start_after_confirmation,
    upload_and_verify,
    upload_file,
    validate_print_file,
)


class FileValidationTests(unittest.TestCase):
    def test_rejects_missing_file(self):
        with self.assertRaisesRegex(Phase0Error, "不存在"):
            validate_print_file(Path("missing.gcode.3mf"))

    def test_rejects_corrupt_zip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "bad.gcode.3mf")
            path.write_text("not a zip", encoding="utf-8")
            with self.assertRaisesRegex(Phase0Error, "完整、可读"):
                validate_print_file(path)

    def test_rejects_archive_without_gcode(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "unsliced.gcode.3mf")
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("3D/3dmodel.model", "model")
            with self.assertRaisesRegex(Phase0Error, r"Metadata/\*\.gcode"):
                validate_print_file(path)

    def test_accepts_expected_plate_gcode(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "tiny.gcode.3mf")
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr(START_GCODE, "G28")
            self.assertEqual(validate_print_file(path), START_GCODE)


class DeviceSafetyTests(unittest.TestCase):
    def test_start_confirmation_requires_task_state_change(self):
        with tempfile.TemporaryDirectory() as directory:
            recorder = StatusRecorder(Path(directory, "states.jsonl"))
            recorder.record(SimpleNamespace(gcode_state="FINISH", subtask_name="old", gcode_file=""))
            previous = recorder.state_signature()
            recorder.record_command(
                {
                    "command": "project_file",
                    "sequence_id": "0",
                    "result": "success",
                    "reason": "success",
                    "msg": 1,
                }
            )
            recorder.record(SimpleNamespace(gcode_state="RUNNING", subtask_name="new", gcode_file=""))

            confirmed = recorder.wait_for_state_change(previous, 1)

        self.assertEqual(confirmed["gcode_state"], "RUNNING")

    def test_command_rejection_fails_without_waiting_for_timeout(self):
        with tempfile.TemporaryDirectory() as directory:
            recorder = StatusRecorder(Path(directory, "states.jsonl"))
            recorder.record(SimpleNamespace(gcode_state="FINISH", subtask_name="old", gcode_file=""))
            previous = recorder.state_signature()
            recorder.record_command(
                {
                    "command": "gcode_file",
                    "sequence_id": "0",
                    "result": "fail",
                    "reason": "bad path",
                    "msg": 1,
                }
            )

            with self.assertRaisesRegex(Phase0Error, "bad path"):
                recorder.wait_for_state_change(previous, 120)

    def test_unreachable_mqtt_port_is_reported_before_client_creation(self):
        connect = Mock(side_effect=TimeoutError)
        with self.assertRaisesRegex(Phase0Error, "host:8883"):
            check_printer_port("host", 1, connect)

    def test_upload_timeout_is_reported(self):
        run = Mock(side_effect=subprocess.TimeoutExpired("curl", 10))
        with self.assertRaisesRegex(Phase0Error, "超过 10 秒"):
            upload_file("curl", PrinterConfig("host", "secret", "serial"), Path("x"), "r", 10, run)

    def test_upload_error_redacts_access_code(self):
        run = Mock(return_value=subprocess.CompletedProcess([], 28, "", "failed secret"))
        with self.assertRaises(Phase0Error) as raised:
            upload_file("curl", PrinterConfig("host", "secret", "serial"), Path("x"), "r", 10, run)
        self.assertNotIn("secret", str(raised.exception))
        self.assertIn("***", str(raised.exception))

    def test_mqtt_publish_waits_for_delivery(self):
        mqtt_client = Mock()
        mqtt_client.is_connected.return_value = True
        result = mqtt_client.publish.return_value
        result.is_published.return_value = True

        publish_command(mqtt_client, "device/serial/request", "payload", 1)

        result.wait_for_publish.assert_called_once_with(1)
        mqtt_client.loop_stop.assert_called_once()

    def test_file_listing_decodes_utf8_independently_of_windows_locale(self):
        run = Mock(
            return_value=subprocess.CompletedProcess(
                [],
                0,
                "旧文件.gcode.3mf\nremote.gcode.3mf\n".encode(),
                b"",
            )
        )

        files = list_remote_files("curl", PrinterConfig("host", "secret", "serial"), "cache", 10, run)

        self.assertIn("remote.gcode.3mf", files)

    @patch("bambu_printer_gateway.phase0.list_remote_files", return_value=[])
    @patch("bambu_printer_gateway.phase0.upload_file")
    def test_missing_remote_file_stops_flow(self, mocked_upload, mocked_list):
        with self.assertRaisesRegex(Phase0Error, "未在打印机中找到"):
            upload_and_verify(
                "curl",
                PrinterConfig("host", "secret", "serial"),
                Path("x"),
                "cache/remote.gcode.3mf",
                10,
            )
        mocked_upload.assert_called_once()
        mocked_list.assert_called_once()

    def test_operator_cancellation_never_starts_print(self):
        client = Mock()
        with self.assertRaisesRegex(Phase0Error, "取消启动"):
            start_after_confirmation(
                client,
                "remote.gcode.3mf",
                "cache/remote.gcode.3mf",
                lambda _: "no",
            )
        client.executeClient.send_command.assert_not_called()

    def test_p1s_start_uses_cache_project_command(self):
        client = Mock()

        start_after_confirmation(
            client,
            "remote.gcode.3mf",
            "cache/remote.gcode.3mf",
            lambda _: "START remote.gcode.3mf",
        )

        payload = json.loads(client.executeClient.send_command.call_args.args[0])
        self.assertEqual(payload["print"]["command"], "project_file")
        self.assertEqual(payload["print"]["param"], START_GCODE)
        self.assertEqual(
            payload["print"]["url"],
            "file:///sdcard/cache/remote.gcode.3mf",
        )
        self.assertTrue(payload["print"]["bed_levelling"])

    def test_project_payload_can_enable_ams(self):
        payload = json.loads(
            build_project_file_payload(
                "remote.gcode.3mf",
                "cache/remote.gcode.3mf",
                use_ams=True,
                ams_mapping=[0],
            )
        )

        self.assertTrue(payload["print"]["use_ams"])
        self.assertEqual(payload["print"]["ams_mapping"], [0])

    def test_project_payload_reads_ams_environment(self):
        with patch.dict("os.environ", {"PRINTER_USE_AMS": "true", "PRINTER_AMS_MAPPING": "[1]"}):
            payload = json.loads(build_project_file_payload("remote.gcode.3mf", "cache/remote.gcode.3mf"))

        self.assertTrue(payload["print"]["use_ams"])
        self.assertEqual(payload["print"]["ams_mapping"], [1])


if __name__ == "__main__":
    unittest.main()
