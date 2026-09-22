"""Job store: `JobStore` is the Protocol every backend implements (ENGINEERING_
REVIEW: no Redis/Celery — boring by default). `SqliteJobStore` is the local/dev/
test implementation (the whole Job persisted as JSON; `status`/`owner`/
`created_at`/`updated_at` are columns for queries). `chorus.stores.postgres.
PostgresJobStore` is the Vercel implementation, selected by `chorus.config_env`
when `DATABASE_URL` is set — same Protocol, same column layout, same
`fail_in_flight`/`save` conditional-write semantics.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import threading
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol, runtime_checkable

from chorus.models import Job, JobStatus

log = logging.getLogger("chorus.jobs")

# Repository-root default, used only when CHORUS_DB_PATH is unset (local dev,
# tests). R1: in Postgres mode (DATABASE_URL set) nothing should ever touch
# this path — chorus.config_env.build_deps/select_* branch before
# construction rather than building a Sqlite* store and discarding it.
_REPO_ROOT_DB = Path(__file__).resolve().parent.parent / "chorus.db"
CHORUS_DB_PATH_ENV = "CHORUS_DB_PATH"


def _default_db() -> Path:
    """`CHORUS_DB_PATH`, when set, overrides the repository-root default —
    e.g. pointed at `/tmp` on a read-only deployment filesystem that still
    wants a local SQLite fallback rather than the repo root (Vercel Functions
    expose a writable filesystem only under `/tmp`)."""
    override = os.environ.get(CHORUS_DB_PATH_ENV)
    return Path(override) if override else _REPO_ROOT_DB


# Evaluated once at import time, matching every previous caller's expectation
# that DEFAULT_DB is a Path constant; chorus.jobs is imported after
# chorus.config.load_env() in every real entrypoint, so a .env.local-set
# CHORUS_DB_PATH is visible here in practice as well as in tests that set the
# env var before importing this module.
DEFAULT_DB = _default_db()

# "master" — the master CHORUS_API_TOKEN's owner value, and the default owner
# recorded on a job created before ownership existed (chorus.models.Job.owner
# duplicates this same literal to avoid a models->jobs import cycle).
# chorus.subscriptions re-exports this name for backward compatibility.
MASTER_OWNER = "master"

# A job in one of these states is still being worked on by a background task.
# After a process restart no such task can exist, so any job still here is stale.
IN_FLIGHT_STATUSES = (JobStatus.queued, JobStatus.digest_ready)
TERMINAL_STATUSES = (JobStatus.done, JobStatus.failed)

# Epoch sentinel used to backfill `updated_at` for rows that predate the
# column (ALTER TABLE ... ADD COLUMN with no default-aware backfill): treated
# as "ancient", so a pre-existing in-flight job is still eligible for the
# very first sweep after this migration runs, rather than silently pinned as
# perpetually fresh.
_EPOCH = "1970-01-01T00:00:00+00:00"


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


@runtime_checkable
class JobStore(Protocol):
    def create(self, owner: str = MASTER_OWNER) -> str: ...

    def get(self, job_id: str) -> Job | None: ...

    def save(self, job: Job) -> None: ...

    def fail_in_flight(self, reason: str, older_than_seconds: float = 0) -> int: ...

    def count_for_owner(self, owner: str, since: datetime) -> int:
        """Jobs `owner` created at or after `since` (rolling-window spend
        quota, R3) — irrespective of current status."""
        ...

    def count_in_flight(self, owner: str) -> int:
        """`owner`'s jobs currently queued or digest_ready (concurrency quota, R3)."""
        ...

    def close(self) -> None: ...


