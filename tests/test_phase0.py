import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import Mock, patch

from bambu_printer_gateway.phase0 import (
    Phase0Error,
    PrinterConfig,
    START_GCODE,
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

    @patch("bambu_printer_gateway.phase0.upload_file")
    def test_missing_remote_file_stops_flow(self, mocked_upload):
        client = Mock()
        client.get_files.return_value = []
        with self.assertRaisesRegex(Phase0Error, "未在打印机根目录找到"):
            upload_and_verify(
                client,
                "curl",
                PrinterConfig("host", "secret", "serial"),
                Path("x"),
                "remote.gcode.3mf",
                10,
            )
        client.start_print.assert_not_called()
        mocked_upload.assert_called_once()

    def test_operator_cancellation_never_starts_print(self):
        client = Mock()
        with self.assertRaisesRegex(Phase0Error, "取消启动"):
            start_after_confirmation(client, "remote.gcode.3mf", lambda _: "no")
        client.start_print.assert_not_called()


if __name__ == "__main__":
    unittest.main()
