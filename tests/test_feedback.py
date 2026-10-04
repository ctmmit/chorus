"""Highlight feedback and soul proposals (chorus/feedback.py)."""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from chorus.app import create_app
from chorus.artifacts import LocalArtifactStore
from chorus.audio import MockAudioRenderer
from chorus.curation import curate_episode, soul_version
from chorus.feedback import (
    MIN_RATINGS_FOR_PROPOSAL,
    FeedbackError,
    FeedbackService,
    MockProposalWriter,
    NotEnoughFeedback,
    Rating,
    SoulEdit,
    SoulProposal,
    SqliteFeedbackStore,
    apply_soul_update,
    make_rating,
    propose_soul_update,
    rating_link,
    summarize_feedback,
)
from chorus.jobs import MASTER_OWNER, SqliteJobStore
from chorus.llm import MockLLMClient
from chorus.models import (
    Digest,
    EpisodeInput,
    Highlight,
    Job,
    JobStatus,
    ResolvedEpisode,
    Transcript,
    highlight_id,
)
from chorus.pipeline import Deps
from chorus.script import MockScriptComposer
from chorus.subscriptions import SqliteSubscriptionStore, Subscription
from chorus.transcripts import FixtureTranscriptProvider

FIX = Path(__file__).resolve().parent.parent / "fixtures"
SOUL = (FIX / "souls" / "soul_investor.md").read_text(encoding="utf-8")
NOW = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)
TOKEN = "s3cret"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture(autouse=True)
def _secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHORUS_UNSUBSCRIBE_SECRET", "links-secret-for-tests")


def _highlight(ts: float, show: str = "Acquired", score: float = 0.8) -> Highlight:
    return Highlight(
        episode_id="ep", episode_title="T", segment_timestamp=ts, quote=f"quote {ts}",
        relevance_score=score, why_surface="reason", show=show,
    )


def _job(job_id: str, *highlights: Highlight, owner: str = MASTER_OWNER) -> Job:
    from chorus.models import EpisodeDigest

    episode = EpisodeDigest(episode_id="ep", episode_title="T", highlights=list(highlights))
    return Job(
        job_id=job_id, status=JobStatus.done, owner=owner,
        digest=Digest(soul_version="v", episodes=[episode]),
    )


def _rating(vote: str, show: str = "Acquired", note: str = "", minutes: int = 0) -> Rating:
    return Rating(
        owner=MASTER_OWNER, job_id=uuid.uuid4().hex, highlight_id="h", vote=vote,  # type: ignore[arg-type]
        note=note, rated_at=NOW + timedelta(minutes=minutes), episode_id="ep", show=show,
        quote="q", relevance_score=0.8,
    )


# --- ids and ratings -------------------------------------------------------------


def test_highlight_id_is_stable_and_filled_in() -> None:
    h = _highlight(754.04)
    assert h.highlight_id == highlight_id("ep", 754.0) == _highlight(754.0).highlight_id
    assert h.highlight_id != _highlight(755.0).highlight_id
    assert len(h.highlight_id) == 12
    assert Highlight.model_validate_json(h.model_dump_json()).highlight_id == h.highlight_id


def test_make_rating_snapshots_the_highlight() -> None:
    h = _highlight(10.0, show="Stratechery", score=0.42)
    rating = make_rating(_job("j", h), h.highlight_id, "down", "  too much gossip  ", NOW)
    assert (rating.show, rating.quote, rating.relevance_score) == ("Stratechery", "quote 10.0", 0.42)
    assert rating.note == "too much gossip"
    with pytest.raises(FeedbackError):
        make_rating(_job("j", h), "nope", "up", "", NOW)


def test_store_rerating_overwrites(tmp_path: Path) -> None:
    store = SqliteFeedbackStore(tmp_path / "f.db")
    h = _highlight(10.0)
    job = _job("j", h)
    store.rate(make_rating(job, h.highlight_id, "up", "", NOW))
    store.rate(make_rating(job, h.highlight_id, "down", "changed my mind", NOW))
    [rating] = store.ratings(MASTER_OWNER)
    assert rating.vote == "down" and rating.note == "changed my mind"
    assert store.ratings("someone-else") == []
    store.close()


