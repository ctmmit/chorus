"""Transcript caching, behind its own Protocol so a live provider chain isn't
re-fetching (and re-paying for) the same episode on every request.

SQLite today, in the same on-disk DB as `chorus.jobs.JobStore` but its own
table — mirrors that store's threading model (one shared connection, one
lock; BackgroundTasks may run the pipeline on a worker thread). A Postgres
cache will be added later (Phase B's store migration) behind this same
Protocol, so `CachingTranscriptProvider` and every caller stay unchanged.
"""
from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol, runtime_checkable

from chorus.jobs import DEFAULT_DB
from chorus.models import EpisodeInput, Transcript
from chorus.transcripts import TranscriptProvider

CACHE_TABLE = "transcripts"


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
        with self._lock:
            row = self._conn.execute(
                f"SELECT payload FROM {CACHE_TABLE} WHERE episode_id = ?", (episode_id,)
            ).fetchone()
        return Transcript.model_validate_json(row[0]) if row else None

    def put(self, transcript: Transcript) -> None:
        fetched_at = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self._conn.execute(
                f"INSERT OR REPLACE INTO {CACHE_TABLE} (episode_id, payload, fetched_at) "
                "VALUES (?, ?, ?)",
                (transcript.video_id, transcript.model_dump_json(), fetched_at),
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
        self.cache.put(transcript)
        return transcript
