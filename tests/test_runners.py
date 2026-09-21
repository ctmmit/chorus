"""chorus/runners.py: BackgroundRunner (in-process, unchanged behavior) and
InngestRunner.submit (records the event on a fake client — no dev server, no
network)."""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import BackgroundTasks

from chorus.artifacts import LocalArtifactStore
from chorus.audio import MockAudioRenderer
from chorus.jobs import SqliteJobStore
from chorus.llm import MockLLMClient
from chorus.models import DigestRequest, EpisodeInput, JobStatus
from chorus.pipeline import Deps
from chorus.runners import INNGEST_DIGEST_EVENT, BackgroundRunner, InngestRunner
from chorus.script import MockScriptComposer
from chorus.transcripts import FixtureTranscriptProvider

FIX = Path(__file__).resolve().parent.parent / "fixtures"
SAMPLE = "sample_public"


def _request() -> DigestRequest:
    return DigestRequest(
        soul=(FIX / "souls" / "soul_investor.md").read_text(encoding="utf-8"),
        context="",
        episodes=[EpisodeInput(video_id=SAMPLE)],
        highlight_count=4,
    )


def test_background_runner_requires_background_tasks(tmp_path: Path) -> None:
    store = SqliteJobStore(tmp_path / "jobs.db")
    deps = Deps(
        FixtureTranscriptProvider(),
        MockLLMClient(),
        MockScriptComposer(),
        MockAudioRenderer(),
        LocalArtifactStore(tmp_path / "artifacts"),
    )
    runner = BackgroundRunner(store, deps)
    job_id = store.create()
    with pytest.raises(RuntimeError, match="BackgroundTasks"):
        runner.submit(job_id, _request())


def test_background_runner_runs_job_via_background_tasks(tmp_path: Path) -> None:
    store = SqliteJobStore(tmp_path / "jobs.db")
    deps = Deps(
        FixtureTranscriptProvider(),
        MockLLMClient(),
        MockScriptComposer(),
        MockAudioRenderer(),
        LocalArtifactStore(tmp_path / "artifacts"),
    )
    runner = BackgroundRunner(store, deps)
    job_id = store.create()
    background = BackgroundTasks()
    runner.submit(job_id, _request(), background)
    # BackgroundTasks defers execution; run it now, as Starlette does after
    # the response is sent.
    import asyncio

    asyncio.run(background())

    job = store.get(job_id)
    assert job is not None
    assert job.status == JobStatus.done


class _FakeInngestClient:
    def __init__(self) -> None:
        self.sent: list[object] = []

    def send_sync(self, event: object) -> list[str]:
        self.sent.append(event)
        return ["evt_fake"]


def test_inngest_runner_sends_event_with_job_id_and_request() -> None:
    client = _FakeInngestClient()
    runner = InngestRunner(client)
    request = _request()

    runner.submit("job-123", request)

    assert len(client.sent) == 1
    event = client.sent[0]
    assert event.name == INNGEST_DIGEST_EVENT  # type: ignore[attr-defined]
    assert event.data["job_id"] == "job-123"  # type: ignore[attr-defined]
    assert event.data["request"]["soul"] == request.soul  # type: ignore[attr-defined]


def test_inngest_runner_ignores_background_tasks_argument() -> None:
    client = _FakeInngestClient()
    runner = InngestRunner(client)
    # A BackgroundTasks instance may be passed (the route always has one) —
    # InngestRunner must simply ignore it, not error.
    runner.submit("job-123", _request(), BackgroundTasks())
    assert len(client.sent) == 1
