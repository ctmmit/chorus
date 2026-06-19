"""Goal 3.3 — async lifecycle end to end via the API, offline (fixtures + mocks).

TestClient runs background tasks before returning, so the job is terminal by the
time we poll.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from chorus.app import create_app
from chorus.audio import MockAudioRenderer
from chorus.jobs import JobStore
from chorus.llm import MockLLMClient
from chorus.pipeline import Deps
from chorus.script import MockScriptComposer
from chorus.transcripts import FixtureTranscriptProvider

FIX = Path(__file__).resolve().parent.parent / "fixtures"
ANDREESSEN = "c4tvVKDhpiY"
GOLDMAN_MISSING = "KhZfxZ-C-2g"


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    deps = Deps(
        provider=FixtureTranscriptProvider(),
        llm=MockLLMClient(),
        composer=MockScriptComposer(),
        renderer=MockAudioRenderer(out_dir=tmp_path / "artifacts"),
    )
    return TestClient(create_app(JobStore(tmp_path / "jobs.db"), deps))


def _payload(*video_ids: str) -> dict:
    return {
        "soul": (FIX / "souls" / "soul_investor.md").read_text(encoding="utf-8"),
        "context": (FIX / "context.md").read_text(encoding="utf-8"),
        "episodes": [{"video_id": v} for v in video_ids],
        "highlight_count": 4,
    }


def test_post_returns_job_id(client: TestClient) -> None:
    r = client.post("/digest", json=_payload(ANDREESSEN))
    assert r.status_code == 200
    assert r.json()["job_id"]


def test_full_lifecycle_reaches_done(client: TestClient) -> None:
    job_id = client.post("/digest", json=_payload(ANDREESSEN)).json()["job_id"]
    body = client.get(f"/digest/{job_id}").json()
    assert body["status"] == "done"
    assert body["digest"]["episodes"][0]["highlights"]
    assert body["audio_url"]


def test_all_transcripts_fail_returns_failed(client: TestClient) -> None:
    # The flagged critical gap: must be failed with a reason, never empty done.
    job_id = client.post("/digest", json=_payload(GOLDMAN_MISSING)).json()["job_id"]
    body = client.get(f"/digest/{job_id}").json()
    assert body["status"] == "failed"
    assert body["error"]


def test_unknown_job_id_404(client: TestClient) -> None:
    assert client.get("/digest/does-not-exist").status_code == 404
