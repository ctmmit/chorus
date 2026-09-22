"""Contract tests for `JobStore` and `TranscriptCache` implementations: the
same behavior every backend promises (chorus.jobs.JobStore /
chorus.transcript_cache.TranscriptCache Protocols), run against every backend
that implements it.

SQLite backends (SqliteJobStore, SqliteTranscriptCache) run always — no
external dependency. Postgres backends (chorus.stores.postgres.
PostgresJobStore / PostgresTranscriptCache) run only when TEST_DATABASE_URL is
set, so anyone adding a third backend can point this env var at a scratch
DSN and get the same coverage for free.

Point TEST_DATABASE_URL at a throwaway/scratch database, not a shared one:
`test_fail_in_flight_sweeps_only_in_flight_jobs` asserts an exact swept count,
which only holds if nothing else is writing `jobs` rows concurrently.
"""
from __future__ import annotations

import os
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from chorus.jobs import JobStore, SqliteJobStore
from chorus.models import JobStatus, Segment, Transcript
from chorus.transcript_cache import SqliteTranscriptCache, TranscriptCache

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")


def _sqlite_job_store(tmp_path: Path) -> JobStore:
    return SqliteJobStore(tmp_path / f"jobs-{uuid.uuid4().hex}.db")


def _postgres_job_store(tmp_path: Path) -> JobStore:
    from chorus.stores.postgres import PostgresJobStore

    assert TEST_DATABASE_URL is not None
    return PostgresJobStore(TEST_DATABASE_URL)


def _sqlite_cache(tmp_path: Path) -> TranscriptCache:
    return SqliteTranscriptCache(tmp_path / f"cache-{uuid.uuid4().hex}.db")


def _postgres_cache(tmp_path: Path) -> TranscriptCache:
    from chorus.stores.postgres import PostgresTranscriptCache

    assert TEST_DATABASE_URL is not None
    return PostgresTranscriptCache(TEST_DATABASE_URL)


JOB_STORE_FACTORIES: list[tuple[str, Callable[[Path], JobStore]]] = [("sqlite", _sqlite_job_store)]
CACHE_FACTORIES: list[tuple[str, Callable[[Path], TranscriptCache]]] = [("sqlite", _sqlite_cache)]
if TEST_DATABASE_URL:
    JOB_STORE_FACTORIES.append(("postgres", _postgres_job_store))
    CACHE_FACTORIES.append(("postgres", _postgres_cache))


