import io
import tempfile
import time
import unittest
import zipfile
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from bambu_printer_gateway.database import open_database
from bambu_printer_gateway.jobs import JobStatus, QueueService
from bambu_printer_gateway.phase0 import START_GCODE
from bambu_printer_gateway.web import create_app


def sliced_3mf() -> bytes:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr(START_GCODE, "G28")
    return stream.getvalue()


class FakePrinter:
    def __init__(self, *, connected=True, state="idle"):
        self.connected = connected
        self.normalized_state = state
        self.raw_status = {"mc_percent": 0}
        self.starts = 0
        self.stops = 0

    def start(self):
        self.starts += 1
        self.connected = True

    def stop(self):
        self.stops += 1
        self.connected = False


class RetryPrinter(FakePrinter):
    def __init__(self, failures: int):
        super().__init__(connected=False, state="offline")
        self.failures = failures

    def start(self):
        self.starts += 1
        if self.starts <= self.failures:
            raise RuntimeError("printer unavailable")
        self.connected = True
        self.normalized_state = "idle"


class FakeAdapter:
    def __init__(self, printer: FakePrinter, *, exists=True, upload_error=None, confirm=True):
        self.printer = printer
        self.exists = exists
        self.upload_error = upload_error
        self.confirm = confirm
        self.uploads = []
        self.started = []

    def upload_file(self, local_path: Path, remote_path: str, timeout: int) -> None:
        self.uploads.append((local_path, remote_path, timeout))
        if self.upload_error:
            raise self.upload_error

    def file_exists(self, remote_path: str, timeout: int) -> bool:
        return self.exists

    def start_print(self, remote_name: str, remote_path: str, *, ams_slot=None) -> None:
        self.started.append((remote_name, remote_path, ams_slot))
        if self.confirm:
            self.printer.normalized_state = "printing"


