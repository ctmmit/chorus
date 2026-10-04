"""Environment-driven selection of stores, caches, artifact backends and job
runners (docs/DEVELOPMENT_PLAN.md §5, "Vercel instead of Railway"). Local dev
and tests need zero configuration — SQLite, local disk, in-process
BackgroundTasks. On Vercel, each backend flips to its cloud equivalent purely
by which env var is present:

    DATABASE_URL              -> Postgres (Neon) job/key/subscription/persona/
                                  saved-item store + transcript cache
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
from chorus.jobs import DEFAULT_DB, MASTER_OWNER, JobStore, SqliteJobStore

if TYPE_CHECKING:
    from chorus.feedback import FeedbackStore
    from chorus.keys import KeyStore
    from chorus.personas import PersonaRegistry
    from chorus.pipeline import Deps
    from chorus.runners import JobRunner
    from chorus.saved_items import SavedItemStore
    from chorus.subscriptions import SubscriptionStore
    from chorus.transcript_cache import TranscriptCache
    from chorus.transcripts import TranscriptProvider

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


def select_saved_item_store(store: JobStore | None = None) -> SavedItemStore:
    """Imported library items (chorus.library): the same selection rule as
    select_subscription_store."""
    dsn = _dsn()
    if dsn:
        from chorus.stores.postgres import PostgresSavedItemStore

        log.info("saved items: %s set — using PostgresSavedItemStore", DATABASE_URL_ENV)
        return PostgresSavedItemStore(dsn)
    from chorus.saved_items import SqliteSavedItemStore

    db_path = getattr(store, "db_path", None) or DEFAULT_DB
    return SqliteSavedItemStore(db_path)


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


def select_feedback_store(store: JobStore | None = None) -> FeedbackStore:
    """Highlight ratings and soul proposals (chorus.feedback): the same
    selection rule as select_subscription_store."""
    dsn = _dsn()
    if dsn:
        from chorus.stores.postgres import PostgresFeedbackStore

        log.info("feedback: %s set — using PostgresFeedbackStore", DATABASE_URL_ENV)
        return PostgresFeedbackStore(dsn)
    from chorus.feedback import SqliteFeedbackStore

    db_path = getattr(store, "db_path", None) or DEFAULT_DB
    return SqliteFeedbackStore(db_path)


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


def build_transcript_chain(cache: TranscriptCache) -> TranscriptProvider:
    """The transcript provider ladder for the current environment, wrapped in
    `cache`. The ONE builder behind both `build_deps` (Postgres/Vercel) and
    `chorus.pipeline.default_deps` (SQLite/local), so the two cannot drift.

    Order, each rung skipped when its key is absent (fixture and RSS always run):

        1. fixture                     tests/dev, never a live third party
        2. RSS `podcast:transcript`    publisher's own file: free, sanctioned,
                                       sometimes word-level with real speaker names
        3. AssemblyAI   (ASSEMBLYAI_API_KEY)  speech-to-text on the RSS enclosure,
                                       primary ASR: ~$0.23/audio hour, diarized
        4. Deepgram     (DEEPGRAM_API_KEY)    backup ASR, a different model lineage
        5. Supadata     (TRANSCRIPT_API_KEY)  native YouTube captions, last resort

    Why this order (reports/Podcast transcript sources.md, 02 Oct 2026): every
    show with a feed is covered by transcribing the publisher's own enclosure
    audio for about $0.29 per 75-minute episode, so price no longer justifies
    the cheapest-looking source. YouTube captions cost about half a cent but
    breach YouTube's terms, fail from cloud IPs, carry no speaker labels, run
    ~10% median WER, and index a video whose intro and ad load can differ from
    the podcast audio, so they only serve shows with no RSS audio. The publisher's
    transcript goes first when it exists because Thomson Reuters v. ROSS
    (3d Cir., 29 Sep 2026) rewards using an authorized source over re-creating
    one for convenience, and because it is free. Speaker labels, which turn a
    pull-quote into an attributed citation, come from the ASR tiers.

    The cache wraps the whole chain and stores only successes, so an episode
    whose publisher transcript appears later is picked up on its next request.
    """
    from chorus.pipeline import (
        ASSEMBLYAI_API_KEY_ENV,
        DEEPGRAM_API_KEY_ENV,
        TRANSCRIPT_API_KEY_ENV,
    )
    from chorus.transcript_cache import CachingTranscriptProvider
    from chorus.transcripts import (
        AssemblyAITranscriptProvider,
        ChainTranscriptProvider,
        DeepgramTranscriptProvider,
        FixtureTranscriptProvider,
        ManagedCaptionsProvider,
        RssTranscriptProvider,
    )

    rss_provider = RssTranscriptProvider()
    providers: list[TranscriptProvider] = [FixtureTranscriptProvider(), rss_provider]
    active = ["fixture", "rss"]

    assemblyai_api_key = os.environ.get(ASSEMBLYAI_API_KEY_ENV)
    if assemblyai_api_key:
        providers.append(AssemblyAITranscriptProvider(assemblyai_api_key, rss_provider=rss_provider))
        active.append("assemblyai")

    deepgram_api_key = os.environ.get(DEEPGRAM_API_KEY_ENV)
    if deepgram_api_key:
        providers.append(DeepgramTranscriptProvider(deepgram_api_key, rss_provider=rss_provider))
        active.append("deepgram")

    transcript_api_key = os.environ.get(TRANSCRIPT_API_KEY_ENV)
    if transcript_api_key:
        providers.append(ManagedCaptionsProvider(transcript_api_key))
        active.append("supadata")

    log.info("transcripts: provider chain = %s", " -> ".join(active))
    return CachingTranscriptProvider(ChainTranscriptProvider(providers), cache)


def build_deps() -> Deps:
    """Deps for the current environment: the shared transcript-provider ladder
    (`build_transcript_chain`) around the environment-selected cache, built
    directly here (R1) — NOT by calling default_deps() and replacing its cache
    afterward, which always constructed (and ran DDL against) a repository-root
    SqliteTranscriptCache even in Postgres mode, before the swap discarded it.
    On Vercel's read-only filesystem outside `/tmp` that stray connection
    attempt can crash startup before a single request is served.
    """
    from chorus.audio import get_audio_renderer
    from chorus.llm import get_llm_client
    from chorus.pipeline import Deps
    from chorus.script import get_script_composer

    cached_provider = build_transcript_chain(select_transcript_cache())

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
    "build_deps",
    "build_transcript_chain",
    "inngest_configured",
    "select_artifact_store",
    "select_job_store",
    "select_key_store",
    "select_persona_registry",
    "select_runner",
    "select_saved_item_store",
    "select_subscription_store",
    "select_transcript_cache",
]
