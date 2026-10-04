"""Cross-source threads (chorus/threads.py): validation, the mock writer,
the outline prompt, the pipeline stage, and the email."""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from chorus.curation import curate_episode
from chorus.llm import MockLLMClient
from chorus.models import (
    MONOLOGUE_PROFILE,
    Digest,
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
from chorus.outline import outline_prompt
from chorus.pipeline import stage_threads
from chorus.script import plan_episode
from chorus.threads import (
    MAX_THREADS,
    MockThreadWriter,
    build_threads,
    thread_writer_for,
    threads_brief,
    validate_threads,
)

FIX = Path(__file__).resolve().parent.parent / "fixtures"
SOUL = (FIX / "souls" / "soul_investor.md").read_text(encoding="utf-8")
CONTEXT = (FIX / "context.md").read_text(encoding="utf-8")


def _h(episode_id: str, ts: float, quote: str) -> Highlight:
    return Highlight(
        episode_id=episode_id, episode_title=f"T {episode_id}", segment_timestamp=ts,
        quote=quote, relevance_score=0.8, why_surface="w", show=f"Show {episode_id}",
    )


def _digest(*highlights: Highlight) -> Digest:
    by_episode: dict[str, list[Highlight]] = {}
    for h in highlights:
        by_episode.setdefault(h.episode_id, []).append(h)
    return Digest(
        soul_version="v",
        episodes=[
            EpisodeDigest(episode_id=e, episode_title=f"T {e}", highlights=hs)
            for e, hs in by_episode.items()
        ],
    )


A = _h("a", 10, "Margins expand as inference costs fall.")
B = _h("b", 20, "Margins will not expand; buyers capture the savings.")
C = _h("c", 30, "Something else entirely.")


def _member(h: Highlight, stance: str = "agrees", episode_id: str = "lie") -> ThreadMember:
    return ThreadMember(highlight_id=h.highlight_id, episode_id=episode_id, stance=stance)  # type: ignore[arg-type]


# --- validation ------------------------------------------------------------------


def test_validation_drops_invented_ids_and_trusts_highlights_for_episodes() -> None:
    thread = Thread(
        question="  Do   margins expand? ",
        members=[
            _member(A, "adds"),
            _member(B, "disagrees"),
            ThreadMember(highlight_id="invented", episode_id="x", stance="agrees"),
            _member(A, "agrees"),  # duplicate
        ],
    )
    [kept] = validate_threads([thread], _digest(A, B))
    assert kept.question == "Do margins expand?"
    assert [(m.highlight_id, m.episode_id) for m in kept.members] == [
        (A.highlight_id, "a"), (B.highlight_id, "b"),
    ]
    assert kept.disagreement


def test_validation_drops_single_source_and_empty_threads() -> None:
    same_source = _h("a", 50, "Another margin point from the same show.")
    single = Thread(question="Q", members=[_member(A), _member(same_source)])
    blank = Thread(question="   ", members=[_member(A), _member(B)])
    assert validate_threads([single, blank], _digest(A, B, same_source)) == []


def test_validation_orders_disagreements_first_and_caps() -> None:
    agree = Thread(question="agree", members=[_member(A), _member(B), _member(C)])
    disagree = Thread(question="disagree", members=[_member(A), _member(B, "disagrees")])
    kept = validate_threads([agree, disagree], _digest(A, B, C))
    assert [t.question for t in kept] == ["disagree", "agree"]
    many = [Thread(question=str(i), members=[_member(A), _member(B)]) for i in range(10)]
    assert len(validate_threads(many, _digest(A, B))) == MAX_THREADS


# --- building ---------------------------------------------------------------------


class _CountingWriter:
    calls = 0

    def write(self, highlights: list[Highlight]) -> list[Thread]:
        self.calls += 1
        return []


def test_no_model_call_with_fewer_than_two_sources() -> None:
    writer = _CountingWriter()
    assert build_threads(_digest(A), writer) == []
    assert writer.calls == 0


def _curated(*video_ids: str) -> Digest:
    episodes = []
    for vid in video_ids:
        transcript = Transcript.model_validate_json(
            (FIX / "transcripts" / f"{vid}.json").read_text(encoding="utf-8")
        )
        resolved = ResolvedEpisode(episode=EpisodeInput(video_id=vid), transcript=transcript)
        episodes.append(curate_episode(resolved, SOUL, CONTEXT, MockLLMClient()))
    return Digest(soul_version="v", episodes=episodes)


def test_rebuttal_transcripts_make_a_disagreement_thread() -> None:
    """The plan's eval check: two public transcripts on the same topic, the
    second arguing against the first, yield a cross-source thread with a
    `disagrees` member."""
    digest = _curated("sample_public", "sample_counter")
    threads = build_threads(digest, MockThreadWriter())
    assert threads, "expected at least one thread"
    first = threads[0]
    assert {m.episode_id for m in first.members} == {"sample_public", "sample_counter"}
    assert any(m.stance == "disagrees" and m.episode_id == "sample_counter" for m in first.members)


def test_mock_writer_needs_shared_words() -> None:
    assert MockThreadWriter().write([A, C]) == []


def test_stage_threads_uses_the_mock_writer_for_a_mock_scorer() -> None:
    assert thread_writer_for(MockLLMClient()) is MockThreadWriter
    digest = _curated("sample_public", "sample_counter")
    assert stage_threads(digest, MockLLMClient())


# --- downstream: outline and email ----------------------------------------------------


def test_threads_reach_the_outline_prompt() -> None:
    digest = _curated("sample_public", "sample_counter")
    digest.threads = build_threads(digest, MockThreadWriter())
    brief = threads_brief(digest)
    assert "(a disagreement)" in brief and "sample_counter disagrees" in brief
    plan = plan_episode(digest, MONOLOGUE_PROFILE)
    assert plan.threads == brief
    prompt = outline_prompt([], plan.overflow, plan.fixed_minutes, plan.threads)
    assert "THREADS ACROSS SOURCES" in prompt and brief in prompt
    assert "THREADS ACROSS SOURCES" not in outline_prompt([], [], None)


def test_digest_email_leads_with_threads() -> None:
    from chorus.digest_email import render_digest_email
    from chorus.subscriptions import Subscription

    digest = _digest(A, B)
    digest.threads = [Thread(question="Do margins expand?",
                             members=[_member(A, "adds", "a"), _member(B, "disagrees", "b")])]
    job = Job(job_id="j", status=JobStatus.done, digest=digest)
    now = datetime.now(UTC)
    sub = Subscription(
        subscription_id="s", owner="master", email="p@example.com", soul=SOUL, context="",
        episodes=[EpisodeInput(video_id="a")], next_run_at=now, created_at=now,
    )
    content = render_digest_email(job, sub, "https://chorus.example", "https://u")
    assert content.text.index("# This week's threads") < content.text.index("## T a")
    assert "- Show b (disagrees): Margins will not expand" in content.text
    assert "Do margins expand?" in content.html
