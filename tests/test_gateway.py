import json
import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from bambu_printer_gateway.gateway import (
    BambuAdapter,
    PrinterService,
    format_status,
    main,
    normalize_printer_state,
    status_snapshot,
)
from bambu_printer_gateway.phase0 import Phase0Error, PrinterConfig


class GatewayStatusTests(unittest.TestCase):
    def test_known_state_normalizes(self):
        self.assertEqual(normalize_printer_state(SimpleNamespace(gcode_state="RUNNING")), "printing")
        self.assertEqual(normalize_printer_state(SimpleNamespace(gcode_state="FINISH")), "finished")
        self.assertEqual(normalize_printer_state(SimpleNamespace(gcode_state="PREPARE")), "starting")
        self.assertEqual(normalize_printer_state(SimpleNamespace(gcode_state="PAUSE")), "paused")
        self.assertEqual(normalize_printer_state(SimpleNamespace(gcode_state="FAILED")), "failed")

    def test_unknown_state_is_preserved_in_snapshot(self):
        status = SimpleNamespace(gcode_state="MYSTERY", mc_percent=12)
        snapshot = status_snapshot(status)

        self.assertEqual(snapshot["normalized_state"], "unknown")
        self.assertEqual(snapshot["gcode_state"], "MYSTERY")

    def test_format_status_includes_extended_fields(self):
        text = format_status(
            {
                "normalized_state": "printing",
                "gcode_state": "RUNNING",
                "mc_percent": 41,
                "mc_remaining_time": 63,
                "layer_num": 102,
                "total_layer_num": 320,
                "subtask_name": "demo",
                "gcode_file": None,
                "nozzle_temper": 220,
                "nozzle_target_temper": 220,
                "bed_temper": 60,
                "bed_target_temper": 60,
                "chamber_temper": 35,
                "cooling_fan_speed": 80,
                "heatbreak_fan_speed": 100,
                "big_fan1_speed": 0,
                "wifi_signal": "-52dBm",
                "ams_status": 0,
                "print_error": 0,
                "hms": [],
            }
        )

        self.assertIn("State: printing (raw: RUNNING)", text)
        self.assertIn("Nozzle: 220 / 220 C", text)
        self.assertIn("WiFi: -52dBm", text)


class PrinterServiceTests(unittest.TestCase):
    def test_status_update_records_once_for_duplicate_snapshot(self):
        adapter = Mock()
        outputs = []
        with tempfile.TemporaryDirectory() as directory:
            service = PrinterService(
                adapter,
                log_path=Path(directory, "gateway-monitor.jsonl"),
                output=outputs.append,
            )
            status = SimpleNamespace(gcode_state="RUNNING", mc_percent=1)

            service.on_status(status)
            service.on_status(status)

            lines = Path(directory, "gateway-monitor.jsonl").read_text(encoding="utf-8").splitlines()

        self.assertEqual(service.normalized_state, "printing")
        self.assertEqual(service.latest_status, status)
        self.assertEqual(len(outputs), 1)
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0])["mc_percent"], 1)

    def test_stop_disconnects_adapter(self):
        adapter = Mock()
        service = PrinterService(adapter)

        service.stop()

        adapter.disconnect.assert_called_once()
        self.assertFalse(service.connected)
        self.assertEqual(service.normalized_state, "offline")


class BambuAdapterTests(unittest.TestCase):
    def test_disconnect_stops_watch_and_execute_client(self):
        adapter = BambuAdapter(PrinterConfig("host", "secret", "serial"))
        client = Mock()
        adapter.client = client
        adapter._watching = True

        adapter.disconnect()

        client.stop_watch_client.assert_called_once()
        client.executeClient.disconnect.assert_called_once()
        self.assertIsNone(adapter.client)

    def test_start_print_uses_project_payload(self):
        adapter = BambuAdapter(PrinterConfig("host", "secret", "serial"))
        client = Mock()
        adapter.client = client

        adapter.start_print("remote.gcode.3mf", "cache/remote.gcode.3mf")

        payload = json.loads(client.executeClient.send_command.call_args.args[0])
        self.assertEqual(payload["print"]["command"], "project_file")
        self.assertEqual(payload["print"]["url"], "file:///sdcard/cache/remote.gcode.3mf")

    def test_start_print_can_select_ams_slot(self):
        adapter = BambuAdapter(PrinterConfig("host", "secret", "serial"))
        client = Mock()
        adapter.client = client

        adapter.start_print("remote.gcode.3mf", "cache/remote.gcode.3mf", ams_slot=1)

        payload = json.loads(client.executeClient.send_command.call_args.args[0])
        self.assertTrue(payload["print"]["use_ams"])
        self.assertEqual(payload["print"]["ams_mapping"], [1])


class GatewayCliTests(unittest.TestCase):
    def test_startup_error_redacts_access_code(self):
        with (
            patch.dict("os.environ", {"PRINTER_ACCESS_CODE": "secret"}, clear=True),
            patch("bambu_printer_gateway.gateway.run_monitor", side_effect=Phase0Error("bad secret")),
            patch("sys.argv", ["bambu-gateway"]),
            patch("sys.stdout", new_callable=io.StringIO) as stdout,
        ):
            code = main()

        self.assertEqual(code, 1)
        self.assertNotIn("secret", stdout.getvalue())
        self.assertIn("***", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
