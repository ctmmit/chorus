"""Brief me now (chorus/quick_take.py): the verdict rule, grounded reasons,
chapters, the route, the share-sheet quick mode, and the latency bound."""
from __future__ import annotations

import time
from pathlib import Path

from fastapi.testclient import TestClient

from chorus.app import create_app
from chorus.artifacts import LocalArtifactStore
from chorus.audio import MockAudioRenderer, RenderedAudio
from chorus.jobs import MASTER_OWNER, SqliteJobStore
from chorus.llm import MockLLMClient
from chorus.models import (
    EpisodeDigest,
    EpisodeInput,
    Highlight,
    JobStatus,
    Script,
    Transcript,
)
from chorus.pipeline import Deps
from chorus.quick_take import (
    LISTEN_SCORE,
    MAX_REASONS,
    DraftReason,
    MockQuickTakeWriter,
    QuickDraft,
    build_take,
    ground_reasons,
    quick_take,
    quick_writer_for,
    verdict_for,
)
from chorus.script import MockScriptComposer
from chorus.transcripts import FixtureTranscriptProvider

FIX = Path(__file__).resolve().parent.parent / "fixtures"
SOUL = (FIX / "souls" / "soul_investor.md").read_text(encoding="utf-8")
POP = (FIX / "souls" / "soul_popculture.md").read_text(encoding="utf-8")
CONTEXT = (FIX / "context.md").read_text(encoding="utf-8")
TOKEN = "s3cret"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
# The mock path (no model, no audio) must answer well inside a minute.
QUICK_TAKE_MOCK_BOUND_S = 5.0


def _h(ts: float, score: float) -> Highlight:
    return Highlight(episode_id="e", episode_title="T", segment_timestamp=ts, quote=f"q{ts}",
                     relevance_score=score, why_surface="names the moat", show="S")


def _digest(*highlights: Highlight, refused: bool = False) -> EpisodeDigest:
    return EpisodeDigest(episode_id="e", episode_title="T", show="S",
                         highlights=list(highlights), refused=refused)


# --- the verdict -------------------------------------------------------------------


def test_verdict_rule() -> None:
    assert verdict_for(_digest(refused=True)) == "skip"
    assert verdict_for(_digest()) == "skip"
    assert verdict_for(_digest(_h(1, LISTEN_SCORE), _h(2, 0.4))) == "listen"
    assert verdict_for(_digest(_h(1, 0.99))) == "skim"  # one strong moment: skim to it
    assert verdict_for(_digest(_h(1, 0.5), _h(2, 0.6))) == "skim"


def test_reasons_must_cite_real_highlights() -> None:
    a, b = _h(1, 0.9), _h(2, 0.8)
    drafts = [DraftReason(text="good", highlight_id=a.highlight_id),
              DraftReason(text="invented", highlight_id="nope"),
              DraftReason(text="again", highlight_id=a.highlight_id),
              DraftReason(text="  ", highlight_id=b.highlight_id)]
    [kept] = ground_reasons(drafts, _digest(a, b))
    assert (kept.text, kept.highlight_id, kept.quote) == ("good", a.highlight_id, "q1")


class _Ungrounded:
    def write(self, digest: EpisodeDigest, soul: str, context: str, verdict: str) -> QuickDraft:
        return QuickDraft(reasons=[DraftReason(text="trust me", highlight_id="x")],
                          monologue="Trust me.")


def test_an_ungrounded_writer_falls_back_to_grounded_reasons() -> None:
    digest = _digest(_h(1, 0.9), _h(2, 0.8))
    take = build_take(digest, SOUL, "", _Ungrounded())
    assert take.reasons and all(r.highlight_id in {h.highlight_id for h in digest.highlights}
                                for r in take.reasons)
    assert "Trust me" not in take.monologue


def test_skip_has_no_reasons() -> None:
    take = build_take(_digest(refused=True), SOUL, "", MockQuickTakeWriter())
    assert take.verdict == "skip" and take.reasons == [] and "skip" in take.monologue


# --- end to end ------------------------------------------------------------------------


class _Mp3:
    def render(self, script: Script, soul: str, job_id: str) -> RenderedAudio:
        frame = bytes([0xFF, 0xFB, 0x90, 0x00]) + bytes(413)
        return RenderedAudio(data=frame * 300, media_type="audio/mpeg", extension="mp3")


