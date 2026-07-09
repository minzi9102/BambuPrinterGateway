import io
import tempfile
import unittest
import zipfile
from contextlib import contextmanager
from pathlib import Path

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

    def start_print(self, remote_name: str, remote_path: str) -> None:
        self.started.append((remote_name, remote_path))
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

    def statuses(self, db_path: Path):
        conn = open_database(db_path)
        try:
            return [job.status for job in QueueService(conn).get_queue()]
        finally:
            conn.close()

    def test_start_next_requires_auth(self):
        printer = FakePrinter()
        adapter = FakeAdapter(printer)
        with self.make_client(printer, adapter) as (client, _):
            self.assertEqual(client.post("/api/admin/start-next").status_code, 401)

    def test_empty_queue_returns_404(self):
        printer = FakePrinter()
        adapter = FakeAdapter(printer)
        with self.make_client(printer, adapter) as (client, _):
            response = client.post("/api/admin/start-next", auth=self.auth())

            self.assertEqual(response.status_code, 404)

    def test_disconnected_printer_returns_503(self):
        printer = FakePrinter(connected=False)
        printer.start = None
        adapter = FakeAdapter(printer)
        with self.make_client(printer, adapter) as (client, _):
            self.post_job(client)

            response = client.post("/api/admin/start-next", auth=self.auth())

            self.assertEqual(response.status_code, 503)

    def test_busy_printer_returns_409(self):
        printer = FakePrinter(state="printing")
        adapter = FakeAdapter(printer)
        with self.make_client(printer, adapter) as (client, _):
            self.post_job(client)

            response = client.post("/api/admin/start-next", auth=self.auth())

            self.assertEqual(response.status_code, 409)

    def test_active_job_blocks_start(self):
        printer = FakePrinter()
        adapter = FakeAdapter(printer)
        with self.make_client(printer, adapter) as (client, root):
            self.post_job(client)
            conn = open_database(root / "queue.db")
            try:
                job = QueueService(conn).get_next_job()
                conn.execute("UPDATE jobs SET status = ? WHERE id = ?", (JobStatus.PRINTING.value, job.id))
                conn.commit()
            finally:
                conn.close()

            response = client.post("/api/admin/start-next", auth=self.auth())

            self.assertEqual(response.status_code, 409)

    def test_success_starts_first_job_and_leaves_second_queued(self):
        printer = FakePrinter()
        adapter = FakeAdapter(printer)
        with self.make_client(printer, adapter) as (client, _):
            first = self.post_job(client, "Alice").json()["id"]
            self.post_job(client, "Bob")

            response = client.post("/api/admin/start-next", auth=self.auth())

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["job"]["id"], first)
            self.assertEqual(response.json()["job"]["status"], "PRINTING")
            self.assertEqual(client.get("/api/queue").json()["jobs"][0]["display_name"], "Bob")
            self.assertEqual(client.get("/api/status").json()["printer"]["current_job"]["id"], first)
            self.assertEqual(len(adapter.uploads), 1)
            self.assertEqual(len(adapter.started), 1)
            self.assertEqual(printer.starts, 2)
            self.assertEqual(printer.stops, 1)

    def test_finished_printer_can_start_next_job(self):
        printer = FakePrinter(state="finished")
        adapter = FakeAdapter(printer)
        with self.make_client(printer, adapter) as (client, _):
            self.post_job(client)

            response = client.post("/api/admin/start-next", auth=self.auth())

            self.assertEqual(response.status_code, 200)

    def test_upload_failure_marks_job_failed(self):
        printer = FakePrinter()
        adapter = FakeAdapter(printer, upload_error=RuntimeError("upload failed"))
        with self.make_client(printer, adapter) as (client, _):
            self.post_job(client)

            response = client.post("/api/admin/start-next", auth=self.auth())

            self.assertEqual(response.status_code, 502)
            self.assertEqual(client.get("/api/queue").json()["jobs"], [])

    def test_missing_remote_file_marks_job_failed(self):
        printer = FakePrinter()
        adapter = FakeAdapter(printer, exists=False)
        with self.make_client(printer, adapter) as (client, _):
            self.post_job(client)

            response = client.post("/api/admin/start-next", auth=self.auth())

            self.assertEqual(response.status_code, 502)
            self.assertEqual(client.get("/api/queue").json()["jobs"], [])

    def test_start_timeout_marks_job_failed(self):
        printer = FakePrinter()
        adapter = FakeAdapter(printer, confirm=False)
        with self.make_client(printer, adapter, timeout=0) as (client, _):
            self.post_job(client)

            response = client.post("/api/admin/start-next", auth=self.auth())

            self.assertEqual(response.status_code, 504)
            self.assertEqual(client.get("/api/queue").json()["jobs"], [])


if __name__ == "__main__":
    unittest.main()