def test_summary_tallies_and_keeps_notes_newest_first() -> None:
    ratings = [
        _rating("down", "Acquired", "too much fundraising gossip", minutes=1),
        _rating("down", "Acquired", minutes=2),
        _rating("up", "Stratechery", "more on pricing power", minutes=3),
        _rating("down", "Acquired", "celebrity drama", minutes=4),
    ]
    summary = summarize_feedback(ratings)
    assert (summary.total, summary.up, summary.down) == (4, 1, 3)
    assert summary.by_show["Acquired"].down == 3
    assert summary.by_band["high"].down == 3
    assert summary.notes_down == ["celebrity drama", "too much fundraising gossip"]
    assert summary.notes_up == ["more on pricing power"]


# --- proposals ----------------------------------------------------------------------


def test_proposal_refuses_below_the_minimum() -> None:
    few = [_rating("down") for _ in range(MIN_RATINGS_FOR_PROPOSAL - 1)]
    with pytest.raises(NotEnoughFeedback, match="at least"):
        propose_soul_update(SOUL, few, MASTER_OWNER, MockProposalWriter(), NOW)


def test_mock_proposal_turns_notes_and_shows_into_edits() -> None:
    ratings = [_rating("down", "Acquired", "fundraising gossip")] + [
        _rating("down", "Acquired") for _ in range(4)
    ] + [_rating("up", "Stratechery", "aggregation theory") for _ in range(3)]
    proposal = propose_soul_update(SOUL, ratings, MASTER_OWNER, MockProposalWriter(), NOW)
    assert proposal.base_soul_version == soul_version(SOUL)
    by_text = {(e.section, e.text) for e in proposal.edits}
    assert ("anti_interests", "fundraising gossip") in by_text
    assert ("attention_triggers", "aggregation theory") in by_text
    assert ("anti_interests", "Routine segments from Acquired") in by_text
    assert all(e.evidence for e in proposal.edits)


class _Writer:
    def __init__(self, edits: list[SoulEdit]) -> None:
        self.edits = edits

    def write(self, soul: str, summary: object) -> list[SoulEdit]:
        return self.edits


def test_remove_edits_must_name_an_existing_bullet() -> None:
    ratings = [_rating("down") for _ in range(MIN_RATINGS_FOR_PROPOSAL)]
    writer = _Writer([
        SoulEdit(section="anti_interests", action="remove", text="Not in the soul", evidence="e"),
        SoulEdit(section="anti_interests", action="remove",
                 text="Hype and round-number predictions with no mechanism", evidence="e"),
    ])
    proposal = propose_soul_update(SOUL, ratings, MASTER_OWNER, writer, NOW)
    assert [e.text for e in proposal.edits] == [
        "Hype and round-number predictions with no mechanism"
    ]


def _proposal(*edits: SoulEdit, soul: str = SOUL) -> SoulProposal:
    return SoulProposal(
        proposal_id="p", owner=MASTER_OWNER, base_soul_version=soul_version(soul),
        edits=list(edits), summary=summarize_feedback([]), created_at=NOW,
    )


def test_apply_adds_and_removes_only_accepted_edits() -> None:
    proposal = _proposal(
        SoulEdit(section="anti_interests", action="add", text="Fundraising gossip", evidence="e"),
        SoulEdit(section="attention_triggers", action="add", text="Aggregation theory", evidence="e"),
        SoulEdit(section="anti_interests", action="remove",
                 text="Hype and round-number predictions with no mechanism", evidence="e"),
    )
    updated = apply_soul_update(SOUL, proposal, [0, 2])
    lines = updated.splitlines()
    ignore = lines.index("## Ignore")
    style = lines.index("## Communication style")
    assert "- Fundraising gossip" in lines[ignore:style]
    assert lines[style - 2] == "- Fundraising gossip" and lines[style - 1] == ""
    assert "- Hype and round-number predictions with no mechanism" not in lines
    assert "Aggregation theory" not in updated
    assert soul_version(updated) != soul_version(SOUL)


def test_apply_creates_a_missing_section_and_rejects_bad_indexes() -> None:
    proposal = _proposal(
        SoulEdit(section="core_interests", action="add", text="Payments", evidence="e")
    )
    updated = apply_soul_update(SOUL, proposal, [0])
    assert updated.rstrip().endswith("## Core Interests\n- Payments")
    with pytest.raises(FeedbackError, match="no edit at index"):
        apply_soul_update(SOUL, proposal, [3])


