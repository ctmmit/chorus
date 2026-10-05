"""The deferred 0.3 items: the "proposal ready" email line, voiced answers,
and Readwise context for local runs."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from chorus.app import create_app
from chorus.artifacts import LocalArtifactStore, job_id_from_artifact_name
from chorus.ask import AnswerCitation, AnswerSentence, AskAnswer, answer_script, voice_answer
from chorus.audio import MockAudioRenderer, RenderedAudio
from chorus.context import MockContextProvider
from chorus.feedback import (
    MIN_RATINGS_FOR_PROPOSAL,
    Rating,
    SoulProposal,
    SqliteFeedbackStore,
    proposal_nudge,
    summarize_feedback,
)
from chorus.jobs import MASTER_OWNER, SqliteJobStore
from chorus.llm import MockLLMClient
from chorus.local_run import local_context_blocks
from chorus.models import (
    ContextItem,
    Digest,
    EpisodeDigest,
    EpisodeInput,
    Job,
    JobStatus,
    Script,
)
from chorus.pipeline import Deps
from chorus.script import MockScriptComposer
from chorus.transcripts import FixtureTranscriptProvider

NOW = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)
TOKEN = "s3cret"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
FRAME = bytes([0xFF, 0xFB, 0x90, 0x00]) + bytes(413)


# --- the "proposal ready" line ---------------------------------------------------


def _rating(minutes: int) -> Rating:
    return Rating(owner=MASTER_OWNER, job_id=f"j{minutes}", highlight_id="h", vote="down",
                  rated_at=NOW + timedelta(minutes=minutes), episode_id="e")


def _proposal(at: datetime) -> SoulProposal:
    return SoulProposal(proposal_id="p", owner=MASTER_OWNER, base_soul_version="v", edits=[],
                        summary=summarize_feedback([]), created_at=at)


def test_nudge_needs_enough_ratings_since_the_last_proposal() -> None:
    enough = [_rating(i) for i in range(MIN_RATINGS_FOR_PROPOSAL)]
    assert proposal_nudge(enough[:-1], None) is None
    note = proposal_nudge(enough, None)
    assert note is not None and f"rated {MIN_RATINGS_FOR_PROPOSAL} highlights" in note
    # A proposal made after those ratings silences the line until new ones pile up.
    assert proposal_nudge(enough, _proposal(NOW + timedelta(days=1))) is None


def test_latest_proposal_is_per_owner_and_newest(tmp_path: Path) -> None:
    store = SqliteFeedbackStore(tmp_path / "f.db")
    older, newer = _proposal(NOW), _proposal(NOW + timedelta(days=2))
    store.save_proposal(older)
    store.save_proposal(newer.model_copy(update={"proposal_id": "p2"}))
    store.save_proposal(older.model_copy(update={"proposal_id": "p3", "owner": "pat"}))
    latest = store.latest_proposal(MASTER_OWNER)
    assert latest is not None and latest.proposal_id == "p2"
    assert store.latest_proposal("nobody") is None
    store.close()


def test_digest_email_carries_the_note() -> None:
    from chorus.digest_email import render_digest_email
    from chorus.subscriptions import Subscription

    job = Job(job_id="j", status=JobStatus.done,
              digest=Digest(soul_version="v", episodes=[
                  EpisodeDigest(episode_id="e", episode_title="T", highlights=[])]))
    sub = Subscription(subscription_id="s", owner=MASTER_OWNER, email="p@example.com",
                       soul="soul", context="", episodes=[EpisodeInput(video_id="e")],
                       next_run_at=NOW, created_at=NOW)
    content = render_digest_email(job, sub, "https://c", "https://u", feedback_note="Rate more.")
    assert "Rate more." in content.text and "Rate more." in content.html
    plain = render_digest_email(job, sub, "https://c", "https://u")
    assert "Rate more." not in plain.text


# --- voiced answers ---------------------------------------------------------------


def _answer(refused: bool = False) -> AskAnswer:
    return AskAnswer(
        question="what about margins?",
        sentences=[] if refused else [AnswerSentence(
            text="They expect margins to expand.",
            citations=[AnswerCitation(episode_id="e", segment_timestamp=12.0,
                                      quote="margins expand as costs fall")],
        )],
        refused=refused,
    )


class _Mp3:
    def render(self, script: Script, soul: str, job_id: str) -> RenderedAudio:
        return RenderedAudio(data=FRAME * 10, media_type="audio/mpeg", extension="mp3")


def test_answer_script_reads_each_sentence_then_its_words() -> None:
    script = answer_script(_answer(), "v")
    assert script.monologue.splitlines()[-1] == "In their words: margins expand as costs fall"


def test_voice_answer_stores_under_the_jobs_own_name(tmp_path: Path) -> None:
    job = Job(job_id="abc123", status=JobStatus.done)
    voiced = voice_answer(_answer(), job, _Mp3(), LocalArtifactStore(tmp_path))
    assert voiced.audio_url and voiced.audio_url.startswith("/artifacts/episode_abc123__ask_")
    name = voiced.audio_url.rsplit("/", 1)[-1]
    assert job_id_from_artifact_name(name) == "abc123"  # same owner check as the episode
    assert job_id_from_artifact_name("episode_abc123.mp3") == "abc123"
    assert voice_answer(_answer(refused=True), job, _Mp3(), LocalArtifactStore(tmp_path)).audio_url is None
    placeholder = voice_answer(_answer(), job, MockAudioRenderer(out_dir=tmp_path),
                               LocalArtifactStore(tmp_path))
    assert placeholder.audio_url is None and placeholder.warnings


def test_ask_route_speaks_when_asked(tmp_path: Path) -> None:
    store = SqliteJobStore(tmp_path / "jobs.db")
    deps = Deps(FixtureTranscriptProvider(), MockLLMClient(), MockScriptComposer(), _Mp3(),
                LocalArtifactStore(tmp_path / "artifacts"))
    client = TestClient(create_app(store, deps, api_token=TOKEN))
    job_id = store.create()
    store.save(Job(job_id=job_id, status=JobStatus.done, digest=Digest(soul_version="v", episodes=[
        EpisodeDigest(episode_id="sample_public", episode_title="Sample", highlights=[])])))
    body = {"question": "What happened to gross margins?", "speak": True}
    answered = client.post(f"/digest/{job_id}/ask", json=body, headers=AUTH).json()
    assert answered["audio_url"]
    audio = client.get(answered["audio_url"], headers=AUTH)
    assert audio.status_code == 200 and audio.content.startswith(bytes([0xFF, 0xFB]))
    silent = client.post(f"/digest/{job_id}/ask", json={**body, "speak": False}, headers=AUTH)
    assert silent.json()["audio_url"] is None


# --- Readwise context for local runs ------------------------------------------------


class _Broken:
    def fetch(self, since: datetime) -> object:
        raise RuntimeError("readwise is down")


def test_local_context_blocks_collect_and_report(monkeypatch: pytest.MonkeyPatch) -> None:
    good = MockContextProvider("Readwise highlights", [ContextItem(text="Inference got cheaper.")])
    empty = MockContextProvider("Notes", [])
    blocks, problems = local_context_blocks(NOW, [good, empty, _Broken()])  # type: ignore[list-item]
    assert [b.source for b in blocks] == ["Readwise highlights"]
    assert problems == ["context source unavailable: RuntimeError: readwise is down"]

    monkeypatch.delenv("READWISE_TOKEN", raising=False)
    assert local_context_blocks(NOW) == ([], [])  # no token, no source, no call


def test_scheduler_reads_ratings_next_to_the_jobs(tmp_path: Path) -> None:
    from chorus.scheduler import _feedback_note

    jobs = SqliteJobStore(tmp_path / "jobs.db")
    feedback = SqliteFeedbackStore(tmp_path / "jobs.db")
    assert _feedback_note(jobs, MASTER_OWNER) is None
    for i in range(MIN_RATINGS_FOR_PROPOSAL):
        feedback.rate(_rating(i).model_copy(update={"job_id": f"job{i}"}))
    note = _feedback_note(jobs, MASTER_OWNER)
    assert note is not None and "propose updates to your soul" in note
    feedback.close()
    jobs.close()
