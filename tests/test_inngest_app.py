"""chorus/inngest_app.py: the Inngest function BODY (`run_digest_body`),
exercised with a fake `step` (an object whose async `run(step_id, handler)`
just awaits `handler()` — no dev server, no network, no real Inngest
execution semantics beyond "run this and remember nothing"). This is exactly
what the module docstring promises is testable this way.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from chorus.artifacts import LocalArtifactStore
from chorus.audio import MockAudioRenderer
from chorus.inngest_app import (
    _job_id_from_failure_event_data,
    finalize_after_exhausted_retries,
    run_digest_body,
)
from chorus.jobs import SqliteJobStore
from chorus.llm import LLMError, MockLLMClient
from chorus.models import DigestRequest, EpisodeInput, JobStatus
from chorus.pipeline import Deps
from chorus.script import MockScriptComposer
from chorus.transcripts import FixtureTranscriptProvider

FIX = Path(__file__).resolve().parent.parent / "fixtures"
SAMPLE = "sample_public"
MISSING = "does-not-exist"


class _FakeStep:
    """`run(step_id, handler)` just awaits `handler()` immediately — no
    memoization, no retries. Good enough to prove run_digest_body's control
    flow (ingest -> curate-per-episode -> script -> audio -> done)."""

    async def run(self, step_id: str, handler: Any) -> Any:
        return await handler()


def _request(*video_ids: str) -> DigestRequest:
    return DigestRequest(
        soul=(FIX / "souls" / "soul_investor.md").read_text(encoding="utf-8"),
        context="",
        episodes=[EpisodeInput(video_id=v) for v in video_ids],
        highlight_count=4,
    )


def _deps(tmp_path: Path) -> Deps:
    return Deps(
        FixtureTranscriptProvider(),
        MockLLMClient(),
        MockScriptComposer(),
        MockAudioRenderer(),
        LocalArtifactStore(tmp_path / "artifacts"),
    )


def test_run_digest_body_reaches_done_with_digest_script_audio(tmp_path: Path) -> None:
    store = SqliteJobStore(tmp_path / "jobs.db")
    job_id = store.create()
    request = _request(SAMPLE)
    event_data = {"job_id": job_id, "request": request.model_dump(mode="json")}

    asyncio.run(run_digest_body(_FakeStep(), event_data, store, _deps(tmp_path)))

    job = store.get(job_id)
    assert job is not None
    assert job.status == JobStatus.done
    assert job.digest is not None and job.digest.episodes
    assert job.script is not None
    assert job.audio_url == f"/artifacts/episode_{job_id}.txt"
    assert job.error is None


def test_run_digest_body_all_transcripts_failed_ends_failed(tmp_path: Path) -> None:
    store = SqliteJobStore(tmp_path / "jobs.db")
    job_id = store.create()
    request = _request(MISSING)
    event_data = {"job_id": job_id, "request": request.model_dump(mode="json")}

    asyncio.run(run_digest_body(_FakeStep(), event_data, store, _deps(tmp_path)))

    job = store.get(job_id)
    assert job is not None
    assert job.status == JobStatus.failed
    assert job.error


def test_run_digest_body_unknown_job_id_logs_and_returns(tmp_path: Path) -> None:
    store = SqliteJobStore(tmp_path / "jobs.db")
    request = _request(SAMPLE)
    event_data = {"job_id": "never-created", "request": request.model_dump(mode="json")}

    # Must not raise: there's no job to mark failed, so it just returns.
    asyncio.run(run_digest_body(_FakeStep(), event_data, store, _deps(tmp_path)))
    assert store.get("never-created") is None


# --- R7: retryable vs terminal classification -------------------------


class _RetryableLLM:
    """LLMError is classified retryable (chorus.errors.is_retryable)."""

    def score_segment(self, text: str, soul: str, context: str) -> tuple[float, str]:
        raise LLMError("simulated transient LLM outage")

    def score_windows(self, windows, soul, context, meter=None):  # type: ignore[no-untyped-def]
        raise LLMError("simulated transient LLM outage")


class _TerminalLLM:
    """A plain ValueError is classified terminal."""

    def score_segment(self, text: str, soul: str, context: str) -> tuple[float, str]:
        raise ValueError("malformed request")

    def score_windows(self, windows, soul, context, meter=None):  # type: ignore[no-untyped-def]
        raise ValueError("malformed request")


def test_run_digest_body_reraises_retryable_failures_with_a_diagnostic(tmp_path: Path) -> None:
    store = SqliteJobStore(tmp_path / "jobs.db")
    job_id = store.create()
    request = _request(SAMPLE)
    event_data = {"job_id": job_id, "request": request.model_dump(mode="json")}
    deps = _deps(tmp_path)
    deps.llm = _RetryableLLM()

    with pytest.raises(LLMError):
        asyncio.run(run_digest_body(_FakeStep(), event_data, store, deps))

    job = store.get(job_id)
    assert job is not None
    # Left in flight (never marked failed) so Inngest's retry of the whole
    # function can pick the job back up.
    assert job.status == JobStatus.queued
    assert job.error is None
    assert any("retryable failure" in w for w in job.warnings)


def test_run_digest_body_terminal_failure_marks_failed_without_reraising(tmp_path: Path) -> None:
    store = SqliteJobStore(tmp_path / "jobs.db")
    job_id = store.create()
    request = _request(SAMPLE)
    event_data = {"job_id": job_id, "request": request.model_dump(mode="json")}
    deps = _deps(tmp_path)
    deps.llm = _TerminalLLM()

    # Must NOT raise: Inngest acknowledges (no retry) a deterministic failure.
    asyncio.run(run_digest_body(_FakeStep(), event_data, store, deps))

    job = store.get(job_id)
    assert job is not None
    assert job.status == JobStatus.failed
    assert "malformed request" in job.error


def test_finalize_after_exhausted_retries_marks_in_flight_job_failed(tmp_path: Path) -> None:
    store = SqliteJobStore(tmp_path / "jobs.db")
    job_id = store.create()

    finalize_after_exhausted_retries(store, job_id)

    job = store.get(job_id)
    assert job is not None
    assert job.status == JobStatus.failed
    assert "exhausted retries" in job.error


def test_finalize_after_exhausted_retries_is_a_noop_for_an_already_terminal_job(tmp_path: Path) -> None:
    store = SqliteJobStore(tmp_path / "jobs.db")
    job_id = store.create()
    job = store.get(job_id)
    assert job is not None
    job.status = JobStatus.done
    store.save(job)

    finalize_after_exhausted_retries(store, job_id)

    job = store.get(job_id)
    assert job is not None
    assert job.status == JobStatus.done


def test_finalize_after_exhausted_retries_handles_missing_job_id() -> None:
    # Never raises even when the failure event's job_id can't be resolved.
    finalize_after_exhausted_retries(SqliteJobStore(":memory:"), None)


def test_job_id_from_failure_event_data_nested_shape() -> None:
    data = {"event": {"data": {"job_id": "abc123"}}, "error": {"message": "boom"}}
    assert _job_id_from_failure_event_data(data) == "abc123"


def test_job_id_from_failure_event_data_flat_fallback_shape() -> None:
    assert _job_id_from_failure_event_data({"job_id": "flat123"}) == "flat123"


def test_job_id_from_failure_event_data_unresolvable_shape_returns_none() -> None:
    assert _job_id_from_failure_event_data({"unexpected": True}) is None