@pytest.fixture(params=[f for _, f in JOB_STORE_FACTORIES], ids=[n for n, _ in JOB_STORE_FACTORIES])
def job_store(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[JobStore]:
    store: JobStore = request.param(tmp_path)
    yield store
    store.close()


@pytest.fixture(params=[f for _, f in CACHE_FACTORIES], ids=[n for n, _ in CACHE_FACTORIES])
def transcript_cache(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[TranscriptCache]:
    cache: TranscriptCache = request.param(tmp_path)
    yield cache
    cache.close()  # type: ignore[attr-defined]  # both implementations define it; not in the Protocol


# --- JobStore contract -------------------------------------------------


def test_create_returns_a_queued_job(job_store: JobStore) -> None:
    job_id = job_store.create()
    job = job_store.get(job_id)
    assert job is not None
    assert job.job_id == job_id
    assert job.status == JobStatus.queued


def test_get_unknown_job_id_returns_none(job_store: JobStore) -> None:
    assert job_store.get(f"missing-{uuid.uuid4().hex}") is None


def test_save_round_trips_full_job_state(job_store: JobStore) -> None:
    job_id = job_store.create()
    job = job_store.get(job_id)
    assert job is not None

    job.status = JobStatus.done
    job.audio_url = "/artifacts/episode_x.mp3"
    job.warnings.append("audio render failed: RuntimeError: simulated")
    job_store.save(job)

    reloaded = job_store.get(job_id)
    assert reloaded is not None
    assert reloaded.status == JobStatus.done
    assert reloaded.audio_url == "/artifacts/episode_x.mp3"
    assert reloaded.warnings == ["audio render failed: RuntimeError: simulated"]


def test_fail_in_flight_sweeps_only_in_flight_jobs(job_store: JobStore) -> None:
    queued_id = job_store.create()

    ready_id = job_store.create()
    ready = job_store.get(ready_id)
    assert ready is not None
    ready.status = JobStatus.digest_ready
    job_store.save(ready)

    done_id = job_store.create()
    done = job_store.get(done_id)
    assert done is not None
    done.status = JobStatus.done
    job_store.save(done)

    swept = job_store.fail_in_flight("interrupted by service restart before completion")

    assert swept == 2
    for job_id in (queued_id, ready_id):
        job = job_store.get(job_id)
        assert job is not None
        assert job.status == JobStatus.failed
        assert job.error == "interrupted by service restart before completion"

    still_done = job_store.get(done_id)
    assert still_done is not None
    assert still_done.status == JobStatus.done
    assert still_done.error is None


# --- R6: windowed sweep + conditional save --------------------------------


def test_fail_in_flight_respects_older_than_seconds_window(job_store: JobStore) -> None:
    """A shared-store (Postgres) deployment only sweeps rows older than
    CHORUS_STALE_JOB_SECONDS — a fresh cold start must not stomp another
    live instance's actively-running job (docs/REVIEW_WAVE1.md #6)."""
    fresh_id = job_store.create()

    swept = job_store.fail_in_flight("should not apply", older_than_seconds=3600)

    assert swept == 0
    job = job_store.get(fresh_id)
    assert job is not None
    assert job.status == JobStatus.queued
    assert job.error is None


def test_fail_in_flight_sweeps_rows_older_than_the_window(job_store: JobStore) -> None:
    stale_id = job_store.create()

    swept = job_store.fail_in_flight("stale", older_than_seconds=0)

    assert swept == 1
    job = job_store.get(stale_id)
    assert job is not None
    assert job.status == JobStatus.failed
    assert job.error == "stale"


def test_save_refuses_to_regress_a_terminal_job_to_a_different_status(job_store: JobStore) -> None:
    """R6: `save` is a conditional write — a stale writer (e.g. a retried
    Inngest step still holding an old in-memory Job) must never be able to
    overwrite a row another writer already finished."""
    job_id = job_store.create()
    finished = job_store.get(job_id)
    assert finished is not None
    finished.status = JobStatus.done
    finished.audio_url = "/artifacts/episode_x.mp3"
    job_store.save(finished)

    stale_writer_view = job_store.get(job_id)
    assert stale_writer_view is not None
    stale_writer_view.status = JobStatus.failed
    stale_writer_view.error = "a stale writer's regression attempt"
    job_store.save(stale_writer_view)

    reloaded = job_store.get(job_id)
    assert reloaded is not None
    assert reloaded.status == JobStatus.done
    assert reloaded.error is None
    assert reloaded.audio_url == "/artifacts/episode_x.mp3"


def test_save_allows_resaving_the_same_terminal_status(job_store: JobStore) -> None:
    """Idempotent finalization (e.g. an Inngest on_failure handler racing a
    successful finish) must still be able to re-save the SAME terminal
    status — only a status CHANGE away from terminal is refused."""
    job_id = job_store.create()
    job = job_store.get(job_id)
    assert job is not None
    job.status = JobStatus.done
    job_store.save(job)

    job.warnings.append("re-finalized")
    job_store.save(job)

    reloaded = job_store.get(job_id)
    assert reloaded is not None
    assert reloaded.status == JobStatus.done
    assert reloaded.warnings == ["re-finalized"]


# --- R3: per-owner counts, used by chorus.quotas.enforce_job_quota -------


def test_count_for_owner_and_count_in_flight(job_store: JobStore) -> None:
    alice_1 = job_store.create(owner="alice")
    job_store.create(owner="alice")
    job_store.create(owner="bob")

    since = datetime.now(UTC) - timedelta(hours=1)
    assert job_store.count_for_owner("alice", since) == 2
    assert job_store.count_for_owner("bob", since) == 1
    assert job_store.count_for_owner("carol", since) == 0
    assert job_store.count_in_flight("alice") == 2

    finishing = job_store.get(alice_1)
    assert finishing is not None
    finishing.status = JobStatus.done
    job_store.save(finishing)

    assert job_store.count_in_flight("alice") == 1
    # count_for_owner counts by creation, regardless of current status.
    assert job_store.count_for_owner("alice", since) == 2


def test_count_for_owner_excludes_jobs_created_before_since(job_store: JobStore) -> None:
    job_store.create(owner="alice")
    future_since = datetime.now(UTC) + timedelta(hours=1)
    assert job_store.count_for_owner("alice", future_since) == 0


# --- TranscriptCache contract -------------------------------------------


def test_cache_miss_returns_none(transcript_cache: TranscriptCache) -> None:
    assert transcript_cache.get(f"missing-{uuid.uuid4().hex}") is None


def test_cache_put_then_get_round_trips(transcript_cache: TranscriptCache) -> None:
    video_id = f"vid-{uuid.uuid4().hex}"
    transcript = Transcript(
        video_id=video_id,
        segments=[Segment(start=0.0, text="hello"), Segment(start=90.0, text="world")],
        source="fixture",
    )
    transcript_cache.put(transcript)

    cached = transcript_cache.get(video_id)
    assert cached is not None
    assert cached.video_id == video_id
    assert [s.text for s in cached.segments] == ["hello", "world"]
    assert cached.source == "fixture"


def test_cache_put_overwrites_existing_entry(transcript_cache: TranscriptCache) -> None:
    video_id = f"vid-{uuid.uuid4().hex}"
    transcript_cache.put(
        Transcript(video_id=video_id, segments=[Segment(start=0.0, text="v1")], source="fixture")
    )
    transcript_cache.put(
        Transcript(video_id=video_id, segments=[Segment(start=0.0, text="v2")], source="rss:json")
    )

    cached = transcript_cache.get(video_id)
    assert cached is not None
    assert cached.segments[0].text == "v2"
    assert cached.source == "rss:json"
