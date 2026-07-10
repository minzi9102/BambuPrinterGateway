"""Persistent FIFO job queue."""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum


class QueueError(RuntimeError):
    """Expected queue operation failure."""


class JobStatus(StrEnum):
    QUEUED = "QUEUED"
    UPLOADING = "UPLOADING"
    STARTING = "STARTING"
    PRINTING = "PRINTING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


@dataclass(frozen=True)
class Job:
    id: str
    display_name: str
    project_name: str
    original_filename: str
    stored_filename: str
    stored_path: str
    remote_filename: str
    status: JobStatus
    queue_sequence: int
    created_at: str
    started_at: str | None = None
    finished_at: str | None = None
    error_message: str | None = None


TRANSITIONS = {
    JobStatus.QUEUED: {JobStatus.CANCELLED, JobStatus.UPLOADING},
    JobStatus.UPLOADING: {JobStatus.STARTING, JobStatus.FAILED},
    JobStatus.STARTING: {JobStatus.PRINTING, JobStatus.FAILED},
    JobStatus.PRINTING: {JobStatus.COMPLETED, JobStatus.FAILED},
    JobStatus.COMPLETED: set(),
    JobStatus.FAILED: set(),
    JobStatus.CANCELLED: set(),
}


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def row_to_job(row: sqlite3.Row) -> Job:
    return Job(
        id=row["id"],
        display_name=row["display_name"],
        project_name=row["project_name"],
        original_filename=row["original_filename"],
        stored_filename=row["stored_filename"],
        stored_path=row["stored_path"],
        remote_filename=row["remote_filename"],
        status=JobStatus(row["status"]),
        queue_sequence=row["queue_sequence"],
        created_at=row["created_at"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        error_message=row["error_message"],
    )


def coerce_status(status: JobStatus | str) -> JobStatus:
    try:
        return status if isinstance(status, JobStatus) else JobStatus(status)
    except ValueError as error:
        raise QueueError(f"unknown job status: {status}") from error


class QueueService:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def create_job(
        self,
        display_name: str,
        project_name: str,
        original_filename: str,
        stored_filename: str,
        stored_path: str,
    ) -> Job:
        job_id = uuid.uuid4().hex
        remote_filename = f"queue_{job_id[:8]}.gcode.3mf"
        created_at = now()
        with self.conn:
            sequence = self.conn.execute(
                "SELECT COALESCE(MAX(queue_sequence), 0) + 1 FROM jobs"
            ).fetchone()[0]
            self.conn.execute(
                """
                INSERT INTO jobs (
                    id, display_name, project_name, original_filename,
                    stored_filename, stored_path, remote_filename, status,
                    queue_sequence, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    display_name,
                    project_name,
                    original_filename,
                    stored_filename,
                    stored_path,
                    remote_filename,
                    JobStatus.QUEUED.value,
                    sequence,
                    created_at,
                ),
            )
        return self._get_job(job_id)

    def get_queue(self) -> list[Job]:
        rows = self.conn.execute(
            """
            SELECT *
            FROM jobs
            WHERE status = ?
            ORDER BY queue_sequence ASC
            """,
            (JobStatus.QUEUED.value,),
        ).fetchall()
        return [row_to_job(row) for row in rows]

    def get_next_job(self) -> Job | None:
        row = self.conn.execute(
            """
            SELECT *
            FROM jobs
            WHERE status = ?
            ORDER BY queue_sequence ASC
            LIMIT 1
            """,
            (JobStatus.QUEUED.value,),
        ).fetchone()
        return row_to_job(row) if row else None

    def get_active_job(self) -> Job | None:
        row = self.conn.execute(
            """
            SELECT *
            FROM jobs
            WHERE status IN (?, ?, ?)
            ORDER BY queue_sequence ASC
            LIMIT 1
            """,
            (JobStatus.UPLOADING.value, JobStatus.STARTING.value, JobStatus.PRINTING.value),
        ).fetchone()
        return row_to_job(row) if row else None

    def get_history(self) -> list[Job]:
        rows = self.conn.execute(
            """
            SELECT *
            FROM jobs
            WHERE status IN (?, ?, ?)
            ORDER BY COALESCE(finished_at, created_at) DESC, queue_sequence DESC
            LIMIT 100
            """,
            (
                JobStatus.COMPLETED.value,
                JobStatus.FAILED.value,
                JobStatus.CANCELLED.value,
            ),
        ).fetchall()
        return [row_to_job(row) for row in rows]

    def cancel_job(self, job_id: str) -> Job:
        job = self._get_job(job_id)
        if job.status != JobStatus.QUEUED:
            raise QueueError(f"only queued jobs can be cancelled: {job_id}")
        with self.conn:
            self.conn.execute(
                "UPDATE jobs SET status = ?, finished_at = ? WHERE id = ?",
                (JobStatus.CANCELLED.value, now(), job_id),
            )
        return self._get_job(job_id)

    def _get_job(self, job_id: str) -> Job:
        row = self.conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if not row:
            raise QueueError(f"job not found: {job_id}")
        return row_to_job(row)


class JobStateService:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def change_job_state(
        self,
        job_id: str,
        new_status: JobStatus | str,
        error_message: str | None = None,
    ) -> Job:
        status = coerce_status(new_status)
        job = QueueService(self.conn)._get_job(job_id)
        if status not in TRANSITIONS[job.status]:
            raise QueueError(f"invalid job state transition: {job.status} -> {status}")

        started_at = job.started_at
        finished_at = job.finished_at
        if status == JobStatus.PRINTING:
            started_at = started_at or now()
        if status in {JobStatus.COMPLETED, JobStatus.FAILED}:
            finished_at = finished_at or now()

        with self.conn:
            self.conn.execute(
                """
                UPDATE jobs
                SET status = ?, started_at = ?, finished_at = ?, error_message = ?
                WHERE id = ?
                """,
                (status.value, started_at, finished_at, error_message, job_id),
            )
        return QueueService(self.conn)._get_job(job_id)
