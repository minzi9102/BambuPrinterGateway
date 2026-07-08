import tempfile
import unittest
from pathlib import Path

from bambu_printer_gateway.database import open_database
from bambu_printer_gateway.jobs import JobStateService, JobStatus, QueueError, QueueService


class QueueTests(unittest.TestCase):
    def open_queue(self, path: Path):
        conn = open_database(path)
        return conn, QueueService(conn), JobStateService(conn)

    def create_job(self, queue: QueueService, name: str):
        return queue.create_job(
            name,
            f"{name} project",
            f"{name}.gcode.3mf",
            f"{name}.gcode.3mf",
            f"uploads/{name}.gcode.3mf",
        )

    def test_three_jobs_keep_fifo_order(self):
        with tempfile.TemporaryDirectory() as directory:
            conn, queue, _ = self.open_queue(Path(directory, "queue.db"))
            first = self.create_job(queue, "alice")
            second = self.create_job(queue, "bob")
            third = self.create_job(queue, "charlie")

            self.assertEqual([job.id for job in queue.get_queue()], [first.id, second.id, third.id])
            self.assertEqual(queue.get_next_job().id, first.id)
            conn.close()

    def test_cancel_middle_job_keeps_remaining_order(self):
        with tempfile.TemporaryDirectory() as directory:
            conn, queue, _ = self.open_queue(Path(directory, "queue.db"))
            first = self.create_job(queue, "alice")
            second = self.create_job(queue, "bob")
            third = self.create_job(queue, "charlie")

            cancelled = queue.cancel_job(second.id)

            self.assertEqual(cancelled.status, JobStatus.CANCELLED)
            self.assertEqual([job.id for job in queue.get_queue()], [first.id, third.id])
            conn.close()

    def test_completed_job_does_not_reenter_queue(self):
        with tempfile.TemporaryDirectory() as directory:
            conn, queue, states = self.open_queue(Path(directory, "queue.db"))
            job = self.create_job(queue, "alice")

            states.change_job_state(job.id, JobStatus.UPLOADING)
            states.change_job_state(job.id, JobStatus.STARTING)
            states.change_job_state(job.id, JobStatus.PRINTING)
            completed = states.change_job_state(job.id, JobStatus.COMPLETED)

            self.assertEqual(completed.status, JobStatus.COMPLETED)
            self.assertEqual(queue.get_queue(), [])
            self.assertIsNotNone(completed.started_at)
            self.assertIsNotNone(completed.finished_at)
            conn.close()

    def test_failed_job_does_not_enter_queue(self):
        with tempfile.TemporaryDirectory() as directory:
            conn, queue, states = self.open_queue(Path(directory, "queue.db"))
            job = self.create_job(queue, "alice")

            states.change_job_state(job.id, JobStatus.UPLOADING)
            failed = states.change_job_state(job.id, JobStatus.FAILED, "upload failed")

            self.assertEqual(failed.status, JobStatus.FAILED)
            self.assertEqual(failed.error_message, "upload failed")
            self.assertEqual(queue.get_queue(), [])
            self.assertIsNotNone(failed.finished_at)
            conn.close()

    def test_queue_survives_reopen(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "queue.db")
            conn, queue, _ = self.open_queue(path)
            first = self.create_job(queue, "alice")
            second = self.create_job(queue, "bob")
            third = self.create_job(queue, "charlie")
            conn.close()

            conn, queue, _ = self.open_queue(path)

            self.assertEqual([job.id for job in queue.get_queue()], [first.id, second.id, third.id])
            conn.close()

    def test_invalid_transition_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            conn, queue, states = self.open_queue(Path(directory, "queue.db"))
            job = self.create_job(queue, "alice")

            with self.assertRaises(QueueError):
                states.change_job_state(job.id, JobStatus.COMPLETED)

            conn.close()

    def test_cancel_non_queued_job_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            conn, queue, states = self.open_queue(Path(directory, "queue.db"))
            job = self.create_job(queue, "alice")
            states.change_job_state(job.id, JobStatus.UPLOADING)

            with self.assertRaises(QueueError):
                queue.cancel_job(job.id)

            conn.close()


if __name__ == "__main__":
    unittest.main()
