"""Environment-driven selection of stores, caches, artifact backends and job
runners (docs/DEVELOPMENT_PLAN.md §5, "Vercel instead of Railway"). Local dev
and tests need zero configuration — SQLite, local disk, in-process
BackgroundTasks. On Vercel, each backend flips to its cloud equivalent purely
by which env var is present:

    DATABASE_URL              -> Postgres (Neon) job store + transcript cache
    BLOB_READ_WRITE_TOKEN      -> Vercel Blob artifact store
    INNGEST_EVENT_KEY +
    INNGEST_SIGNING_KEY        -> Inngest job runner (durable steps)

`chorus.config.load_env` (dotenv for LIVE runs only) is unrelated to this
module: this module only ever reads `os.environ`, so it stays hermetic for
tests exactly like chorus.pipeline.default_deps does.
"""
from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

from chorus.artifacts import ArtifactStore, LocalArtifactStore, VercelBlobStore
from chorus.jobs import DEFAULT_DB, JobStore, SqliteJobStore
from chorus.transcript_cache import SqliteTranscriptCache, TranscriptCache

if TYPE_CHECKING:
    from chorus.keys import KeyStore
    from chorus.pipeline import Deps
    from chorus.runners import JobRunner
    from chorus.subscriptions import SubscriptionStore

log = logging.getLogger("chorus.config_env")

DATABASE_URL_ENV = "DATABASE_URL"
BLOB_TOKEN_ENV = "BLOB_READ_WRITE_TOKEN"
INNGEST_EVENT_KEY_ENV = "INNGEST_EVENT_KEY"
INNGEST_SIGNING_KEY_ENV = "INNGEST_SIGNING_KEY"

# Every Chorus function/step registers under this Inngest app id.
INNGEST_APP_ID = "chorus"


def select_job_store() -> JobStore:
    dsn = os.environ.get(DATABASE_URL_ENV)
    if dsn:
        from chorus.stores.postgres import PostgresJobStore

        log.info("jobs: %s set — using PostgresJobStore", DATABASE_URL_ENV)
        return PostgresJobStore(dsn)
    return SqliteJobStore()


def select_key_store(store: JobStore | None = None) -> KeyStore:
    """DATABASE_URL set -> PostgresKeyStore (Phase F: issued keys otherwise
    live in an ephemeral SQLite file on Vercel and vanish between
    invocations). Otherwise SqliteKeyStore, co-located with `store`'s SQLite
    file when one is given (same file `store` uses, e.g. a test's tmp_path)
    so issuing a key and using it against the same app instance work
    together, exactly as chorus.app.create_app did when it built
    SqliteKeyStore inline."""
    dsn = os.environ.get(DATABASE_URL_ENV)
    if dsn:
        from chorus.stores.postgres import PostgresKeyStore

        log.info("keys: %s set — using PostgresKeyStore", DATABASE_URL_ENV)
        return PostgresKeyStore(dsn)
    from chorus.keys import SqliteKeyStore

    db_path = getattr(store, "db_path", None) or DEFAULT_DB
    return SqliteKeyStore(db_path)


def select_subscription_store(store: JobStore | None = None) -> SubscriptionStore:
    """Same selection rule as select_key_store: Postgres when DATABASE_URL is
    set, else SQLite co-located with `store`'s file (or DEFAULT_DB)."""
    dsn = os.environ.get(DATABASE_URL_ENV)
    if dsn:
        from chorus.stores.postgres import PostgresSubscriptionStore

        log.info("subscriptions: %s set — using PostgresSubscriptionStore", DATABASE_URL_ENV)
        return PostgresSubscriptionStore(dsn)
    from chorus.subscriptions import SqliteSubscriptionStore

    db_path = getattr(store, "db_path", None) or DEFAULT_DB
    return SqliteSubscriptionStore(db_path)


def select_transcript_cache() -> TranscriptCache:
    dsn = os.environ.get(DATABASE_URL_ENV)
    if dsn:
        from chorus.stores.postgres import PostgresTranscriptCache

        log.info("transcripts: %s set — cache = PostgresTranscriptCache", DATABASE_URL_ENV)
        return PostgresTranscriptCache(dsn)
    return SqliteTranscriptCache()


def select_artifact_store() -> ArtifactStore:
    token = os.environ.get(BLOB_TOKEN_ENV)
    if token:
        log.info("artifacts: %s set — using VercelBlobStore", BLOB_TOKEN_ENV)
        return VercelBlobStore(token)
    return LocalArtifactStore()


def inngest_configured() -> bool:
    """True once BOTH keys Inngest needs are present: the event key to send
    and receive events, the signing key to verify that a request to
    /api/inngest actually came from Inngest (not an open, unauthenticated
    trigger of the pipeline)."""
    return bool(os.environ.get(INNGEST_EVENT_KEY_ENV) and os.environ.get(INNGEST_SIGNING_KEY_ENV))


def build_deps() -> Deps:
    """Deps for the current environment: the same transcript-provider ladder
    as chorus.pipeline.default_deps (fixtures first, then whichever live
    providers have keys), but with the transcript cache and artifact store
    selected by env instead of hardcoded to SQLite/local — Postgres/Blob on
    Vercel, SQLite/local everywhere else (including every test, since none of
    DATABASE_URL/BLOB_READ_WRITE_TOKEN are set there).
    """
    from chorus.pipeline import default_deps

    deps = default_deps()
    # CachingTranscriptProvider (chorus.transcript_cache) wraps the provider
    # chain in a `.cache` attribute; swapping it post-construction avoids
    # duplicating default_deps's provider-chain-building logic here.
    deps.provider.cache = select_transcript_cache()  # type: ignore[attr-defined]
    deps.artifacts = select_artifact_store()
    return deps


def select_runner(store: JobStore, deps: Deps) -> JobRunner:
    from chorus.runners import BackgroundRunner, InngestRunner

    if inngest_configured():
        import inngest

        client = inngest.Inngest(
            app_id=INNGEST_APP_ID,
            event_key=os.environ.get(INNGEST_EVENT_KEY_ENV),
            signing_key=os.environ.get(INNGEST_SIGNING_KEY_ENV),
        )
        log.info("runner: Inngest keys set — using InngestRunner")
        return InngestRunner(client)
    return BackgroundRunner(store, deps)
