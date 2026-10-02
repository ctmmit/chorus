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
from datetime import UTC, datetime, timedelta

from psycopg_pool import ConnectionPool

from chorus.jobs import IN_FLIGHT_STATUSES, MASTER_OWNER
from chorus.keys import KEY_BYTES, KEY_PREFIX, KEY_RATE_LIMIT, KeyRateLimited
from chorus.models import Job, JobStatus, Transcript
from chorus.personas import PERSONA_TABLE, Persona
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

_EPOCH = "1970-01-01T00:00:00+00:00"


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


class PostgresJobStore:
    """Mirrors chorus.jobs.SqliteJobStore's table shape and semantics
    (owner/created_at/updated_at columns, conditional `save`, windowed
    `fail_in_flight`), against Postgres via a DSN (Neon Marketplace
    `DATABASE_URL`)."""

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn
        self._pool = ConnectionPool(dsn, min_size=POOL_MIN_SIZE, max_size=POOL_MAX_SIZE, open=True)
        with self._pool.connection() as conn:
            conn.execute(
                f"CREATE TABLE IF NOT EXISTS {JOBS_TABLE} "
                "(job_id TEXT PRIMARY KEY, status TEXT NOT NULL, payload TEXT NOT NULL)"
            )
            conn.execute(
                f"ALTER TABLE {JOBS_TABLE} ADD COLUMN IF NOT EXISTS "
                f"owner TEXT NOT NULL DEFAULT '{MASTER_OWNER}'"
            )
            conn.execute(
                f"ALTER TABLE {JOBS_TABLE} ADD COLUMN IF NOT EXISTS "
                f"created_at TEXT NOT NULL DEFAULT '{_EPOCH}'"
            )
            conn.execute(
                f"ALTER TABLE {JOBS_TABLE} ADD COLUMN IF NOT EXISTS "
                f"updated_at TEXT NOT NULL DEFAULT '{_EPOCH}'"
            )
            conn.execute(
                f"CREATE INDEX IF NOT EXISTS idx_jobs_owner_created ON {JOBS_TABLE} (owner, created_at)"
            )
            conn.commit()

    def create(self, owner: str = MASTER_OWNER) -> str:
        job = Job(job_id=uuid.uuid4().hex, status=JobStatus.queued, owner=owner)
        now = _iso(_now())
        with self._pool.connection() as conn:
            conn.execute(
                f"INSERT INTO {JOBS_TABLE} (job_id, status, owner, payload, created_at, updated_at) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (job.job_id, job.status.value, owner, job.model_dump_json(), now, now),
            )
            conn.commit()
        return job.job_id

    def get(self, job_id: str) -> Job | None:
        with self._pool.connection() as conn:
            row = conn.execute(
                f"SELECT payload FROM {JOBS_TABLE} WHERE job_id = %s", (job_id,)
            ).fetchone()
        return Job.model_validate_json(row[0]) if row else None

    def save(self, job: Job) -> None:
        """Conditional write (R6): a single atomic UPSERT that refuses to
        move an existing terminal row (done/failed) to a different status —
        `WHERE` on the `ON CONFLICT DO UPDATE` clause is evaluated against
        the pre-update row, so a stale writer's UPDATE simply matches zero
        rows instead of clobbering a newer terminal state."""
        now = _iso(_now())
        with self._pool.connection() as conn:
            row = conn.execute(
                f"INSERT INTO {JOBS_TABLE} (job_id, status, owner, payload, created_at, updated_at) "
                "VALUES (%s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (job_id) DO UPDATE SET "
                "status = EXCLUDED.status, payload = EXCLUDED.payload, updated_at = EXCLUDED.updated_at "
                f"WHERE {JOBS_TABLE}.status NOT IN (%s, %s) OR {JOBS_TABLE}.status = EXCLUDED.status "
                "RETURNING job_id",
                (
                    job.job_id,
                    job.status.value,
                    job.owner,
                    job.model_dump_json(),
                    now,
                    now,
                    JobStatus.done.value,
                    JobStatus.failed.value,
                ),
            ).fetchone()
            conn.commit()
        if row is None:
            log.warning("save refused: job %s is terminal; dropping write to status %s", job.job_id, job.status.value)

    def fail_in_flight(self, reason: str, older_than_seconds: float = 0) -> int:
        """Same windowed, compare-and-set sweep semantics as
        SqliteJobStore.fail_in_flight: only rows in an in-flight status whose
        `updated_at` predates `cutoff` are swept, and each row's UPDATE is
        guarded on (job_id, status, updated_at) so a writer that legitimately
        advanced the row between the SELECT and UPDATE always wins (R6)."""
        cutoff = _iso(_now() - timedelta(seconds=older_than_seconds))
        placeholders = ",".join("%s" for _ in IN_FLIGHT_STATUSES)
        with self._pool.connection() as conn:
            rows = conn.execute(
                f"SELECT job_id, payload, status, updated_at FROM {JOBS_TABLE} "
                f"WHERE status IN ({placeholders}) AND updated_at < %s",
                (*(s.value for s in IN_FLIGHT_STATUSES), cutoff),
            ).fetchall()
            swept = 0
            for job_id, payload, row_status, row_updated_at in rows:
                job = Job.model_validate_json(payload)
                job.status = JobStatus.failed
                job.error = reason
                now = _iso(_now())
                cur = conn.execute(
                    f"UPDATE {JOBS_TABLE} SET status = %s, payload = %s, updated_at = %s "
                    "WHERE job_id = %s AND status = %s AND updated_at = %s",
                    (job.status.value, job.model_dump_json(), now, job_id, row_status, row_updated_at),
                )
                swept += cur.rowcount
            conn.commit()
        return swept

    def count_for_owner(self, owner: str, since: datetime) -> int:
        with self._pool.connection() as conn:
            row = conn.execute(
                f"SELECT COUNT(*) FROM {JOBS_TABLE} WHERE owner = %s AND created_at >= %s",
                (owner, _iso(since)),
            ).fetchone()
        return int(row[0]) if row else 0

    def count_in_flight(self, owner: str) -> int:
        placeholders = ",".join("%s" for _ in IN_FLIGHT_STATUSES)
        with self._pool.connection() as conn:
            row = conn.execute(
                f"SELECT COUNT(*) FROM {JOBS_TABLE} WHERE owner = %s AND status IN ({placeholders})",
                (owner, *(s.value for s in IN_FLIGHT_STATUSES)),
            ).fetchone()
        return int(row[0]) if row else 0

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
        fetched_at = datetime.now(UTC).isoformat()
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
            # revoked = 0: a key revoked by chorus.app's compensating revoke
            # (R23, delivery failed after mint) must not itself block a retry.
            recent = conn.execute(
                f"SELECT 1 FROM {API_KEYS_TABLE} WHERE email = %s AND created_at > %s "
                "AND revoked = 0 LIMIT 1",
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


class PostgresPersonaRegistry:
    """Mirrors chorus.personas.SqlitePersonaRegistry's table shape and
    semantics (same 3-column persona_id/public/payload pattern), against
    Postgres via a DSN. Selected by chorus.config_env.select_persona_registry
    when DATABASE_URL is set (R1: before this class existed, a Postgres
    deployment fell back to a repository-root SqlitePersonaRegistry, which
    cannot write on Vercel's read-only filesystem)."""

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn
        self._pool = ConnectionPool(dsn, min_size=POOL_MIN_SIZE, max_size=POOL_MAX_SIZE, open=True)
        with self._pool.connection() as conn:
            conn.execute(
                f"CREATE TABLE IF NOT EXISTS {PERSONA_TABLE} "
                "(persona_id TEXT PRIMARY KEY, public INTEGER NOT NULL, payload TEXT NOT NULL)"
            )
            conn.commit()

    def create(self, persona: Persona) -> Persona:
        with self._pool.connection() as conn:
            conn.execute(
                f"INSERT INTO {PERSONA_TABLE} (persona_id, public, payload) VALUES (%s, %s, %s) "
                "ON CONFLICT (persona_id) DO UPDATE SET "
                "public = EXCLUDED.public, payload = EXCLUDED.payload",
                (persona.persona_id, int(persona.public), persona.model_dump_json()),
            )
            conn.commit()
        return persona

    def get(self, persona_id: str) -> Persona | None:
        with self._pool.connection() as conn:
            row = conn.execute(
                f"SELECT payload FROM {PERSONA_TABLE} WHERE persona_id = %s", (persona_id,)
            ).fetchone()
        return Persona.model_validate_json(row[0]) if row else None

    def list(self, *, public_only: bool = False) -> list[Persona]:
        query = f"SELECT payload FROM {PERSONA_TABLE}"
        if public_only:
            query += " WHERE public = 1"
        with self._pool.connection() as conn:
            rows = conn.execute(query).fetchall()
        return [Persona.model_validate_json(r[0]) for r in rows]

    def delete(self, persona_id: str) -> bool:
        with self._pool.connection() as conn:
            cur = conn.execute(f"DELETE FROM {PERSONA_TABLE} WHERE persona_id = %s", (persona_id,))
            conn.commit()
        return cur.rowcount > 0

    def close(self) -> None:
        self._pool.close()
