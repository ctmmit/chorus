"""Goal 6 — Layer 2 pick-and-choose: catalog + selection endpoint."""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from chorus import catalog
from chorus.app import create_app
from chorus.artifacts import LocalArtifactStore
from chorus.audio import MockAudioRenderer
from chorus.jobs import SqliteJobStore
from chorus.llm import MockLLMClient
from chorus.pipeline import Deps
from chorus.script import MockScriptComposer
from chorus.transcripts import FixtureTranscriptProvider

FIX = Path(__file__).resolve().parent.parent / "fixtures"
ANDREESSEN = "c4tvVKDhpiY"
ANDREESSEN_SHOW = "20VC with Harry Stebbings"


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    deps = Deps(
        provider=FixtureTranscriptProvider(),
        llm=MockLLMClient(),
        composer=MockScriptComposer(),
        renderer=MockAudioRenderer(out_dir=tmp_path / "artifacts"),
        artifacts=LocalArtifactStore(tmp_path / "artifacts"),
    )
    return TestClient(create_app(SqliteJobStore(tmp_path / "jobs.db"), deps))


def _creds() -> dict:
    return {
        "soul": (FIX / "souls" / "soul_investor.md").read_text(encoding="utf-8"),
        "context": (FIX / "context.md").read_text(encoding="utf-8"),
    }


def test_resolve_by_video_id() -> None:
    eps = catalog.resolve(video_ids=[ANDREESSEN])
    assert [e.video_id for e in eps] == [ANDREESSEN]


def test_resolve_by_show() -> None:
    eps = catalog.resolve(shows=[ANDREESSEN_SHOW])
    assert any(e.video_id == ANDREESSEN for e in eps)


def test_resolve_empty_selection() -> None:
    assert catalog.resolve() == []


def test_shows_endpoint_lists_catalog(client: TestClient) -> None:
    shows = client.get("/shows").json()
    assert any(s["show"] == ANDREESSEN_SHOW for s in shows)


def test_select_by_show_runs_to_done(client: TestClient) -> None:
    payload = {**_creds(), "shows": [ANDREESSEN_SHOW]}
    job_id = client.post("/digest/select", json=payload).json()["job_id"]
    body = client.get(f"/digest/{job_id}").json()
    assert body["status"] == "done"
    assert body["digest"]["episodes"]


def test_empty_selection_is_400(client: TestClient) -> None:
    payload = {**_creds(), "shows": ["No Such Show"]}
    assert client.post("/digest/select", json=payload).status_code == 400
