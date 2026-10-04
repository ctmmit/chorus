"""Running memory across weeks (chorus/memory.py): claims, the repeat
penalty, remembered thread members, the store, and two weeks back to back."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from chorus.artifacts import LocalArtifactStore
from chorus.audio import MockAudioRenderer
from chorus.curation import curate_episode
from chorus.jobs import MASTER_OWNER, SqliteJobStore
from chorus.llm import MockLLMClient
from chorus.memory import (
    MEMORY_LOOKBACK_WEEKS,
    REPEAT_PENALTY,
    REPEAT_WINDOW_WEEKS,
    Claim,
    SqliteClaimStore,
    as_remembered,
    claims_from,
    recall,
    related_claims,
    repeated_claim,
)
from chorus.models import (
    Digest,
    DigestRequest,
    EpisodeDigest,
    EpisodeInput,
    Highlight,
    Job,
    JobStatus,
    ResolvedEpisode,
    Thread,
    ThreadMember,
    Transcript,
)
from chorus.pipeline import Deps, run_job
from chorus.script import MockScriptComposer
from chorus.threads import validate_threads
from chorus.transcripts import FixtureTranscriptProvider

FIX = Path(__file__).resolve().parent.parent / "fixtures"
SOUL = (FIX / "souls" / "soul_investor.md").read_text(encoding="utf-8")
CONTEXT = (FIX / "context.md").read_text(encoding="utf-8")
NOW = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)


def _claim(quote: str, days_ago: int = 1, episode_id: str = "old", hid: str = "h-old") -> Claim:
    return Claim(
        owner=MASTER_OWNER, highlight_id=hid, job_id="j-old", episode_id=episode_id,
        show="Old Show", quote=quote, surfaced_at=NOW - timedelta(days=days_ago),
    )


def _h(episode_id: str, ts: float, quote: str) -> Highlight:
    return Highlight(
        episode_id=episode_id, episode_title="T", segment_timestamp=ts, quote=quote,
        relevance_score=0.8, why_surface="w", show=f"Show {episode_id}",
    )


# --- pure logic ------------------------------------------------------------------


def test_repeated_claim_needs_most_of_the_claims_content() -> None:
    claim = _claim("Gross margins expand when inference costs fall")
    assert repeated_claim("We think gross margins expand as inference costs fall fast.", [claim])
    assert repeated_claim("Margins are interesting.", [claim]) is None
    assert repeated_claim("anything", []) is None


def test_claims_from_a_job_carry_thread_questions() -> None:
    a, b = _h("a", 1, "Margins expand."), _h("b", 2, "Margins will not expand.")
    digest = Digest(
        soul_version="v",
        episodes=[EpisodeDigest(episode_id="a", episode_title="T", highlights=[a]),
                  EpisodeDigest(episode_id="b", episode_title="T", highlights=[b])],
        threads=[Thread(question="Do margins expand?", members=[
            ThreadMember(highlight_id=a.highlight_id, episode_id="a", stance="adds"),
            ThreadMember(highlight_id=b.highlight_id, episode_id="b", stance="disagrees"),
        ])],
    )
    job = Job(job_id="j1", status=JobStatus.done, owner="pat", digest=digest)
    claims = claims_from(job, NOW)
    assert [(c.highlight_id, c.owner, c.thread_question) for c in claims] == [
        (a.highlight_id, "pat", "Do margins expand?"),
        (b.highlight_id, "pat", "Do margins expand?"),
    ]
    assert claims_from(Job(job_id="j2", status=JobStatus.failed), NOW) == []


def test_related_claims_skip_this_weeks_and_unrelated() -> None:
    current = _h("new", 1, "Regulatory licenses create a moat for two players.")
    related = _claim("The licenses are a regulatory moat", hid="h1")
    unrelated = _claim("Pineapple belongs on pizza", hid="h2")
    same = _claim("Regulatory licenses create a moat", hid=current.highlight_id)
    assert related_claims([current], [related, unrelated, same]) == [related]


def test_recall_splits_the_repeat_window_from_the_lookback(tmp_path: Path) -> None:
    store = SqliteClaimStore(tmp_path / "m.db")
    store.remember([
        _claim("recent claim words here", days_ago=7, hid="r"),
        _claim("older claim words here", days_ago=7 * (REPEAT_WINDOW_WEEKS + 2), hid="o"),
        _claim("ancient claim words", days_ago=7 * (MEMORY_LOOKBACK_WEEKS + 2), hid="a"),
    ])
    memory = recall(store, MASTER_OWNER, NOW)
    assert [c.highlight_id for c in memory.repeats] == ["r"]
    assert [c.highlight_id for c in memory.lookback] == ["r", "o"]
    assert recall(None, MASTER_OWNER, NOW).lookback == []
    store.close()


def test_store_upserts_and_clears_per_owner(tmp_path: Path) -> None:
    store = SqliteClaimStore(tmp_path / "m.db")
    store.remember([_claim("first", days_ago=5)])
    store.remember([_claim("first, again", days_ago=1)])  # same highlight id
    store.remember([_claim("theirs", hid="t").model_copy(update={"owner": "pat"})])
    [mine] = store.recent(MASTER_OWNER, NOW - timedelta(days=30))
    assert mine.quote == "first, again"
    assert store.clear(MASTER_OWNER) == 1
    assert store.recent(MASTER_OWNER, NOW - timedelta(days=30)) == []
    assert len(store.recent("pat", NOW - timedelta(days=30))) == 1
    store.close()


# --- curation and threads ---------------------------------------------------------------


def _resolved(video_id: str) -> ResolvedEpisode:
    transcript = Transcript.model_validate_json(
        (FIX / "transcripts" / f"{video_id}.json").read_text(encoding="utf-8")
    )
    return ResolvedEpisode(episode=EpisodeInput(video_id=video_id), transcript=transcript)


def test_curation_penalizes_a_repeated_window() -> None:
    resolved = _resolved("sample_public")
    before = curate_episode(resolved, SOUL, CONTEXT, MockLLMClient())
    first = before.highlights[0]
    claim = _claim(first.quote, hid="earlier")
    after = curate_episode(resolved, SOUL, CONTEXT, MockLLMClient(), remembered=[claim])
    window = next(w for w in after.windows if w.start <= first.segment_timestamp < w.start + 90)
    baseline = next(w for w in before.windows if w.start == window.start)
    assert window.score == pytest.approx(max(0.0, baseline.score - REPEAT_PENALTY))
    repeated = next(h for h in after.highlights if h.segment_timestamp == first.segment_timestamp)
    assert "repeats a point surfaced" in repeated.why_surface


def test_a_thread_needs_one_of_this_weeks_highlights() -> None:
    now_h = _h("new", 1, "Margins will not expand.")
    old_a = as_remembered(_claim("Margins expand", episode_id="a", hid="ha"))
    old_b = as_remembered(_claim("Margins expand a lot", episode_id="b", hid="hb"))
    digest = Digest(soul_version="v", episodes=[
        EpisodeDigest(episode_id="new", episode_title="T", highlights=[now_h])
    ])
    only_past = Thread(question="Q", members=[
        ThreadMember(highlight_id="ha", episode_id="x", stance="adds"),
        ThreadMember(highlight_id="hb", episode_id="x", stance="agrees"),
    ])
    mixed = Thread(question="Q2", members=[
        ThreadMember(highlight_id=now_h.highlight_id, episode_id="x", stance="disagrees"),
        ThreadMember(highlight_id="ha", episode_id="x", stance="adds"),
    ])
    [kept] = validate_threads([only_past, mixed], digest, [old_a, old_b])
    remembered = next(m for m in kept.members if m.highlight_id == "ha")
    assert kept.question == "Q2"
    assert remembered.remembered_at == old_a.surfaced_at
    assert (remembered.episode_id, remembered.quote, remembered.source) == ("a", "Margins expand", "Old Show")


# --- two weeks back to back -------------------------------------------------------------------


def _deps(tmp_path: Path, claims: SqliteClaimStore | None) -> Deps:
    return Deps(
        FixtureTranscriptProvider(), MockLLMClient(), MockScriptComposer(),
        MockAudioRenderer(out_dir=tmp_path), LocalArtifactStore(tmp_path), claims,
    )


def _run(store: SqliteJobStore, deps: Deps, video_id: str, remember: bool = True) -> Job:
    job_id = store.create()
    request = DigestRequest(
        soul=SOUL, context=CONTEXT, episodes=[EpisodeInput(video_id=video_id)], remember=remember
    )
    run_job(job_id, request, store, deps)
    job = store.get(job_id)
    assert job is not None and job.status is JobStatus.done
    return job


def test_the_second_week_refers_back_to_the_first(tmp_path: Path) -> None:
    store = SqliteJobStore(tmp_path / "jobs.db")
    claims = SqliteClaimStore(tmp_path / "jobs.db")
    deps = _deps(tmp_path, claims)

    week1 = _run(store, deps, "sample_public")
    assert week1.digest is not None
    remembered = {c.highlight_id for c in claims.recent(MASTER_OWNER, NOW - timedelta(days=3650))}
    assert remembered == {h.highlight_id for h in week1.digest.highlights}

    week2 = _run(store, deps, "sample_counter")
    assert week2.digest is not None and week2.digest.threads
    past = [m for t in week2.digest.threads for m in t.members if m.remembered_at is not None]
    assert past and {m.highlight_id for m in past} <= remembered
    assert all(m.episode_id == "sample_public" and m.quote for m in past)

    week3 = _run(store, deps, "sample_public")
    assert week3.digest is not None
    assert all("repeats a point surfaced" in h.why_surface for h in week3.digest.highlights)


def test_remember_false_neither_reads_nor_writes(tmp_path: Path) -> None:
    store = SqliteJobStore(tmp_path / "jobs.db")
    claims = SqliteClaimStore(tmp_path / "jobs.db")
    deps = _deps(tmp_path, claims)
    _run(store, deps, "sample_public")
    count = len(claims.recent(MASTER_OWNER, NOW - timedelta(days=3650)))

    private = _run(store, deps, "sample_public", remember=False)
    assert private.digest is not None
    assert not any("repeats" in h.why_surface for h in private.digest.highlights)
    assert len(claims.recent(MASTER_OWNER, NOW - timedelta(days=3650))) == count


def test_no_claim_store_means_no_memory(tmp_path: Path) -> None:
    store = SqliteJobStore(tmp_path / "jobs.db")
    deps = _deps(tmp_path, None)
    _run(store, deps, "sample_public")
    again = _run(store, deps, "sample_public")
    assert again.digest is not None
    assert not any("repeats" in h.why_surface for h in again.digest.highlights)