class AdminStartTests(unittest.TestCase):
    @contextmanager
    def make_client(self, printer=None, adapter=None, timeout=1):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = create_app(
                database_path=root / "queue.db",
                upload_dir=root / "uploads",
                printer_service=printer,
                adapter=adapter,
                admin_username="admin",
                admin_password="secret",
                start_confirm_timeout=timeout,
                upload_timeout=1,
            )
            with TestClient(app) as client:
                yield client, root

    def auth(self):
        return ("admin", "secret")

    def post_job(self, client: TestClient, name: str = "Alice"):
        return client.post(
            "/api/jobs",
            data={"display_name": name, "project_name": f"{name} Project"},
            files={"file": (f"{name}.gcode.3mf", sliced_3mf(), "application/octet-stream")},
        )

    def start_next(self, client: TestClient, ams_slot=0):
        return client.post("/api/admin/start-next", json={"ams_slot": ams_slot}, auth=self.auth())

    def cancel_job(self, client: TestClient, job_id: str):
        return client.post(f"/api/admin/jobs/{job_id}/cancel", auth=self.auth())

    def move_job(self, client: TestClient, job_id: str, direction: str):
        return client.post(
            f"/api/admin/jobs/{job_id}/move",
            json={"direction": direction},
            auth=self.auth(),
        )

    def statuses(self, db_path: Path):
        conn = open_database(db_path)
        try:
            return [job.status for job in QueueService(conn).get_queue()]
        finally:
            conn.close()

    def job_status(self, db_path: Path, job_id: str):
        conn = open_database(db_path)
        try:
            row = conn.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)).fetchone()
            return row["status"]
        finally:
            conn.close()

    def set_job_status(self, db_path: Path, job_id: str, status: JobStatus):
        conn = open_database(db_path)
        try:
            conn.execute("UPDATE jobs SET status = ? WHERE id = ?", (status.value, job_id))
            conn.commit()
        finally:
            conn.close()

    def test_start_next_requires_auth(self):
        printer = FakePrinter()
        adapter = FakeAdapter(printer)
        with self.make_client(printer, adapter) as (client, _):
            self.assertEqual(client.post("/api/admin/start-next").status_code, 401)

    @patch("bambu_printer_gateway.web.MQTT_RECONNECT_SECONDS", 0.01)
    def test_startup_retries_printer_connection(self):
        printer = RetryPrinter(failures=1)
        adapter = FakeAdapter(printer)
        with self.make_client(printer, adapter) as (client, _):
            deadline = time.monotonic() + 1
            while time.monotonic() < deadline and not client.get("/api/status").json()["printer"]["connected"]:
                time.sleep(0.01)

            self.assertTrue(client.get("/api/status").json()["printer"]["connected"])
            self.assertEqual(printer.starts, 2)

    @patch("bambu_printer_gateway.web.MQTT_RECONNECT_SECONDS", 60)
    def test_shutdown_cancels_pending_printer_retry(self):
        printer = RetryPrinter(failures=10)
        adapter = FakeAdapter(printer)
        with self.make_client(printer, adapter) as (client, _):
            self.assertFalse(client.get("/api/status").json()["printer"]["connected"])
            self.assertEqual(printer.starts, 1)

        self.assertEqual(printer.stops, 2)

    def test_queue_management_requires_auth_and_valid_direction(self):
        with self.make_client() as (client, _):
            job_id = self.post_job(client).json()["id"]

            self.assertEqual(client.post(f"/api/admin/jobs/{job_id}/cancel").status_code, 401)
            self.assertEqual(
                client.post(f"/api/admin/jobs/{job_id}/move", json={"direction": "up"}).status_code,
                401,
            )
            self.assertEqual(self.move_job(client, job_id, "sideways").status_code, 400)

    def test_admin_moves_queued_job_before_starting(self):
        printer = FakePrinter()
        adapter = FakeAdapter(printer)
        with self.make_client(printer, adapter) as (client, _):
            first = self.post_job(client, "Alice").json()["id"]
            second = self.post_job(client, "Bob").json()["id"]

            moved = self.move_job(client, second, "up")

            self.assertEqual(moved.status_code, 200)
            self.assertEqual(moved.json()["job"]["id"], second)
            self.assertEqual(moved.json()["position"], 1)
            self.assertEqual(
                [job["id"] for job in client.get("/api/queue").json()["jobs"]],
                [second, first],
            )
            started = self.start_next(client)
            self.assertEqual(started.status_code, 200)
            self.assertEqual(started.json()["job"]["id"], second)

    def test_admin_cancels_queued_job_and_keeps_history_and_file(self):
        with self.make_client() as (client, root):
            first = self.post_job(client, "Alice").json()["id"]
            second = self.post_job(client, "Bob").json()["id"]
            conn = open_database(root / "queue.db")
            try:
                stored_path = Path(conn.execute("SELECT stored_path FROM jobs WHERE id = ?", (first,)).fetchone()[0])
            finally:
                conn.close()

            cancelled = self.cancel_job(client, first)

            self.assertEqual(cancelled.status_code, 200)
            self.assertEqual(cancelled.json()["job"]["status"], "CANCELLED")
            self.assertEqual(client.get("/api/queue").json()["jobs"], [
                {"position": 1, "id": second, "display_name": "Bob", "project_name": "Bob Project", "status": "QUEUED"}
            ])
            history = client.get("/api/admin/history", auth=self.auth()).json()["jobs"]
            self.assertEqual(history[0]["id"], first)
            self.assertEqual(history[0]["status"], "CANCELLED")
            self.assertTrue(stored_path.exists())

    def test_queue_management_rejects_unavailable_jobs(self):
        with self.make_client() as (client, root):
            job_id = self.post_job(client).json()["id"]

            self.assertEqual(self.move_job(client, job_id, "up").status_code, 409)
            self.assertEqual(self.cancel_job(client, "missing").status_code, 409)
            self.set_job_status(root / "queue.db", job_id, JobStatus.STARTING)
            self.assertEqual(self.cancel_job(client, job_id).status_code, 409)

    def test_queue_management_broadcasts_changes(self):
        with self.make_client() as (client, _):
            first = self.post_job(client, "Alice").json()["id"]
            second = self.post_job(client, "Bob").json()["id"]
            with client.websocket_connect("/ws") as websocket:
                moved = self.move_job(client, second, "up")
                move_event = websocket.receive_json()
                cancelled = self.cancel_job(client, first)
                cancel_events = [websocket.receive_json() for _ in range(2)]

            self.assertEqual(moved.status_code, 200)
            self.assertEqual(move_event, {"type": "queue.changed"})
            self.assertEqual(cancelled.status_code, 200)
            self.assertEqual(cancel_events, [{"type": "queue.changed"}, {"type": "job.changed"}])

    def test_empty_queue_returns_404(self):
        printer = FakePrinter()
        adapter = FakeAdapter(printer)
        with self.make_client(printer, adapter) as (client, _):
            response = self.start_next(client)

            self.assertEqual(response.status_code, 404)

    def test_disconnected_printer_returns_503(self):
        printer = FakePrinter(connected=False)
        printer.start = None
        adapter = FakeAdapter(printer)
        with self.make_client(printer, adapter) as (client, _):
            self.post_job(client)

            response = self.start_next(client)

            self.assertEqual(response.status_code, 503)

    def test_busy_printer_returns_409(self):
        printer = FakePrinter(state="printing")
        adapter = FakeAdapter(printer)
        with self.make_client(printer, adapter) as (client, _):
            self.post_job(client)

            response = self.start_next(client)

            self.assertEqual(response.status_code, 409)

    def test_active_job_blocks_start(self):
        printer = FakePrinter()
        adapter = FakeAdapter(printer)
        with self.make_client(printer, adapter) as (client, root):
            self.post_job(client)
            conn = open_database(root / "queue.db")
            try:
                job = QueueService(conn).get_next_job()
            finally:
                conn.close()
            self.set_job_status(root / "queue.db", job.id, JobStatus.STARTING)

            response = self.start_next(client)

            self.assertEqual(response.status_code, 409)

    def test_status_shows_reconnecting_active_job(self):
        printer = FakePrinter()
        adapter = FakeAdapter(printer)
        with self.make_client(printer, adapter) as (client, root):
            job_id = self.post_job(client).json()["id"]
            self.set_job_status(root / "queue.db", job_id, JobStatus.STARTING)
            printer.connected = False
            printer.normalized_state = "offline"

            printer_status = client.get("/api/status").json()["printer"]

            self.assertFalse(printer_status["connected"])
            self.assertEqual(printer_status["state"], "reconnecting")
            self.assertEqual(printer_status["current_job"]["id"], job_id)
            self.assertEqual(printer_status["current_job"]["status"], "STARTING")

    def test_success_starts_first_job_and_leaves_second_queued(self):
        printer = FakePrinter()
        adapter = FakeAdapter(printer)
        with self.make_client(printer, adapter) as (client, _):
            first = self.post_job(client, "Alice").json()["id"]
            self.post_job(client, "Bob")

            response = self.start_next(client)

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["job"]["id"], first)
            self.assertEqual(response.json()["job"]["status"], "PRINTING")
            self.assertEqual(client.get("/api/queue").json()["jobs"][0]["display_name"], "Bob")
            self.assertEqual(client.get("/api/status").json()["printer"]["current_job"]["id"], first)
            self.assertEqual(len(adapter.uploads), 1)
            self.assertEqual(len(adapter.started), 1)
            self.assertEqual(adapter.started[0][2], 0)
            self.assertEqual(printer.starts, 2)
            self.assertEqual(printer.stops, 1)

    def test_start_next_uses_selected_ams_slot(self):
        printer = FakePrinter()
        adapter = FakeAdapter(printer)
        with self.make_client(printer, adapter) as (client, _):
            self.post_job(client)

            response = self.start_next(client, ams_slot=2)

            self.assertEqual(response.status_code, 200)
            self.assertEqual(adapter.started[0][2], 2)

    def test_start_broadcasts_active_state_changes(self):
        printer = FakePrinter()
        adapter = FakeAdapter(printer)
        with self.make_client(printer, adapter) as (client, _):
            self.post_job(client)
            with client.websocket_connect("/ws") as websocket:
                response = self.start_next(client)
                events = [websocket.receive_json() for _ in range(5)]

            self.assertEqual(response.status_code, 200)
            self.assertEqual(
                events,
                [
                    {"type": "queue.changed"},
                    {"type": "job.changed"},
                    {"type": "job.changed"},
                    {"type": "job.changed"},
                    {"type": "queue.changed"},
                ],
            )

    def test_status_includes_ams_trays(self):
        printer = FakePrinter()
        printer.raw_status = {
            "gcode_state": "RUNNING",
            "ams": {
                "ams": [
                    {
                        "tray": [
                            {
                                "id": "0",
                                "tray_type": "PLA",
                                "tray_sub_brands": "PLA Lite",
                                "tray_color": "004EA8FF",
                                "remain": 83,
                                "tray_id_name": "A18-B1",
                            }
                        ]
                    }
                ]
            }
        }
        adapter = FakeAdapter(printer)
        with self.make_client(printer, adapter) as (client, _):
            printer_status = client.get("/api/status").json()["printer"]
            tray = printer_status["ams_trays"][0]

            self.assertEqual(printer_status["raw_state"], "RUNNING")
            self.assertEqual(tray["slot"], 0)
            self.assertEqual(tray["label"], "AMS Slot 1 - PLA Lite - A18-B1 - 83%")

    def test_admin_debug_requires_auth(self):
        printer = FakePrinter()
        adapter = FakeAdapter(printer)
        with self.make_client(printer, adapter) as (client, _):
            self.assertEqual(client.get("/api/admin/debug").status_code, 401)

    def test_admin_debug_returns_runtime_and_printer_fields(self):
        printer = FakePrinter(state="unknown")
        printer.last_seen_at = "2026-07-09T04:00:00+00:00"
        printer.raw_status = {
            "gcode_state": "SLICING",
            "print_error": 123,
            "hms": [{"code": 1}],
            "subtask_name": "queue_demo.gcode",
            "mc_percent": 7,
            "ams_status": 0,
            "ams_rfid_status": 0,
            "ams": {"ams": [{"tray": [{"id": "1", "tray_type": "PLA", "remain": 50}]}]},
        }
        adapter = FakeAdapter(printer)
        with self.make_client(printer, adapter) as (client, _):
            debug = client.get("/api/admin/debug", auth=self.auth()).json()

            self.assertIn("python", debug["runtime"])
            self.assertIn("web_module_file", debug["runtime"])
            self.assertEqual(debug["printer"]["raw_gcode_state"], "SLICING")
            self.assertEqual(debug["printer"]["current_task"], "queue_demo.gcode")
            self.assertTrue(debug["ams"]["ams_present"])
            self.assertEqual(debug["ams"]["ams_tray_count"], 1)
            self.assertEqual(debug["ams"]["ams_trays"][0]["slot"], 1)

    def test_invalid_ams_slot_returns_400_and_keeps_queue(self):
        printer = FakePrinter()
        adapter = FakeAdapter(printer)
        with self.make_client(printer, adapter) as (client, _):
            self.post_job(client)

            self.assertEqual(client.post("/api/admin/start-next", auth=self.auth()).status_code, 400)
            for body in ({}, {"ams_slot": "1"}, {"ams_slot": True}, {"ams_slot": -1}, {"ams_slot": 4}):
                response = client.post("/api/admin/start-next", json=body, auth=self.auth())
                self.assertEqual(response.status_code, 400)

            self.assertEqual(len(client.get("/api/queue").json()["jobs"]), 1)
            self.assertEqual(adapter.started, [])

    def test_finished_printer_can_start_next_job(self):
        printer = FakePrinter(state="finished")
        adapter = FakeAdapter(printer)
        with self.make_client(printer, adapter) as (client, _):
            self.post_job(client)

            response = self.start_next(client)

            self.assertEqual(response.status_code, 200)

    def test_status_marks_finished_printing_job_completed(self):
        printer = FakePrinter(state="finished")
        adapter = FakeAdapter(printer)
        with self.make_client(printer, adapter) as (client, root):
            job_id = self.post_job(client).json()["id"]
            self.set_job_status(root / "queue.db", job_id, JobStatus.PRINTING)

            printer_status = client.get("/api/status").json()["printer"]

            self.assertIsNone(printer_status["current_job"])
            self.assertEqual(self.job_status(root / "queue.db", job_id), JobStatus.COMPLETED.value)

    def test_status_marks_idle_printing_job_completed(self):
        printer = FakePrinter(state="idle")
        adapter = FakeAdapter(printer)
        with self.make_client(printer, adapter) as (client, root):
            job_id = self.post_job(client).json()["id"]
            self.set_job_status(root / "queue.db", job_id, JobStatus.PRINTING)

            client.get("/api/status")

            self.assertEqual(self.job_status(root / "queue.db", job_id), JobStatus.COMPLETED.value)

    def test_status_marks_failed_printing_job_failed(self):
        printer = FakePrinter(state="failed")
        adapter = FakeAdapter(printer)
        with self.make_client(printer, adapter) as (client, root):
            job_id = self.post_job(client).json()["id"]
            self.set_job_status(root / "queue.db", job_id, JobStatus.PRINTING)

            client.get("/api/status")

            self.assertEqual(self.job_status(root / "queue.db", job_id), JobStatus.FAILED.value)

    def test_status_does_not_complete_uploading_or_starting_jobs(self):
        for status in (JobStatus.UPLOADING, JobStatus.STARTING):
            printer = FakePrinter(state="finished")
            adapter = FakeAdapter(printer)
            with self.make_client(printer, adapter) as (client, root):
                job_id = self.post_job(client).json()["id"]
                self.set_job_status(root / "queue.db", job_id, status)

                current = client.get("/api/status").json()["printer"]["current_job"]

                self.assertEqual(self.job_status(root / "queue.db", job_id), status.value)
                self.assertEqual(current["id"], job_id)
                self.assertEqual(current["status"], status.value)

    def test_failed_printer_can_start_next_job(self):
        printer = FakePrinter(state="failed")
        adapter = FakeAdapter(printer)
        with self.make_client(printer, adapter) as (client, _):
            self.post_job(client)

            response = self.start_next(client)

            self.assertEqual(response.status_code, 200)

    def test_failed_printer_marks_active_printing_job_failed_before_start(self):
        printer = FakePrinter(state="failed")
        adapter = FakeAdapter(printer)
        with self.make_client(printer, adapter) as (client, root):
            stale = self.post_job(client, "Stale").json()["id"]
            next_job = self.post_job(client, "Next").json()["id"]
            conn = open_database(root / "queue.db")
            try:
                conn.execute("UPDATE jobs SET status = ? WHERE id = ?", (JobStatus.PRINTING.value, stale))
                conn.commit()
            finally:
                conn.close()

            response = self.start_next(client)

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["job"]["id"], next_job)
            self.assertEqual(self.job_status(root / "queue.db", stale), JobStatus.FAILED.value)
            self.assertEqual(adapter.started[0][2], 0)

    def test_finished_printer_completes_active_job_before_starting_next(self):
        printer = FakePrinter(state="finished")
        adapter = FakeAdapter(printer)
        with self.make_client(printer, adapter) as (client, root):
            stale = self.post_job(client, "Stale").json()["id"]
            next_job = self.post_job(client, "Next").json()["id"]
            self.set_job_status(root / "queue.db", stale, JobStatus.PRINTING)

            response = self.start_next(client)

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["job"]["id"], next_job)
            self.assertEqual(self.job_status(root / "queue.db", stale), JobStatus.COMPLETED.value)

    def test_upload_failure_marks_job_failed(self):
        printer = FakePrinter()
        adapter = FakeAdapter(printer, upload_error=RuntimeError("upload failed"))
        with self.make_client(printer, adapter) as (client, _):
            self.post_job(client)

            response = self.start_next(client)

            self.assertEqual(response.status_code, 502)
            self.assertEqual(client.get("/api/queue").json()["jobs"], [])

    def test_missing_remote_file_marks_job_failed(self):
        printer = FakePrinter()
        adapter = FakeAdapter(printer, exists=False)
        with self.make_client(printer, adapter) as (client, _):
            self.post_job(client)

            response = self.start_next(client)

            self.assertEqual(response.status_code, 502)
            self.assertEqual(client.get("/api/queue").json()["jobs"], [])

    def test_start_timeout_marks_job_failed(self):
        printer = FakePrinter()
        adapter = FakeAdapter(printer, confirm=False)
        with self.make_client(printer, adapter, timeout=0) as (client, _):
            self.post_job(client)

            response = self.start_next(client)

            self.assertEqual(response.status_code, 504)
            self.assertEqual(client.get("/api/queue").json()["jobs"], [])


if __name__ == "__main__":
    unittest.main()
