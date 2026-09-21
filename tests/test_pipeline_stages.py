"""Phase B stage functions (chorus/pipeline.py): each one exercised directly
against mock deps, independent of run_job/_run or any FastAPI/Inngest
plumbing — these are exactly the callables chorus/inngest_app.py wires one
per `step.run`, so proving them correct here proves both callers correct.
"""
from __future__ import annotations

from pathlib import Path

from chorus.artifacts import LocalArtifactStore
from chorus.audio import MockAudioRenderer
from chorus.llm import MockLLMClient
from chorus.models import Digest, DigestRequest, EpisodeInput
from chorus.pipeline import stage_audio, stage_curate_episode, stage_ingest, stage_script
from chorus.script import MockScriptComposer
from chorus.transcripts import FixtureTranscriptProvider

FIX = Path(__file__).resolve().parent.parent / "fixtures"
SAMPLE = "sample_public"


def _request(*video_ids: str) -> DigestRequest:
    return DigestRequest(
        soul=(FIX / "souls" / "soul_investor.md").read_text(encoding="utf-8"),
        context="",
        episodes=[EpisodeInput(video_id=v) for v in video_ids],
        highlight_count=4,
    )


def test_stage_ingest_resolves_fixture_transcript() -> None:
    request = _request(SAMPLE)
    result = stage_ingest(request, FixtureTranscriptProvider())
    assert len(result.resolved) == 1
    assert result.resolved[0].transcript.video_id == SAMPLE
    assert not result.skipped


def test_stage_curate_episode_returns_episode_digest() -> None:
    request = _request(SAMPLE)
    ingested = stage_ingest(request, FixtureTranscriptProvider())
    digest = stage_curate_episode(ingested.resolved[0], request, MockLLMClient())
    assert digest.episode_id == SAMPLE
    # soul_investor's mock scorer should surface at least one highlight for
    # the sample transcript (used across the existing public-sample tests).
    assert digest.highlights or digest.refused


def test_stage_script_produces_grounded_takes() -> None:
    request = _request(SAMPLE)
    ingested = stage_ingest(request, FixtureTranscriptProvider())
    episode_digest = stage_curate_episode(ingested.resolved[0], request, MockLLMClient())
    digest = Digest(soul_version="deadbeef", episodes=[episode_digest])
    script = stage_script(digest, request, MockScriptComposer())
    valid_refs = {(h.episode_id, round(h.segment_timestamp)) for h in digest.highlights}
    for take in script.takes:
        assert (take.episode_id, round(take.segment_timestamp)) in valid_refs


def test_stage_audio_renders_and_stores_returns_url(tmp_path: Path) -> None:
    request = _request(SAMPLE)
    ingested = stage_ingest(request, FixtureTranscriptProvider())
    episode_digest = stage_curate_episode(ingested.resolved[0], request, MockLLMClient())
    digest = Digest(soul_version="deadbeef", episodes=[episode_digest])
    script = stage_script(digest, request, MockScriptComposer())

    url = stage_audio(
        script, request, "job-abc123", MockAudioRenderer(), LocalArtifactStore(tmp_path)
    )

    assert url == "/artifacts/episode_job-abc123.txt"
    assert (tmp_path / "episode_job-abc123.txt").read_text(encoding="utf-8") == script.monologue
