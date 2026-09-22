"""The spine on the redistributable synthetic transcript. Runs everywhere,
including a public-repo CI that has no access to the private show fixtures."""
from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from chorus.app import create_app
from chorus.artifacts import LocalArtifactStore
from chorus.audio import MockAudioRenderer
from chorus.curation import citation_resolves, curate_episode
from chorus.ingest import ingest
from chorus.jobs import SqliteJobStore
from chorus.llm import MockLLMClient
from chorus.models import EpisodeInput
from chorus.pipeline import Deps
from chorus.script import MockScriptComposer
from chorus.transcripts import FixtureTranscriptProvider

FIX = Path(__file__).resolve().parent.parent / "fixtures"
SAMPLE = "sample_public"


def _soul(name: str) -> str:
    return (FIX / "souls" / name).read_text(encoding="utf-8")


def test_sample_resolves_and_surfaces_investor_highlights() -> None:
    resolved = ingest([EpisodeInput(video_id=SAMPLE)], FixtureTranscriptProvider()).resolved[0]
    ep = curate_episode(resolved, _soul("soul_investor.md"), "", MockLLMClient())
    assert not ep.refused
    assert ep.highlights
    for h in ep.highlights:
        assert citation_resolves(resolved.transcript, h.segment_timestamp, h.quote)
    # The off-topic chatter window (starts at 90.5s) must never surface.
    assert all(round(h.segment_timestamp) != 90 for h in ep.highlights)
    # Every window is reported for the viewer, surfaced or not, plus duration.
    assert len(ep.windows) == 3
    assert ep.duration_seconds == 219.0
    chatter = next(w for w in ep.windows if round(w.start) == 90)
    assert chatter.score < min(h.relevance_score for h in ep.highlights)


def test_sample_full_lifecycle_via_api(tmp_path: Path) -> None:
    deps = Deps(
        provider=FixtureTranscriptProvider(),
        llm=MockLLMClient(),
        composer=MockScriptComposer(),
        renderer=MockAudioRenderer(out_dir=tmp_path / "artifacts"),
        artifacts=LocalArtifactStore(tmp_path / "artifacts"),
    )
    client = TestClient(create_app(SqliteJobStore(tmp_path / "jobs.db"), deps))
    payload = {
        "soul": _soul("soul_investor.md"),
        "context": "",
        "episodes": [{"video_id": SAMPLE}],
    }
    job_id = client.post("/digest", json=payload).json()["job_id"]
    body = client.get(f"/digest/{job_id}").json()
    assert body["status"] == "done"
    assert body["digest"]["episodes"][0]["highlights"]
    assert body["script"]["takes"]
    assert body["audio_url"]


def test_mock_audio_is_flagged_as_placeholder_and_titles_are_enriched(tmp_path: Path) -> None:
    """Cold-agent dogfood findings (docs/cold-agent-log.md): a dev instance's
    .txt audio must be announced in `warnings`, and a bare catalog video id
    must come back titled."""
    deps = Deps(
        provider=FixtureTranscriptProvider(),
        llm=MockLLMClient(),
        composer=MockScriptComposer(),
        renderer=MockAudioRenderer(out_dir=tmp_path / "artifacts"),
        artifacts=LocalArtifactStore(tmp_path / "artifacts"),
    )
    client = TestClient(create_app(SqliteJobStore(tmp_path / "jobs.db"), deps))
    catalog_id = "c4tvVKDhpiY"
    if not (FIX / "transcripts" / f"{catalog_id}.json").exists():
        catalog_id = SAMPLE  # private fixtures absent: title enrichment not exercised
    payload = {"soul": _soul("soul_investor.md"), "context": "", "episodes": [{"video_id": catalog_id}]}
    job_id = client.post("/digest", json=payload).json()["job_id"]
    body = client.get(f"/digest/{job_id}").json()
    assert body["status"] == "done"
    assert body["audio_url"].endswith(".txt")
    assert any("placeholder" in w for w in body["warnings"])
    if catalog_id != SAMPLE:
        assert body["digest"]["episodes"][0]["episode_title"]


def test_agent_card_url_follows_the_request_host(tmp_path: Path, monkeypatch: object) -> None:
    import pytest

    assert isinstance(monkeypatch, pytest.MonkeyPatch)
    monkeypatch.delenv("CHORUS_PUBLIC_URL", raising=False)
    app = create_app(SqliteJobStore(tmp_path / "jobs.db"))
    client = TestClient(app, base_url="http://chorus.example:8765")
    card = client.get("/.well-known/agent.json").json()
    assert card["url"] == "http://chorus.example:8765"