class SqliteJobStore:
    """`jobs(job_id TEXT PRIMARY KEY, status TEXT NOT NULL, owner TEXT NOT
    NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT
    NULL)` in the local `chorus.db` by default. Local dev + tests."""

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
            self._ensure_columns_locked()
            self._conn.commit()

    def _ensure_columns_locked(self) -> None:
        """Caller holds `self._lock`. Additive migration for rows created by
        an older version of this store: `owner` defaults to MASTER_OWNER
        (matches Job.owner's own default for pre-ownership rows), `created_at`
        /`updated_at` default to the epoch sentinel so pre-existing in-flight
        jobs remain eligible for the first post-migration sweep."""
        existing = {row[1] for row in self._conn.execute("PRAGMA table_info(jobs)").fetchall()}
        if "owner" not in existing:
            self._conn.execute(f"ALTER TABLE jobs ADD COLUMN owner TEXT NOT NULL DEFAULT '{MASTER_OWNER}'")
        if "created_at" not in existing:
            self._conn.execute(f"ALTER TABLE jobs ADD COLUMN created_at TEXT NOT NULL DEFAULT '{_EPOCH}'")
        if "updated_at" not in existing:
            self._conn.execute(f"ALTER TABLE jobs ADD COLUMN updated_at TEXT NOT NULL DEFAULT '{_EPOCH}'")

    def create(self, owner: str = MASTER_OWNER) -> str:
        job = Job(job_id=uuid.uuid4().hex, status=JobStatus.queued, owner=owner)
        now = _iso(_now())
        with self._lock:
            self._conn.execute(
                "INSERT INTO jobs (job_id, status, owner, payload, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (job.job_id, job.status.value, owner, job.model_dump_json(), now, now),
            )
            self._conn.commit()
        return job.job_id

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT payload FROM jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
        return Job.model_validate_json(row[0]) if row else None

    def save(self, job: Job) -> None:
        """Conditional write (R6): refuses to move a row already in a
        terminal status (done/failed) to a *different* status — a retried or
        racing writer cannot regress a job another writer already finished.
        Re-saving the same terminal status (e.g. idempotent finalization) is
        allowed. Rows are created by `create()`; a `save()` for an id that
        somehow doesn't exist yet inserts it (defensive; not expected on the
        normal create-then-save path)."""
        now = _iso(_now())
        payload = job.model_dump_json()
        with self._lock:
            cur = self._conn.execute(
                "UPDATE jobs SET status = ?, payload = ?, updated_at = ? "
                "WHERE job_id = ? AND (status NOT IN (?, ?) OR status = ?)",
                (
                    job.status.value,
                    payload,
                    now,
                    job.job_id,
                    JobStatus.done.value,
                    JobStatus.failed.value,
                    job.status.value,
                ),
            )
            if cur.rowcount == 0:
                existing = self._conn.execute(
                    "SELECT status, owner FROM jobs WHERE job_id = ?", (job.job_id,)
                ).fetchone()
                if existing is None:
                    self._conn.execute(
                        "INSERT INTO jobs (job_id, status, owner, payload, created_at, updated_at) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (job.job_id, job.status.value, job.owner, payload, now, now),
                    )
                else:
                    log.warning(
                        "save refused: job %s is terminal (%s); dropping write to status %s",
                        job.job_id,
                        existing[0],
                        job.status.value,
                    )
            self._conn.commit()

    def fail_in_flight(self, reason: str, older_than_seconds: float = 0) -> int:
        """Mark every in-flight job older than `older_than_seconds` `failed`.
        `older_than_seconds=0` (the SQLite/single-process default, matching
        chorus.app's startup sweep) treats every in-flight row as stale, since
        no earlier process's background task can survive a restart in that
        deployment shape. Each row is updated with a compare-and-set guarded
        on (job_id, status, updated_at) so a writer that legitimately advanced
        the row between our SELECT and UPDATE always wins over this sweep —
        no blind overwrite of a newer state (R6)."""
        cutoff = _iso(_now() - timedelta(seconds=older_than_seconds))
        placeholders = ",".join("?" for _ in IN_FLIGHT_STATUSES)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT job_id, payload, status, updated_at FROM jobs "
                f"WHERE status IN ({placeholders}) AND updated_at < ?",
                (*(s.value for s in IN_FLIGHT_STATUSES), cutoff),
            ).fetchall()
        swept = 0
        for job_id, payload, row_status, row_updated_at in rows:
            job = Job.model_validate_json(payload)
            job.status = JobStatus.failed
            job.error = reason
            now = _iso(_now())
            with self._lock:
                cur = self._conn.execute(
                    "UPDATE jobs SET status = ?, payload = ?, updated_at = ? "
                    "WHERE job_id = ? AND status = ? AND updated_at = ?",
                    (job.status.value, job.model_dump_json(), now, job_id, row_status, row_updated_at),
                )
                self._conn.commit()
            if cur.rowcount:
                swept += 1
        return swept

    def count_for_owner(self, owner: str, since: datetime) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM jobs WHERE owner = ? AND created_at >= ?",
                (owner, _iso(since)),
            ).fetchone()
        return int(row[0]) if row else 0

    def count_in_flight(self, owner: str) -> int:
        placeholders = ",".join("?" for _ in IN_FLIGHT_STATUSES)
        with self._lock:
            row = self._conn.execute(
                f"SELECT COUNT(*) FROM jobs WHERE owner = ? AND status IN ({placeholders})",
                (owner, *(s.value for s in IN_FLIGHT_STATUSES)),
            ).fetchone()
        return int(row[0]) if row else 0

    def close(self) -> None:
        with self._lock:
            self._conn.close()
