import io
import tempfile
import unittest
import zipfile
from contextlib import contextmanager
from pathlib import Path

from fastapi.testclient import TestClient

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


if __name__ == "__main__":
    unittest.main()
