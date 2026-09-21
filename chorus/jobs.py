"""SQLite-backed job store (ENGINEERING_REVIEW: no Redis/Celery — boring by
default). The whole Job is persisted as JSON; `status` is a column for queries."""
from __future__ import annotations

import sqlite3
import threading
import uuid
from pathlib import Path

from chorus.models import Job, JobStatus

DEFAULT_DB = Path(__file__).resolve().parent.parent / "chorus.db"

# A job in one of these states is still being worked on by a background task.
# After a process restart no such task can exist, so any job still here is stale.
IN_FLIGHT_STATUSES = (JobStatus.queued, JobStatus.digest_ready)


class JobStore:
    def __init__(self, db_path: Path | str = DEFAULT_DB) -> None:
        self.db_path = str(db_path)
        # check_same_thread=False: BackgroundTasks may run on a worker thread.
        # One connection is shared across those threads, so serialize access.
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock:
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS jobs "
                "(job_id TEXT PRIMARY KEY, status TEXT NOT NULL, payload TEXT NOT NULL)"
            )
            self._conn.commit()

    def create(self) -> str:
        job = Job(job_id=uuid.uuid4().hex, status=JobStatus.queued)
        self.save(job)
        return job.job_id

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT payload FROM jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
        return Job.model_validate_json(row[0]) if row else None

    def save(self, job: Job) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO jobs (job_id, status, payload) VALUES (?, ?, ?)",
                (job.job_id, job.status.value, job.model_dump_json()),
            )
            self._conn.commit()

    def fail_in_flight(self, reason: str) -> int:
        """Mark every in-flight job `failed`. Called at service startup: a job
        that was queued or mid-pipeline when the process died would otherwise
        sit non-terminal forever and a caller would poll it indefinitely.
        Returns the number of jobs marked."""
        placeholders = ",".join("?" for _ in IN_FLIGHT_STATUSES)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT payload FROM jobs WHERE status IN ({placeholders})",
                tuple(s.value for s in IN_FLIGHT_STATUSES),
            ).fetchall()
        stale = [Job.model_validate_json(r[0]) for r in rows]
        for job in stale:
            job.status = JobStatus.failed
            job.error = reason
            self.save(job)
        return len(stale)

    def close(self) -> None:
        with self._lock:
            self._conn.close()
