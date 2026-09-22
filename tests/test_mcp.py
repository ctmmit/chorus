from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from chorus.app import create_app
from chorus.audio import MockAudioRenderer
from chorus.jobs import SqliteJobStore
from chorus.llm import MockLLMClient
from chorus.mcp_server import ChorusTools
from chorus.models import EpisodeInput, Job, JobStatus
from chorus.pipeline import Deps
from chorus.script import MockScriptComposer
from chorus.transcripts import FixtureTranscriptProvider

FIX = Path(__file__).resolve().parent.parent / "fixtures"
ANDREESSEN = "c4tvVKDhpiY"


def _deps(tmp_path: Path) -> Deps:
    return Deps(
        provider=FixtureTranscriptProvider(),
        llm=MockLLMClient(),
        composer=MockScriptComposer(),
        renderer=MockAudioRenderer(out_dir=tmp_path / "artifacts"),
    )


def _await_terminal(tools: ChorusTools, job_id: str, timeout: float = 5.0) -> Job:
    # R9: BackgroundRunner.submit(background=None) runs on a daemon thread,
    # so submission returns before the job is done — poll like a real caller
    # (chorus/mcp_server.py's docstring: "poll the returned job id").
    deadline = time.monotonic() + timeout
    job = tools.get_digest(job_id)
    while job.status in (JobStatus.queued, JobStatus.digest_ready) and time.monotonic() < deadline:
        time.sleep(0.02)
        job = tools.get_digest(job_id)
    return job


def test_submit_digest_tool_reaches_done(tmp_path: Path) -> None:
    store = SqliteJobStore(tmp_path / "jobs.db")
    tools = ChorusTools(store, _deps(tmp_path))
    submitted = tools.submit_digest(
        soul=(FIX / "souls" / "soul_investor.md").read_text(encoding="utf-8"),
        context=(FIX / "context.md").read_text(encoding="utf-8"),
        episodes=[EpisodeInput(video_id=ANDREESSEN)],
    )
    assert submitted.keys() == {"job_id"}  # R9: returns immediately, no digest yet

    job = _await_terminal(tools, submitted["job_id"])

    assert job.status == "done"
    assert job.digest is not None
    assert job.digest.episodes[0].highlights


def test_mcp_mount_is_protected_by_master_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The MCP session manager starts in the app lifespan, so use `with` (the
    # real SDK raises on a request that arrives before run() was entered).
    for k in ("ANTHROPIC_API_KEY", "ELEVENLABS_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    app = create_app(SqliteJobStore(tmp_path / "jobs.db"), _deps(tmp_path), api_token="s3cret")
    with TestClient(app) as client:
        assert client.get("/mcp").status_code == 401
        authorized = client.get("/mcp", headers={"Authorization": "Bearer s3cret"})
        assert authorized.status_code != 401


def test_list_shows_and_submit_selection(tmp_path: Path) -> None:
    tools = ChorusTools(SqliteJobStore(tmp_path / "jobs.db"), _deps(tmp_path))

    shows = tools.list_shows()
    submitted = tools.submit_selection(
        soul=(FIX / "souls" / "soul_investor.md").read_text(encoding="utf-8"),
        context=(FIX / "context.md").read_text(encoding="utf-8"),
        video_ids=[ANDREESSEN],
    )

    assert shows
    assert _await_terminal(tools, submitted["job_id"]).status == "done"


def test_build_soul_from_interview_uses_expected_sections(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    tools = ChorusTools(SqliteJobStore(tmp_path / "jobs.db"), _deps(tmp_path))

    soul = tools.build_soul_from_interview(
        {
            "identity": "Public-markets investor",
            "interests": "semiconductors, capital allocation",
            "triggers": "channel checks",
            "ignore": "generic motivation",
            "style": "Empirical and concise",
            "guidance": "Only surface decision-relevant evidence.",
        }
    )

    for section in (
        "Identity & Role",
        "Core Interests",
        "Attention Triggers",
        "Anti-interests",
        "Taste & Sensibility",
        "Curation Guidance",
    ):
        assert f"## {section}" in soul