def test_apply_refuses_a_result_that_no_longer_validates() -> None:
    thin = "## Identity\nAn investor.\n\n## Attention triggers\n- moats\n\n## Ignore\n- gossip\n\n## Curation Guidance\nHigh bar.\n"
    # An empty Ignore section is allowed (souls may have no anti-interests);
    # an empty Attention triggers section is not.
    proposal = _proposal(
        SoulEdit(section="attention_triggers", action="remove", text="moats", evidence="e"),
        soul=thin,
    )
    with pytest.raises(FeedbackError, match="would not validate"):
        apply_soul_update(thin, proposal, [0])


# --- the service --------------------------------------------------------------------------


def _service(tmp_path: Path) -> tuple[FeedbackService, SqliteJobStore, SqliteSubscriptionStore]:
    jobs = SqliteJobStore(tmp_path / "jobs.db")
    subs = SqliteSubscriptionStore(tmp_path / "jobs.db")
    service = FeedbackService(
        SqliteFeedbackStore(tmp_path / "jobs.db"), jobs, subs,
        writer_factory=MockProposalWriter, clock=lambda: NOW,
    )
    return service, jobs, subs


def _rate_many(service: FeedbackService, jobs: SqliteJobStore, note: str, n: int) -> None:
    for i in range(n):
        h = _highlight(float(i))
        job_id = jobs.create()
        jobs.save(_job(job_id, h))
        service.rate(MASTER_OWNER, job_id, h.highlight_id, "down", note)


def test_service_updates_a_subscription_soul(tmp_path: Path) -> None:
    service, jobs, subs = _service(tmp_path)
    sub = Subscription(
        subscription_id="s1", owner=MASTER_OWNER, email="p@example.com", soul=SOUL, context="",
        episodes=[EpisodeInput(video_id="sample_public")], next_run_at=NOW, created_at=NOW,
    )
    subs.create(sub)
    _rate_many(service, jobs, "fundraising gossip", MIN_RATINGS_FOR_PROPOSAL)

    proposal = service.propose(MASTER_OWNER, subscription_id="s1")
    applied = service.apply(MASTER_OWNER, proposal.proposal_id, [0], subscription_id="s1")
    stored = subs.get("s1")
    assert stored is not None
    assert "- fundraising gossip" in stored.soul
    assert stored.soul_origin == applied.soul_origin == f"feedback:{proposal.proposal_id}"
    assert applied.saved_to == "subscription s1"

    with pytest.raises(FeedbackError, match="soul changed"):
        service.apply(MASTER_OWNER, proposal.proposal_id, [0], subscription_id="s1")


def test_service_rejects_another_owners_job(tmp_path: Path) -> None:
    service, jobs, _ = _service(tmp_path)
    h = _highlight(1.0)
    job_id = jobs.create(owner="pat@example.com")
    jobs.save(_job(job_id, h, owner="pat@example.com"))
    with pytest.raises(FeedbackError, match="unknown job"):
        service.rate("sam@example.com", job_id, h.highlight_id, "up")


# --- HTTP and email ------------------------------------------------------------------------


def _client(tmp_path: Path) -> tuple[TestClient, SqliteJobStore]:
    store = SqliteJobStore(tmp_path / "jobs.db")
    deps = Deps(
        FixtureTranscriptProvider(), MockLLMClient(), MockScriptComposer(),
        MockAudioRenderer(), LocalArtifactStore(tmp_path / "artifacts"),
    )
    return TestClient(create_app(store, deps, api_token=TOKEN)), store


