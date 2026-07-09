import io
import sqlite3
import tempfile
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


class WebTests(unittest.TestCase):
    @contextmanager
    def make_client(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = create_app(
                database_path=root / "queue.db",
                upload_dir=root / "uploads",
                max_upload_mb=1,
            )
            with TestClient(app) as client:
                yield client, root / "uploads"

    def post_job(self, client: TestClient, name: str = "Alice"):
        return client.post(
            "/api/jobs",
            data={"display_name": name, "project_name": f"{name} Project"},
            files={"file": (f"{name}.gcode.3mf", sliced_3mf(), "application/octet-stream")},
        )

    def test_upload_valid_file_queues_job(self):
        with self.make_client() as (client, _):
            response = self.post_job(client)

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["status"], "QUEUED")
            queue = client.get("/api/queue").json()["jobs"]
            self.assertEqual(len(queue), 1)
            self.assertEqual(queue[0]["display_name"], "Alice")
            self.assertEqual(queue[0]["position"], 1)

    def test_invalid_file_returns_400_without_job_or_file(self):
        with self.make_client() as (client, uploads):
            response = client.post(
                "/api/jobs",
                data={"display_name": "Alice", "project_name": "Bad"},
                files={"file": ("bad.gcode.3mf", b"not a zip", "application/octet-stream")},
            )

            self.assertEqual(response.status_code, 400)
            self.assertEqual(client.get("/api/queue").json()["jobs"], [])
            self.assertEqual(list(uploads.glob("*")), [])

    def test_oversized_file_returns_400_without_job_or_file(self):
        with self.make_client() as (client, uploads):
            response = client.post(
                "/api/jobs",
                data={"display_name": "Alice", "project_name": "Large"},
                files={"file": ("large.gcode.3mf", b"x" * (1024 * 1024 + 1), "application/octet-stream")},
            )

            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.json()["detail"], "Uploaded file is too large.")
            self.assertEqual(client.get("/api/queue").json()["jobs"], [])
            self.assertEqual(list(uploads.glob("*")), [])

    def test_queue_positions_are_fifo(self):
        with self.make_client() as (client, _):
            self.post_job(client, "Alice")
            self.post_job(client, "Bob")

            queue = client.get("/api/queue").json()["jobs"]

            self.assertEqual([(job["position"], job["display_name"]) for job in queue], [(1, "Alice"), (2, "Bob")])

    def test_websockets_receive_queue_changed(self):
        with self.make_client() as (client, _):
            with client.websocket_connect("/ws") as first, client.websocket_connect("/ws") as second:
                response = self.post_job(client)

                self.assertEqual(response.status_code, 200)
                self.assertEqual(first.receive_json(), {"type": "queue.changed"})
                self.assertEqual(second.receive_json(), {"type": "queue.changed"})

    def test_status_is_unknown_without_printer_connection(self):
        with self.make_client() as (client, _):
            response = client.get("/api/status")

            self.assertEqual(response.status_code, 200)
            self.assertEqual(
                response.json(),
                {"printer": {"connected": False, "state": "unknown", "raw_state": None, "ams_trays": []}},
            )

    def test_startup_marks_interrupted_startup_jobs_failed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db_path = root / "queue.db"
            conn = open_database(db_path)
            try:
                queue = QueueService(conn)
                uploading = queue.create_job("Uploading", "Project", "a.3mf", "a.3mf", "a")
                starting = queue.create_job("Starting", "Project", "b.3mf", "b.3mf", "b")
                printing = queue.create_job("Printing", "Project", "c.3mf", "c.3mf", "c")
                conn.execute("UPDATE jobs SET status = ? WHERE id = ?", (JobStatus.UPLOADING.value, uploading.id))
                conn.execute("UPDATE jobs SET status = ? WHERE id = ?", (JobStatus.STARTING.value, starting.id))
                conn.execute("UPDATE jobs SET status = ? WHERE id = ?", (JobStatus.PRINTING.value, printing.id))
                conn.commit()
            finally:
                conn.close()

            app = create_app(database_path=db_path, upload_dir=root / "uploads")
            with TestClient(app):
                pass
            conn = open_database(db_path)
            try:
                rows = {
                    row["display_name"]: (row["status"], row["error_message"])
                    for row in conn.execute("SELECT display_name, status, error_message FROM jobs")
                }
            finally:
                conn.close()

        self.assertEqual(rows["Uploading"], (JobStatus.FAILED.value, "Server restarted during job startup"))
        self.assertEqual(rows["Starting"], (JobStatus.FAILED.value, "Server restarted during job startup"))
        self.assertEqual(rows["Printing"], (JobStatus.PRINTING.value, None))

    def test_sqlite_errors_return_safe_message(self):
        with self.make_client() as (client, _):
            with patch(
                "bambu_printer_gateway.web.queue_response",
                side_effect=sqlite3.OperationalError(r"C:\secret\queue.db is locked"),
            ):
                response = client.get("/api/queue")

            self.assertEqual(response.status_code, 500)
            self.assertEqual(response.json(), {"detail": "Queue database error."})
            self.assertNotIn("secret", response.text)

    def test_public_page_contains_offline_notice(self):
        static_dir = Path(__file__).resolve().parents[1] / "src" / "bambu_printer_gateway" / "static"
        html = (static_dir / "index.html").read_text(encoding="utf-8")
        script = (static_dir / "app.js").read_text(encoding="utf-8")
        styles = (static_dir / "styles.css").read_text(encoding="utf-8")

        self.assertIn("PRINTER OFFLINE", html)
        self.assertIn("Printing is temporarily unavailable. Existing queue has been preserved.", html)
        self.assertIn("offlineNotice.hidden", script)
        self.assertIn('"offline"', script)
        self.assertIn('"unknown"', script)
        self.assertIn(".notice[hidden]", styles)


if __name__ == "__main__":
    unittest.main()
