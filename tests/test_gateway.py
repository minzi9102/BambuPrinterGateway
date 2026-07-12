import json
import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from bambu_printer_gateway.gateway import (
    BambuAdapter,
    MQTT_RECONNECT_SECONDS,
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
    def test_start_clears_stale_connected_event(self):
        adapter = Mock()
        service = PrinterService(adapter)
        service._connected_event.set()

        def connect_watch(_, on_connected, _on_disconnected):
            self.assertFalse(service._connected_event.is_set())
            on_connected()

        adapter.start_watch.side_effect = connect_watch

        service.start(connect_timeout=0)

        adapter.dump_info.assert_called_once()

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

    def test_status_keeps_last_ams_trays_when_next_snapshot_omits_ams(self):
        service = PrinterService(Mock(), output=lambda _: None)
        ams = {"ams": [{"tray": [{"id": "0", "tray_type": "PLA"}]}]}

        service.on_status(SimpleNamespace(gcode_state="IDLE", mc_percent=0, ams=ams, ams_status=0))
        service.on_status(SimpleNamespace(gcode_state="RUNNING", mc_percent=1))

        self.assertEqual(service.normalized_state, "printing")
        self.assertEqual(service.raw_status["ams"], ams)
        self.assertEqual(service.raw_status["ams_status"], 0)

    def test_stop_disconnects_adapter(self):
        adapter = Mock()
        service = PrinterService(adapter)

        service.stop()

        adapter.disconnect.assert_called_once()
        self.assertFalse(service.connected)
        self.assertEqual(service.normalized_state, "offline")

    def test_connection_callbacks_update_service_state(self):
        service = PrinterService(Mock())

        service.on_connected()

        self.assertTrue(service.connected)
        self.assertEqual(service.normalized_state, "unknown")
        self.assertTrue(service._connected_event.is_set())
        service.on_disconnected()
        self.assertFalse(service.connected)
        self.assertEqual(service.normalized_state, "offline")
        self.assertFalse(service._connected_event.is_set())


class BambuAdapterTests(unittest.TestCase):
    def test_watch_uses_fixed_reconnect_delay_and_connection_callbacks(self):
        adapter = BambuAdapter(PrinterConfig("host", "secret", "serial"))
        client = Mock()
        mqtt_client = client.watchClient.client
        watch_on_connect = Mock()
        mqtt_client.on_connect = watch_on_connect
        connected = Mock()
        disconnected = Mock()
        adapter.client = client

        adapter.start_watch(Mock(), connected, disconnected)

        mqtt_client.reconnect_delay_set.assert_called_once_with(MQTT_RECONNECT_SECONDS, MQTT_RECONNECT_SECONDS)
        mqtt_client.on_connect(mqtt_client, None, None, 1)
        watch_on_connect.assert_not_called()
        mqtt_client.on_connect(mqtt_client, None, None, 0)
        watch_on_connect.assert_called_once_with(mqtt_client, None, None, 0)
        mqtt_client.on_disconnect(mqtt_client, None, 1)
        disconnected.assert_called_once()

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
