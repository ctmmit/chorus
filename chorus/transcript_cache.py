"""Transcript caching, behind its own Protocol so a live provider chain isn't
re-fetching (and re-paying for) the same episode on every request.

SQLite today, in the same on-disk DB as `chorus.jobs.JobStore` but its own
table — mirrors that store's threading model (one shared connection, one
lock; BackgroundTasks may run the pipeline on a worker thread). A Postgres
cache will be added later (Phase B's store migration) behind this same
Protocol, so `CachingTranscriptProvider` and every caller stay unchanged.

R11 (docs/REVIEW_WAVE1.md #11): cache keys are namespaced by source family
(`yt:<id>` for YouTube, `rss:<id>` for RSS) inside this module, without
changing the `TranscriptCache` Protocol's `get(episode_id)`/`put(transcript)`
shape — a YouTube video id and an RSS hash id are drawn from disjoint
namespaces by construction, but the explicit prefix means a lookup can never
silently cross a family boundary even if that stopped being true.
`CachingTranscriptProvider` additionally refuses to cache (or return from a
lookup by a mismatched id) a transcript whose `video_id` does not match the
identity it was fetched for — the second half of R11's poisoning fix,
alongside `ChainTranscriptProvider`'s equivalent check.
"""
from __future__ import annotations

import logging
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol, runtime_checkable

from chorus.jobs import DEFAULT_DB
from chorus.models import EpisodeInput, Transcript
from chorus.transcripts import TranscriptProvider, TranscriptProviderError

log = logging.getLogger("chorus.transcript_cache")

CACHE_TABLE = "transcripts"


def _namespaced_key(episode_id: str) -> str:
    """`rss-<hash>` ids are namespaced `rss:`, everything else (YouTube video
    ids) `yt:` — an explicit prefix rather than relying solely on the
    `rss-` string convention staying collision-free forever."""
    family = "rss" if episode_id.startswith("rss-") else "yt"
    return f"{family}:{episode_id}"


@runtime_checkable
class TranscriptCache(Protocol):
    def get(self, episode_id: str) -> Transcript | None: ...
    def put(self, transcript: Transcript) -> None: ...


class SqliteTranscriptCache:
    """`transcripts(episode_id TEXT PRIMARY KEY, payload TEXT NOT NULL,
    fetched_at TEXT NOT NULL)` in the shared chorus.db by default."""

    def __init__(self, db_path: Path | str = DEFAULT_DB) -> None:
        self.db_path = str(db_path)
        # check_same_thread=False to match JobStore: BackgroundTasks may run
        # the pipeline (and therefore cache access) on a worker thread.
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock:
            self._conn.execute(
                f"CREATE TABLE IF NOT EXISTS {CACHE_TABLE} "
                "(episode_id TEXT PRIMARY KEY, payload TEXT NOT NULL, fetched_at TEXT NOT NULL)"
            )
            self._conn.commit()

    def get(self, episode_id: str) -> Transcript | None:
        key = _namespaced_key(episode_id)
        with self._lock:
            row = self._conn.execute(
                f"SELECT payload FROM {CACHE_TABLE} WHERE episode_id = ?", (key,)
            ).fetchone()
        return Transcript.model_validate_json(row[0]) if row else None

    def put(self, transcript: Transcript) -> None:
        key = _namespaced_key(transcript.video_id)
        fetched_at = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self._conn.execute(
                f"INSERT OR REPLACE INTO {CACHE_TABLE} (episode_id, payload, fetched_at) "
                "VALUES (?, ?, ?)",
                (key, transcript.model_dump_json(), fetched_at),
            )
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()


class CachingTranscriptProvider:
    """Wraps a TranscriptProvider (typically a ChainTranscriptProvider): a
    cache hit skips `inner` entirely; a miss calls `inner` and stores the
    result before returning it."""

    def __init__(self, inner: TranscriptProvider, cache: TranscriptCache) -> None:
        self.inner = inner
        self.cache = cache

    def get(self, episode: EpisodeInput) -> Transcript:
        episode_id = episode.resolved_id()
        cached = self.cache.get(episode_id)
        if cached is not None:
            return cached
        transcript = self.inner.get(episode)
        if transcript.video_id != episode_id:
            # R11: never poison the cache under the requested id with a
            # transcript that resolved to a different one.
            log.error(
                "transcript_cache: refusing to cache mismatched transcript "
                "(got video_id=%r, requested %r)",
                transcript.video_id,
                episode_id,
            )
            raise TranscriptProviderError(
                f"provider returned video_id {transcript.video_id!r} for requested {episode_id!r}"
            )
        self.cache.put(transcript)
        return transcript
