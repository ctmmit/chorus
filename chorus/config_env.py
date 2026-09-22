"""Environment-driven selection of stores, caches, artifact backends and job
runners (docs/DEVELOPMENT_PLAN.md §5, "Vercel instead of Railway"). Local dev
and tests need zero configuration — SQLite, local disk, in-process
BackgroundTasks. On Vercel, each backend flips to its cloud equivalent purely
by which env var is present:

    DATABASE_URL              -> Postgres (Neon) job/key/subscription/persona
                                  store + transcript cache
    BLOB_READ_WRITE_TOKEN      -> Vercel Blob artifact store
    INNGEST_EVENT_KEY +
    INNGEST_SIGNING_KEY        -> Inngest job runner (durable steps)

`chorus.config.load_env` (dotenv for LIVE runs only) is unrelated to this
module: this module only ever reads `os.environ`, so it stays hermetic for
tests exactly like chorus.pipeline.default_deps does.

R1 (docs/REVIEW_WAVE1.md #1): every `select_*` function below branches on
`DATABASE_URL` BEFORE constructing anything, so a Postgres deployment never
even imports/constructs a Sqlite* class — Vercel Functions expose a
read-only filesystem outside `/tmp`, so a stray `sqlite3.connect()` at a
repository-root path can crash startup before serving a single request.
`build_deps()` in particular builds its own transcript-provider chain rather
than calling `chorus.pipeline.default_deps()` and swapping the cache
afterward, because that older approach always constructed a
`SqliteTranscriptCache()` (and immediately ran its `CREATE TABLE`) even when
the result was about to be discarded.
"""
from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

from chorus.artifacts import ArtifactStore, LocalArtifactStore, VercelBlobStore
from chorus.jobs import DEFAULT_DB, JobStore, MASTER_OWNER, SqliteJobStore

if TYPE_CHECKING:
    from chorus.keys import KeyStore
    from chorus.personas import PersonaRegistry
    from chorus.pipeline import Deps
    from chorus.runners import JobRunner
    from chorus.subscriptions import SubscriptionStore
    from chorus.transcript_cache import TranscriptCache

log = logging.getLogger("chorus.config_env")

DATABASE_URL_ENV = "DATABASE_URL"
BLOB_TOKEN_ENV = "BLOB_READ_WRITE_TOKEN"
INNGEST_EVENT_KEY_ENV = "INNGEST_EVENT_KEY"
INNGEST_SIGNING_KEY_ENV = "INNGEST_SIGNING_KEY"

# Every Chorus function/step registers under this Inngest app id.
INNGEST_APP_ID = "chorus"


def _dsn() -> str | None:
    return os.environ.get(DATABASE_URL_ENV)


def select_job_store() -> JobStore:
    dsn = _dsn()
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
    dsn = _dsn()
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
    dsn = _dsn()
    if dsn:
        from chorus.stores.postgres import PostgresSubscriptionStore

        log.info("subscriptions: %s set — using PostgresSubscriptionStore", DATABASE_URL_ENV)
        return PostgresSubscriptionStore(dsn)
    from chorus.subscriptions import SqliteSubscriptionStore

    db_path = getattr(store, "db_path", None) or DEFAULT_DB
    return SqliteSubscriptionStore(db_path)


def select_persona_registry(store: JobStore | None = None) -> PersonaRegistry:
    """Same selection rule again: Postgres when DATABASE_URL is set (R1 —
    previously chorus.app.create_app always built a SqlitePersonaRegistry,
    which cannot write on Vercel's read-only filesystem), else SQLite
    co-located with `store`'s file (or DEFAULT_DB), matching how
    chorus.app.create_app built it inline before this function existed."""
    dsn = _dsn()
    if dsn:
        from chorus.stores.postgres import PostgresPersonaRegistry

        log.info("personas: %s set — using PostgresPersonaRegistry", DATABASE_URL_ENV)
        return PostgresPersonaRegistry(dsn)
    from chorus.personas import SqlitePersonaRegistry

    db_path = getattr(store, "db_path", None) or DEFAULT_DB
    return SqlitePersonaRegistry(db_path)


def select_transcript_cache() -> TranscriptCache:
    dsn = _dsn()
    if dsn:
        from chorus.stores.postgres import PostgresTranscriptCache

        log.info("transcripts: %s set — cache = PostgresTranscriptCache", DATABASE_URL_ENV)
        return PostgresTranscriptCache(dsn)
    from chorus.transcript_cache import SqliteTranscriptCache

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
    providers have keys), built directly here (R1) — NOT by calling
    default_deps() and replacing its cache afterward, which always
    constructed (and ran DDL against) a repository-root SqliteTranscriptCache
    even in Postgres mode, before the swap discarded it. On Vercel's
    read-only filesystem outside `/tmp` that stray connection attempt can
    crash startup before a single request is served.
    """
    from chorus.audio import get_audio_renderer
    from chorus.llm import get_llm_client
    from chorus.pipeline import (
        DEEPGRAM_API_KEY_ENV,
        TRANSCRIPT_API_KEY_ENV,
        Deps,
    )
    from chorus.script import get_script_composer
    from chorus.transcript_cache import CachingTranscriptProvider
    from chorus.transcripts import (
        ChainTranscriptProvider,
        DeepgramTranscriptProvider,
        FixtureTranscriptProvider,
        ManagedCaptionsProvider,
        RssTranscriptProvider,
        TranscriptProvider,
    )

    providers: list[TranscriptProvider] = [FixtureTranscriptProvider()]
    active = ["fixture"]

    transcript_api_key = os.environ.get(TRANSCRIPT_API_KEY_ENV)
    if transcript_api_key:
        providers.append(ManagedCaptionsProvider(transcript_api_key))
        active.append("supadata")

    rss_provider = RssTranscriptProvider()
    providers.append(rss_provider)
    active.append("rss")

    deepgram_api_key = os.environ.get(DEEPGRAM_API_KEY_ENV)
    if deepgram_api_key:
        providers.append(DeepgramTranscriptProvider(deepgram_api_key, rss_provider=rss_provider))
        active.append("deepgram")

    log.info("transcripts: provider chain = %s", " -> ".join(active))
    chain = ChainTranscriptProvider(providers)
    cached_provider = CachingTranscriptProvider(chain, select_transcript_cache())

    return Deps(
        provider=cached_provider,
        llm=get_llm_client(),
        composer=get_script_composer(),
        renderer=get_audio_renderer(),
        artifacts=select_artifact_store(),
    )


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


__all__ = [
    "MASTER_OWNER",
    "select_job_store",
    "select_key_store",
    "select_subscription_store",
    "select_persona_registry",
    "select_transcript_cache",
    "select_artifact_store",
    "inngest_configured",
    "build_deps",
    "select_runner",
]