def test_http_rating_proposal_and_apply(tmp_path: Path) -> None:
    client, store = _client(tmp_path)
    ids: list[tuple[str, str]] = []
    for i in range(MIN_RATINGS_FOR_PROPOSAL):
        h = _highlight(float(i))
        job_id = store.create()
        store.save(_job(job_id, h))
        ids.append((job_id, h.highlight_id))

    body = {"job_id": ids[0][0], "highlight_id": ids[0][1], "vote": "down", "note": "gossip"}
    assert client.post("/feedback", json=body).status_code == 401
    assert client.post("/feedback", json=body, headers=AUTH).status_code == 200
    early = client.post("/feedback/proposals", json={"soul": SOUL}, headers=AUTH)
    assert early.status_code == 409

    for job_id, hid in ids[1:]:
        client.post("/feedback", json={"job_id": job_id, "highlight_id": hid, "vote": "down"},
                    headers=AUTH)
    proposal = client.post("/feedback/proposals", json={"soul": SOUL}, headers=AUTH).json()
    applied = client.post(
        f"/feedback/proposals/{proposal['proposal_id']}/apply",
        json={"accept": [0], "soul": SOUL}, headers=AUTH,
    ).json()
    assert "- gossip" in applied["soul"] and applied["saved_to"] is None
    missing = client.post("/feedback/proposals/nope/apply", json={"accept": [0], "soul": SOUL},
                          headers=AUTH)
    assert missing.status_code == 404


def test_signed_email_link_records_a_vote_without_a_token(tmp_path: Path) -> None:
    client, store = _client(tmp_path)
    h = _highlight(5.0)
    job_id = store.create()
    job = _job(job_id, h)
    store.save(job)

    link = rating_link("http://testserver", job, h.highlight_id, "up").replace("http://testserver", "")
    page = client.get(link)
    assert page.status_code == 200 and "more like this" in page.text
    assert client.get(link.replace("v=up", "v=down")).status_code == 404  # signature is per vote
    assert client.get(link[:-4] + "0000").status_code == 404

    from chorus import config_env

    ratings = config_env.select_feedback_store(store).ratings(MASTER_OWNER)
    assert [(r.highlight_id, r.vote) for r in ratings] == [(h.highlight_id, "up")]


def test_digest_email_carries_rating_links() -> None:
    from chorus.digest_email import render_digest_email

    h = _highlight(5.0)
    job = _job("j1", h)
    sub = Subscription(
        subscription_id="s1", owner=MASTER_OWNER, email="p@example.com", soul=SOUL, context="",
        episodes=[EpisodeInput(video_id="ep")], next_run_at=NOW, created_at=NOW,
    )
    content = render_digest_email(job, sub, "https://chorus.example", "https://u")
    assert rating_link("https://chorus.example", job, h.highlight_id, "up") in content.text
    assert "Less like this" in content.html


# --- the plan's eval check -------------------------------------------------------------------


def test_down_voting_a_theme_and_accepting_demotes_it() -> None:
    """Down-vote the regulatory/market-structure window of the public sample
    with a note, accept the proposal, and that window drops out of the
    investor digest while the must window at 0s stays."""
    transcript = Transcript.model_validate_json(
        (FIX / "transcripts" / "sample_public.json").read_text(encoding="utf-8")
    )
    resolved = ResolvedEpisode(episode=EpisodeInput(video_id="sample_public"), transcript=transcript)
    context = (FIX / "context.md").read_text(encoding="utf-8")

    def surfaced(soul: str) -> list[int]:
        digest = curate_episode(resolved, soul, context, MockLLMClient())
        return sorted(int(h.segment_timestamp // 90) * 90 for h in digest.highlights)

    before = surfaced(SOUL)
    assert 0 in before and 180 in before

    digest = curate_episode(resolved, SOUL, context, MockLLMClient())
    target = next(h for h in digest.highlights if h.segment_timestamp >= 180)
    note = "regulatory licenses and market structure"
    ratings = [
        make_rating(_job(f"j{i}", target), target.highlight_id, "down", note, NOW)
        for i in range(MIN_RATINGS_FOR_PROPOSAL)
    ]
    proposal = propose_soul_update(SOUL, ratings, MASTER_OWNER, MockProposalWriter(), NOW)
    updated = apply_soul_update(SOUL, proposal, list(range(len(proposal.edits))))

    after = surfaced(updated)
    assert 180 not in after
    assert 0 in after


def test_a_mock_scorer_gets_the_mock_writer(monkeypatch: pytest.MonkeyPatch) -> None:
    from chorus.feedback import proposal_writer_for

    monkeypatch.setenv("ANTHROPIC_API_KEY", "would-be-billed")
    assert proposal_writer_for(MockLLMClient()) is MockProposalWriter
