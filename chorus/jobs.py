"""SQLite-backed job store (ENGINEERING_REVIEW: no Redis/Celery — boring by
default). The whole Job is persisted as JSON; `status` is a column for queries."""
from __future__ import annotations

import sqlite3
import uuid
from pathlib import Path

from chorus.models import Job, JobStatus

DEFAULT_DB = Path(__file__).resolve().parent.parent / "chorus.db"


class JobStore:
    def __init__(self, db_path: Path | str = DEFAULT_DB) -> None:
        self.db_path = str(db_path)
        # check_same_thread=False: BackgroundTasks may run on a worker thread.
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
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
        row = self._conn.execute(
            "SELECT payload FROM jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
        return Job.model_validate_json(row[0]) if row else None

    def save(self, job: Job) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO jobs (job_id, status, payload) VALUES (?, ?, ?)",
            (job.job_id, job.status.value, job.model_dump_json()),
        )
        self._conn.commit()
