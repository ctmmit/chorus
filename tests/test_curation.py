"""Goal 2 — curation, tested against real fixtures with the deterministic mock.

The headline test (`test_two_souls_diverge`) turns the hand-validated visibility
result into a regression: same episode, swap the soul, get different highlights.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from chorus.curation import REFUSAL, build_digest, citation_resolves, curate_episode
from chorus.ingest import ingest
from chorus.llm import MockLLMClient
from chorus.models import EpisodeInput
from chorus.transcripts import FixtureTranscriptProvider

FIX = Path(__file__).resolve().parent.parent / "fixtures"
ANDREESSEN = "c4tvVKDhpiY"
EGGS = "IAgmW_gTxls"


def _soul(name: str) -> str:
    return (FIX / "souls" / name).read_text(encoding="utf-8")


def _context() -> str:
    return (FIX / "context.md").read_text(encoding="utf-8")


@pytest.fixture
def client() -> MockLLMClient:
    return MockLLMClient()


def _resolve(video_id: str):
    result = ingest([EpisodeInput(video_id=video_id)], FixtureTranscriptProvider())
    return result.resolved[0]


def test_clean_episode_has_highlights(client: MockLLMClient) -> None:
    ep = curate_episode(_resolve(ANDREESSEN), _soul("soul_investor.md"), _context(), client)
    assert not ep.refused
    assert len(ep.highlights) >= 1


def test_citations_resolve(client: MockLLMClient) -> None:
    resolved = _resolve(ANDREESSEN)
    ep = curate_episode(resolved, _soul("soul_investor.md"), _context(), client)
    for h in ep.highlights:
        assert citation_resolves(resolved.transcript, h.segment_timestamp, h.quote), (
            f"highlight at {h.segment_timestamp}s did not resolve"
        )


def test_two_souls_diverge(client: MockLLMClient) -> None:
    ig = ingest([EpisodeInput(video_id=ANDREESSEN)], FixtureTranscriptProvider())
    inv = build_digest(ig, _soul("soul_investor.md"), _context(), client)
    pop = build_digest(ig, _soul("soul_popculture.md"), _context(), client)

    inv_ts = {round(h.segment_timestamp) for h in inv.highlights}
    pop_ts = {round(h.segment_timestamp) for h in pop.highlights}
    assert inv_ts and pop_ts, "both souls should surface something on this episode"
    assert inv_ts != pop_ts, "souls produced identical highlights — lens not working"
    overlap = len(inv_ts & pop_ts) / max(len(inv_ts), len(pop_ts))
    assert overlap < 0.5, f"highlight overlap too high ({overlap:.0%}); lenses barely differ"
    assert inv.soul_version != pop.soul_version  # provenance distinguishes them


def test_ungrounded_episode_refuses(client: MockLLMClient) -> None:
    ep = curate_episode(_resolve(EGGS), _soul("soul_investor.md"), _context(), client)
    assert ep.refused
    assert ep.refusal_reason == REFUSAL
    assert ep.highlights == []


# --- Excerpts and source metadata for the script stage -------------------------------


@pytest.mark.parametrize(
    ("raw", "clean"),
    [
        (">> So um, the the thing is, uh, margins are are expanding .", "So the thing is, margins are expanding."),
        ("Hmm. Mhm, right.", "right."),
        ("Humble umbrella, uhh, ermine.", "Humble umbrella, ermine."),
        ("  spaced   out  ", "spaced out"),
    ],
)
def test_clean_spoken_text(raw: str, clean: str) -> None:
    from chorus.curation import clean_spoken_text

    assert clean_spoken_text(raw) == clean


def test_labeled_text_marks_speaker_changes_only() -> None:
    from chorus.curation import intro_excerpt, labeled_text
    from chorus.models import Segment

    segments = [
        Segment(start=0, text="Welcome back.", speaker="Harry"),
        Segment(start=5, text="Today, um, Marc is here.", speaker="Harry"),
        Segment(start=200, text="Thanks for having me.", speaker="Marc"),
        Segment(start=400, text="Late line.", speaker="Marc"),
    ]
    assert labeled_text(segments) == (
        "[Harry] Welcome back. Today, Marc is here. [Marc] Thanks for having me. Late line."
    )
    assert labeled_text([Segment(start=0, text="No labels here.")]) == "No labels here."
    assert intro_excerpt(segments) == "[Harry] Welcome back. Today, Marc is here. [Marc] Thanks for having me."


def test_curated_episode_carries_metadata_and_clean_excerpts() -> None:
    from datetime import UTC, datetime

    from chorus.models import ResolvedEpisode

    published = datetime(2026, 9, 30, tzinfo=UTC)
    episode = EpisodeInput(
        video_id=ANDREESSEN, show="a16z", title="The Future", published_at=published, description="Notes."
    )
    transcript = FixtureTranscriptProvider().get(EpisodeInput(video_id=ANDREESSEN))
    digest = curate_episode(
        ResolvedEpisode(episode=episode, transcript=transcript),
        _soul("soul_investor.md"),
        _context(),
        MockLLMClient(),
    )
    assert (digest.show, digest.published_at, digest.description) == ("a16z", published, "Notes.")
    assert digest.intro_excerpt
    for h in digest.highlights:
        assert h.show == "a16z"
        assert h.excerpt and " um " not in f" {h.excerpt} " and ">>" not in h.excerpt
        assert citation_resolves(transcript, h.segment_timestamp, h.quote)
