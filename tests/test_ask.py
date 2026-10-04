"""Ask the episode (chorus/ask.py): retrieval, grounding, refusal, routes."""
from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from chorus.app import create_app
from chorus.artifacts import LocalArtifactStore
from chorus.ask import (
    REFUSAL,
    UNGROUNDED,
    DraftSentence,
    MockAnswerWriter,
    Window,
    answer,
    answer_writer_for,
    ground,
    ground_quote,
    retrieve,
    transcript_for,
)
from chorus.audio import MockAudioRenderer
from chorus.jobs import MASTER_OWNER, SqliteJobStore
from chorus.llm import MockLLMClient
from chorus.models import Digest, EpisodeDigest, Job, JobStatus, Transcript
from chorus.pipeline import Deps
from chorus.script import MockScriptComposer
from chorus.transcripts import FixtureTranscriptProvider

FIX = Path(__file__).resolve().parent.parent / "fixtures"
TOKEN = "s3cret"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def _transcript(video_id: str) -> Transcript:
    return Transcript.model_validate_json(
        (FIX / "transcripts" / f"{video_id}.json").read_text(encoding="utf-8")
    )


def _job(job_id: str = "j1", owner: str = MASTER_OWNER, *video_ids: str) -> Job:
    ids = video_ids or ("sample_public",)
    episodes = [EpisodeDigest(episode_id=v, episode_title=f"Title {v}", highlights=[]) for v in ids]
    return Job(job_id=job_id, status=JobStatus.done, owner=owner,
               digest=Digest(soul_version="v", episodes=episodes))


def _window(text: str, start: float = 100.0) -> Window:
    pieces = [p.strip() + "." for p in text.split(".") if p.strip()]
    return Window(episode_id="e", start=start, text=" ".join(pieces),
                  starts=[start + 5 * i for i in range(len(pieces))], pieces=pieces)


# --- retrieval ---------------------------------------------------------------------


def test_retrieve_ranks_windows_by_shared_words() -> None:
    windows = retrieve(
        "What about gross margins and inference costs?",
        {"sample_public": _transcript("sample_public")},
        {"sample_public": "Sample"},
    )
    assert windows and windows[0].start == 0.0 and windows[0].title == "Sample"
    assert all(w.episode_id == "sample_public" for w in windows)
    assert retrieve("zebra xylophone", {"sample_public": _transcript("sample_public")}, {}) == []


def test_transcript_for_reads_fixtures_and_never_guesses_rss() -> None:
    provider = FixtureTranscriptProvider()
    assert transcript_for("sample_public", provider) is not None
    assert transcript_for("rss-0123456789abcdef", provider) is None
    assert transcript_for("does-not-exist", provider) is None


# --- grounding ---------------------------------------------------------------------


def test_ground_quote_needs_verbatim_words_and_finds_the_segment() -> None:
    window = _window("Margins expand quickly. Buyers capture the savings within two quarters.")
    citation = ground_quote(window, '"Buyers capture the savings within two quarters."')
    assert citation is not None and citation.segment_timestamp == 105.0
    assert ground_quote(window, "Buyers capture all of the savings") is None
    assert ground_quote(window, "Margins expand") is None  # too short to trust


def test_ground_drops_sentences_citing_unretrieved_windows() -> None:
    windows = [_window("Margins expand quickly as costs fall today.")]
    drafts = [
        DraftSentence(text="Margins expand.", window=0, quote="Margins expand quickly as costs"),
        DraftSentence(text="Invented.", window=3, quote="Margins expand quickly as costs"),
        DraftSentence(text="Misquoted.", window=0, quote="Margins collapse quickly as costs"),
    ]
    kept = ground(drafts, windows)
    assert [s.text for s in kept] == ["Margins expand."]


# --- answers ------------------------------------------------------------------------


def test_answer_quotes_the_transcript() -> None:
    result = answer("What happened to gross margins?", _job(), FixtureTranscriptProvider(),
                    MockAnswerWriter())
    assert not result.refused and result.sentences
    source = " ".join(s.text for s in _transcript("sample_public").segments)
    for sentence in result.sentences:
        [citation] = sentence.citations
        assert " ".join(citation.quote.split()) in " ".join(source.split())


def test_answer_refuses_when_nothing_speaks_to_it() -> None:
    result = answer("zebra xylophone quasar", _job(), FixtureTranscriptProvider(),
                    MockAnswerWriter())
    assert result.refused and result.refusal_reason == REFUSAL and not result.sentences


class _MakesThingsUp:
    def write(self, question: str, windows: list[Window]) -> list[DraftSentence]:
        return [DraftSentence(text="Margins tripled.", window=0,
                              quote="margins tripled in a single week")]


def test_answer_refuses_when_nothing_grounded_survives() -> None:
    result = answer("What happened to gross margins?", _job(), FixtureTranscriptProvider(),
                    _MakesThingsUp())
    assert result.refused and result.refusal_reason == UNGROUNDED


def test_unavailable_transcripts_are_reported() -> None:
    job = _job("j", MASTER_OWNER, "sample_public", "rss-0123456789abcdef")
    result = answer("gross margins", job, FixtureTranscriptProvider(), MockAnswerWriter())
    assert result.unavailable == ["rss-0123456789abcdef"] and not result.refused


def test_a_mock_scorer_gets_the_mock_writer() -> None:
    assert answer_writer_for(MockLLMClient()) is MockAnswerWriter


# --- the route -----------------------------------------------------------------------


def test_ask_route_checks_auth_and_owner(tmp_path: Path) -> None:
    store = SqliteJobStore(tmp_path / "jobs.db")
    deps = Deps(FixtureTranscriptProvider(), MockLLMClient(), MockScriptComposer(),
                MockAudioRenderer(), LocalArtifactStore(tmp_path))
    client = TestClient(create_app(store, deps, api_token=TOKEN))
    mine = store.create()
    store.save(_job(mine))
    theirs = store.create(owner="pat@example.com")
    store.save(_job(theirs, "pat@example.com"))

    body = {"question": "What happened to gross margins?"}
    assert client.post(f"/digest/{mine}/ask", json=body).status_code == 401
    ok = client.post(f"/digest/{mine}/ask", json=body, headers=AUTH)
    assert ok.status_code == 200 and ok.json()["sentences"]
    assert client.post("/digest/nope/ask", json=body, headers=AUTH).status_code == 404
    assert client.post(f"/digest/{mine}/ask", json={"question": ""}, headers=AUTH).status_code == 422
    # The master token may read any job; an issued key may not read another's.
    assert client.post(f"/digest/{theirs}/ask", json=body, headers=AUTH).status_code == 200
