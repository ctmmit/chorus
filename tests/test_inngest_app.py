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

from chorus.artifacts import LocalArtifactStore
from chorus.audio import MockAudioRenderer
from chorus.inngest_app import run_digest_body
from chorus.jobs import SqliteJobStore
from chorus.llm import MockLLMClient
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
