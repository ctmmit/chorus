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

import hashlib
import logging
import secrets
import uuid
from datetime import UTC, datetime, timezone

from psycopg_pool import ConnectionPool

from chorus.jobs import IN_FLIGHT_STATUSES
from chorus.keys import KEY_BYTES, KEY_PREFIX, KEY_RATE_LIMIT, KeyRateLimited
from chorus.models import Job, JobStatus, Transcript
from chorus.subscriptions import Subscription, SubscriptionList

log = logging.getLogger("chorus.stores.postgres")

JOBS_TABLE = "jobs"
TRANSCRIPTS_TABLE = "transcripts"
API_KEYS_TABLE = "api_keys"
SUBSCRIPTIONS_TABLE = "subscriptions"

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


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class PostgresKeyStore:
    """Mirrors chorus.keys.SqliteKeyStore's table shape and semantics
    (same api_keys layout: key_hash/email/created_at/revoked), against
    Postgres via a DSN. Selected by chorus.config_env.select_key_store when
    DATABASE_URL is set (Phase F: a Vercel deploy's issued keys otherwise
    live in an ephemeral SQLite file and vanish between invocations)."""

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn
        self._pool = ConnectionPool(dsn, min_size=POOL_MIN_SIZE, max_size=POOL_MAX_SIZE, open=True)
        with self._pool.connection() as conn:
            conn.execute(
                f"CREATE TABLE IF NOT EXISTS {API_KEYS_TABLE} "
                "(key_hash TEXT PRIMARY KEY, email TEXT NOT NULL, created_at TEXT NOT NULL, "
                "revoked INTEGER NOT NULL DEFAULT 0)"
            )
            conn.commit()

    def issue(self, email: str) -> str:
        normalized_email = email.strip().lower()
        now = datetime.now(UTC)
        cutoff = (now - KEY_RATE_LIMIT).isoformat()
        with self._pool.connection() as conn:
            recent = conn.execute(
                f"SELECT 1 FROM {API_KEYS_TABLE} WHERE email = %s AND created_at > %s LIMIT 1",
                (normalized_email, cutoff),
            ).fetchone()
            if recent is not None:
                raise KeyRateLimited("one API key may be issued per email per hour")

            token = f"{KEY_PREFIX}{secrets.token_urlsafe(KEY_BYTES)}"
            conn.execute(
                f"INSERT INTO {API_KEYS_TABLE} (key_hash, email, created_at, revoked) "
                "VALUES (%s, %s, %s, 0)",
                (_hash_token(token), normalized_email, now.isoformat()),
            )
            conn.commit()
        return token

    def is_valid(self, token: str) -> bool:
        with self._pool.connection() as conn:
            row = conn.execute(
                f"SELECT revoked FROM {API_KEYS_TABLE} WHERE key_hash = %s", (_hash_token(token),)
            ).fetchone()
        return row is not None and row[0] == 0

    def revoke(self, token: str) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                f"UPDATE {API_KEYS_TABLE} SET revoked = 1 WHERE key_hash = %s", (_hash_token(token),)
            )
            conn.commit()

    def owner_of(self, token: str) -> str | None:
        with self._pool.connection() as conn:
            row = conn.execute(
                f"SELECT email FROM {API_KEYS_TABLE} WHERE key_hash = %s AND revoked = 0",
                (_hash_token(token),),
            ).fetchone()
        return row[0] if row else None

    def close(self) -> None:
        self._pool.close()


class PostgresSubscriptionStore:
    """Mirrors chorus.subscriptions.SqliteSubscriptionStore's table shape
    and semantics (subscription_id/owner/active/next_run_at/payload),
    against Postgres via a DSN. Selected by
    chorus.config_env.select_subscription_store when DATABASE_URL is set."""

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn
        self._pool = ConnectionPool(dsn, min_size=POOL_MIN_SIZE, max_size=POOL_MAX_SIZE, open=True)
        with self._pool.connection() as conn:
            conn.execute(
                f"CREATE TABLE IF NOT EXISTS {SUBSCRIPTIONS_TABLE} ("
                "subscription_id TEXT PRIMARY KEY, owner TEXT NOT NULL, "
                "active INTEGER NOT NULL, next_run_at TEXT NOT NULL, payload TEXT NOT NULL)"
            )
            conn.execute(
                f"CREATE INDEX IF NOT EXISTS idx_subscriptions_due "
                f"ON {SUBSCRIPTIONS_TABLE} (active, next_run_at)"
            )
            conn.execute(
                f"CREATE INDEX IF NOT EXISTS idx_subscriptions_owner ON {SUBSCRIPTIONS_TABLE} (owner)"
            )
            conn.commit()

    def _upsert(self, subscription: Subscription) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                f"INSERT INTO {SUBSCRIPTIONS_TABLE} "
                "(subscription_id, owner, active, next_run_at, payload) VALUES (%s, %s, %s, %s, %s) "
                "ON CONFLICT (subscription_id) DO UPDATE SET "
                "owner = EXCLUDED.owner, active = EXCLUDED.active, "
                "next_run_at = EXCLUDED.next_run_at, payload = EXCLUDED.payload",
                (
                    subscription.subscription_id,
                    subscription.owner,
                    int(subscription.active),
                    subscription.next_run_at.isoformat(),
                    subscription.model_dump_json(),
                ),
            )
            conn.commit()

    def create(self, subscription: Subscription) -> str:
        self._upsert(subscription)
        return subscription.subscription_id

    def get(self, subscription_id: str) -> Subscription | None:
        with self._pool.connection() as conn:
            row = conn.execute(
                f"SELECT payload FROM {SUBSCRIPTIONS_TABLE} WHERE subscription_id = %s",
                (subscription_id,),
            ).fetchone()
        return Subscription.model_validate_json(row[0]) if row else None

    def list(self, owner: str | None = None) -> SubscriptionList:
        with self._pool.connection() as conn:
            if owner is None:
                rows = conn.execute(f"SELECT payload FROM {SUBSCRIPTIONS_TABLE}").fetchall()
            else:
                rows = conn.execute(
                    f"SELECT payload FROM {SUBSCRIPTIONS_TABLE} WHERE owner = %s", (owner,)
                ).fetchall()
        return [Subscription.model_validate_json(r[0]) for r in rows]

    def save(self, subscription: Subscription) -> None:
        self._upsert(subscription)

    def delete(self, subscription_id: str) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                f"DELETE FROM {SUBSCRIPTIONS_TABLE} WHERE subscription_id = %s", (subscription_id,)
            )
            conn.commit()

    def due(self, now: datetime) -> SubscriptionList:
        with self._pool.connection() as conn:
            rows = conn.execute(
                f"SELECT payload FROM {SUBSCRIPTIONS_TABLE} WHERE active = 1 AND next_run_at <= %s",
                (now.isoformat(),),
            ).fetchall()
        return [Subscription.model_validate_json(r[0]) for r in rows]

    def close(self) -> None:
        self._pool.close()
