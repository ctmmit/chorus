"""Postgres-backed `JobStore` and `TranscriptCache` (the Protocols in
chorus.jobs / chorus.transcript_cache) — the Vercel-port replacement for
SQLite (Phase B, docs/DEVELOPMENT_PLAN.md §5): Neon's pooled DSN is the
target, selected by chorus.config_env when DATABASE_URL is set.

Same column layout as the SQLite tables (job_id/status/payload,
episode_id/payload/fetched_at), so anyone with a DSN can run these classes
through tests/test_store_contract.py's contract suite — the same
create/get/save/fail_in_flight and cache get/put behavior the SQLite
implementations are checked against.

Uses psycopg v3 (`psycopg[binary]`) with a small connection pool
(`psycopg_pool.ConnectionPool`) rather than a bare connection per call, since
one Vercel function instance can serve more than one request/invocation
before it is frozen or recycled.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from psycopg_pool import ConnectionPool

from chorus.jobs import IN_FLIGHT_STATUSES
from chorus.models import Job, JobStatus, Transcript

log = logging.getLogger("chorus.stores.postgres")

JOBS_TABLE = "jobs"
TRANSCRIPTS_TABLE = "transcripts"

# min_size=0: a cold Vercel function opens no DB connections until the first
# request; max_size kept small since Neon's pooled DSN already pools upstream
# (PgBouncer), so a large pool here just duplicates that layer.
POOL_MIN_SIZE = 0
POOL_MAX_SIZE = 5


class PostgresJobStore:
    """Mirrors chorus.jobs.SqliteJobStore's table shape and semantics,
    against Postgres via a DSN (Neon Marketplace `DATABASE_URL`)."""

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn
        self._pool = ConnectionPool(dsn, min_size=POOL_MIN_SIZE, max_size=POOL_MAX_SIZE, open=True)
        with self._pool.connection() as conn:
            conn.execute(
                f"CREATE TABLE IF NOT EXISTS {JOBS_TABLE} "
                "(job_id TEXT PRIMARY KEY, status TEXT NOT NULL, payload TEXT NOT NULL)"
            )
            conn.commit()

    def create(self) -> str:
        job = Job(job_id=uuid.uuid4().hex, status=JobStatus.queued)
        self.save(job)
        return job.job_id

    def get(self, job_id: str) -> Job | None:
        with self._pool.connection() as conn:
            row = conn.execute(
                f"SELECT payload FROM {JOBS_TABLE} WHERE job_id = %s", (job_id,)
            ).fetchone()
        return Job.model_validate_json(row[0]) if row else None

    def save(self, job: Job) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                f"INSERT INTO {JOBS_TABLE} (job_id, status, payload) VALUES (%s, %s, %s) "
                "ON CONFLICT (job_id) DO UPDATE SET status = EXCLUDED.status, payload = EXCLUDED.payload",
                (job.job_id, job.status.value, job.model_dump_json()),
            )
            conn.commit()

    def fail_in_flight(self, reason: str) -> int:
        """Same restart-sweep semantics as SqliteJobStore.fail_in_flight:
        every job left queued/digest_ready when the previous process/
        invocation died is marked failed so it never polls forever."""
        placeholders = ",".join("%s" for _ in IN_FLIGHT_STATUSES)
        with self._pool.connection() as conn:
            rows = conn.execute(
                f"SELECT payload FROM {JOBS_TABLE} WHERE status IN ({placeholders})",
                tuple(s.value for s in IN_FLIGHT_STATUSES),
            ).fetchall()
        stale = [Job.model_validate_json(r[0]) for r in rows]
        for job in stale:
            job.status = JobStatus.failed
            job.error = reason
            self.save(job)
        return len(stale)

    def close(self) -> None:
        self._pool.close()


class PostgresTranscriptCache:
    """Mirrors chorus.transcript_cache.SqliteTranscriptCache's table shape
    and semantics, against Postgres via a DSN."""

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn
        self._pool = ConnectionPool(dsn, min_size=POOL_MIN_SIZE, max_size=POOL_MAX_SIZE, open=True)
        with self._pool.connection() as conn:
            conn.execute(
                f"CREATE TABLE IF NOT EXISTS {TRANSCRIPTS_TABLE} "
                "(episode_id TEXT PRIMARY KEY, payload TEXT NOT NULL, fetched_at TEXT NOT NULL)"
            )
            conn.commit()

    def get(self, episode_id: str) -> Transcript | None:
        with self._pool.connection() as conn:
            row = conn.execute(
                f"SELECT payload FROM {TRANSCRIPTS_TABLE} WHERE episode_id = %s", (episode_id,)
            ).fetchone()
        return Transcript.model_validate_json(row[0]) if row else None

    def put(self, transcript: Transcript) -> None:
        fetched_at = datetime.now(timezone.utc).isoformat()
        with self._pool.connection() as conn:
            conn.execute(
                f"INSERT INTO {TRANSCRIPTS_TABLE} (episode_id, payload, fetched_at) VALUES (%s, %s, %s) "
                "ON CONFLICT (episode_id) DO UPDATE SET "
                "payload = EXCLUDED.payload, fetched_at = EXCLUDED.fetched_at",
                (transcript.video_id, transcript.model_dump_json(), fetched_at),
            )
            conn.commit()

    def close(self) -> None:
        self._pool.close()