def _deps(tmp_path: Path, renderer: object | None = None) -> Deps:
    return Deps(FixtureTranscriptProvider(), MockLLMClient(), MockScriptComposer(),
                renderer or MockAudioRenderer(out_dir=tmp_path),  # type: ignore[arg-type]
                LocalArtifactStore(tmp_path))


def test_quick_take_is_fast_grounded_and_stored(tmp_path: Path) -> None:
    store = SqliteJobStore(tmp_path / "jobs.db")
    started = time.perf_counter()
    job = quick_take(EpisodeInput(video_id="sample_public"), SOUL, CONTEXT, _deps(tmp_path), store)
    assert time.perf_counter() - started < QUICK_TAKE_MOCK_BOUND_S
    assert job.status is JobStatus.done and job.quick_take is not None
    take = job.quick_take
    assert take.verdict in ("listen", "skim") and 1 <= len(take.reasons) <= MAX_REASONS
    surfaced = {h.highlight_id for h in job.digest.highlights} if job.digest else set()
    assert all(r.highlight_id in surfaced for r in take.reasons)
    stored = store.get(job.job_id)
    assert stored is not None and stored.quick_take == take
    assert job.audio_url is None  # audio only when asked


def test_quick_take_with_audio_has_a_chapter_per_reason(tmp_path: Path) -> None:
    store = SqliteJobStore(tmp_path / "jobs.db")
    job = quick_take(EpisodeInput(video_id="sample_public"), SOUL, CONTEXT,
                     _deps(tmp_path, _Mp3()), store, audio=True)
    assert job.quick_take is not None and job.audio_url
    assert [c.title for c in job.chapters][0].startswith("Verdict: ")
    assert len(job.chapters) == 1 + len(job.quick_take.reasons)


def test_unreadable_episode_fails_with_the_reason(tmp_path: Path) -> None:
    store = SqliteJobStore(tmp_path / "jobs.db")
    job = quick_take(EpisodeInput(video_id="KhZfxZ-C-2g"), SOUL, CONTEXT, _deps(tmp_path), store)
    assert job.status is JobStatus.failed and job.error and "no transcript" in job.error


def test_the_mock_scorer_gets_the_mock_writer() -> None:
    assert quick_writer_for(MockLLMClient()) is MockQuickTakeWriter


# --- routes ----------------------------------------------------------------------------


def _client(tmp_path: Path) -> TestClient:
    store = SqliteJobStore(tmp_path / "jobs.db")
    return TestClient(create_app(store, _deps(tmp_path), api_token=TOKEN))


def test_quick_take_route(tmp_path: Path) -> None:
    client = _client(tmp_path)
    body = {"episode": {"video_id": "sample_public"}, "soul": SOUL, "context": CONTEXT}
    assert client.post("/quick-take", json=body).status_code == 401
    ok = client.post("/quick-take", json=body, headers=AUTH)
    assert ok.status_code == 200 and ok.json()["quick_take"]["verdict"] in ("listen", "skim")
    assert ok.json()["owner"] == MASTER_OWNER
    missing = client.post("/quick-take", json={**body, "episode": {"video_id": "KhZfxZ-C-2g"}},
                          headers=AUTH)
    assert missing.status_code == 422


class _AnyIdIsTheSample:
    """Serves the public sample transcript for any YouTube id, so a well-formed
    share link resolves without the private fixtures."""

    def get(self, episode: EpisodeInput) -> Transcript:
        transcript = FixtureTranscriptProvider().get(EpisodeInput(video_id="sample_public"))
        return transcript.model_copy(update={"video_id": episode.resolved_id()})


def test_share_sheet_quick_mode(tmp_path: Path) -> None:
    deps = _deps(tmp_path)
    deps.provider = _AnyIdIsTheSample()
    client = TestClient(create_app(SqliteJobStore(tmp_path / "jobs.db"), deps, api_token=TOKEN))
    link = "https://youtu.be/AAAAAAAAAAA"
    saved = client.post("/library/share", json={"links": [link]}, headers=AUTH)
    assert saved.status_code == 200 and saved.json()["quick_take"] is None
    assert [e["video_id"] or e["url"] for e in saved.json()["episodes"]]

    quick = client.post("/library/share", json={"links": [link], "mode": "quick", "soul": SOUL},
                        headers=AUTH)
    assert quick.status_code == 200
    take = quick.json()["quick_take"]
    assert take["job_id"] and take["quick_take"]["verdict"] in ("listen", "skim")

    no_lens = client.post("/library/share", json={"links": [link], "mode": "quick"}, headers=AUTH)
    assert no_lens.status_code == 422
