"""Scorer excerpts: a highlight quotes the most relevant verbatim span of its
window, and only when that span really is in the window.

Runs on the redistributable synthetic transcript, so it needs no private
fixtures. A scripted scorer plays the model; nothing calls a provider.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from chorus.curation import (
    QUOTE_WORDS,
    citation_resolves,
    curate_episode,
    grounded_excerpt,
    window_segments,
)
from chorus.ingest import ingest
from chorus.llm import MockLLMClient, ScoredWindow, TokenUsage
from chorus.models import EpisodeInput, ResolvedEpisode, Segment, Transcript
from chorus.transcripts import FixtureTranscriptProvider

FIX = Path(__file__).resolve().parent.parent / "fixtures"
SAMPLE = "sample_public"
MARGINS = "Gross margins went from forty percent to seventy percent in two quarters."
MARGINS_AT = 20.4
WELCOME = "Welcome back, today we are talking with an operator"


class _Scripted:
    """An LLMClient that returns fixed results, one per window."""

    def __init__(self, results: list[ScoredWindow]) -> None:
        self.results = results

    def score_segment(self, text: str, soul: str, context: str) -> tuple[float, str]:
        raise NotImplementedError

    def score_windows(
        self, windows: list[str], soul: str, context: str, meter: TokenUsage | None = None
    ) -> list[ScoredWindow]:
        assert len(windows) == len(self.results)
        return self.results


@pytest.fixture
def sample() -> ResolvedEpisode:
    return ingest([EpisodeInput(video_id=SAMPLE)], FixtureTranscriptProvider()).resolved[0]


def _first_window_only(excerpt: str | None) -> _Scripted:
    """Window 0 clears the bar (optionally with an excerpt); the rest do not."""
    first: ScoredWindow = (0.9, "margin claim", excerpt) if excerpt is not None else (0.9, "margin claim")
    return _Scripted([first, (0.0, "chatter"), (0.0, "later")])


def _curate(resolved: ResolvedEpisode, client: _Scripted | MockLLMClient, soul: str = "") -> list:
    return curate_episode(resolved, soul, "", client).highlights


# --- the two outcomes ------------------------------------------------------


def test_valid_mid_window_excerpt_is_the_quote(sample: ResolvedEpisode) -> None:
    [h] = _curate(sample, _first_window_only(MARGINS))
    assert h.quote == MARGINS
    assert h.segment_timestamp == MARGINS_AT  # the segment the excerpt starts in
    assert citation_resolves(sample.transcript, h.segment_timestamp, h.quote)


def test_fabricated_excerpt_is_rejected_and_falls_back(sample: ResolvedEpisode) -> None:
    fabricated = "Gross margins rose from 40% to 70% after inference costs collapsed."
    [h] = _curate(sample, _first_window_only(fabricated))
    assert "40%" not in h.quote
    assert h.quote.startswith(WELCOME)
    assert h.segment_timestamp == 0.0
    assert citation_resolves(sample.transcript, h.segment_timestamp, h.quote)


def test_no_excerpt_keeps_the_window_opening(sample: ResolvedEpisode) -> None:
    [h] = _curate(sample, _first_window_only(None))
    assert h.quote.startswith(WELCOME)
    assert h.segment_timestamp == 0.0
    assert citation_resolves(sample.transcript, h.segment_timestamp, h.quote)


# --- what counts as verbatim -----------------------------------------------


@pytest.mark.parametrize(
    "excerpt",
    [
        f"  {MARGINS.replace(' ', '   ', 3)}\n",  # whitespace is normalised
        f'"{MARGINS}"',  # wrapping quote marks are not part of the span
        f"...{MARGINS[:-1]}...",  # nor are wrapping ellipses
        f"“{MARGINS}”",
    ],
)
def test_wrapping_whitespace_and_quote_marks_are_tolerated(
    sample: ResolvedEpisode, excerpt: str
) -> None:
    [h] = _curate(sample, _first_window_only(excerpt))
    assert h.segment_timestamp == MARGINS_AT
    assert h.quote.startswith("Gross margins went from forty percent")
    assert citation_resolves(sample.transcript, h.segment_timestamp, h.quote)


@pytest.mark.parametrize(
    ("excerpt", "why"),
    [
        ("gross margins went from forty percent to seventy percent", "case differs"),
        ("ross margins went from forty percent to seventy", "starts mid-word"),
        ("Gross margins went from forty percent to seventy perc", "ends mid-word"),
        ("Gross margins went", "too short to verify"),
        ("Gross margins went from forty percent. That cost curve is the mechanism", "joined"),
        ("Pricing power moves to the two players with the licenses", "another window"),
    ],
)
def test_non_verbatim_excerpts_fall_back(sample: ResolvedEpisode, excerpt: str, why: str) -> None:
    [h] = _curate(sample, _first_window_only(excerpt))
    assert h.segment_timestamp == 0.0, why
    assert h.quote.startswith(WELCOME), why
    assert citation_resolves(sample.transcript, h.segment_timestamp, h.quote)


def test_excerpt_spanning_segments_is_stamped_where_it_starts(sample: ResolvedEpisode) -> None:
    excerpt = "in two quarters. That cost curve is the mechanism, and it is falsifiable"
    [h] = _curate(sample, _first_window_only(excerpt))
    assert h.quote == excerpt
    assert h.segment_timestamp == MARGINS_AT
    assert citation_resolves(sample.transcript, h.segment_timestamp, h.quote)


def test_long_excerpt_is_capped_like_any_quote(sample: ResolvedEpisode) -> None:
    window = window_segments(sample.transcript.segments)[0]
    excerpt = " ".join(window.text.split()[20:70])
    [h] = _curate(sample, _first_window_only(excerpt))
    assert h.quote.endswith("...")
    assert len(h.quote.removesuffix("...").split()) == QUOTE_WORDS
    assert citation_resolves(sample.transcript, h.segment_timestamp, h.quote)


def test_excerpt_with_its_own_ellipsis_resolves() -> None:
    """An ellipsis inside the transcript is text, not our truncation marker."""
    segments = [
        Segment(start=0.0, text="Intro chatter before the point."),
        Segment(start=8.0, text="And the answer is... margins doubled in a single year, honestly."),
    ]
    resolved = ResolvedEpisode(
        episode=EpisodeInput(video_id="ellipsis"),
        transcript=Transcript(video_id="ellipsis", segments=segments),
    )
    excerpt = "the answer is... margins doubled in a single year"
    [h] = _curate(resolved, _Scripted([(0.9, "r", excerpt)]))
    assert (h.segment_timestamp, h.quote) == (8.0, excerpt)
    assert citation_resolves(resolved.transcript, h.segment_timestamp, h.quote)


def test_grounded_excerpt_maps_offsets_past_blank_segments() -> None:
    segments = [
        Segment(start=0.0, text="First segment has some words."),
        Segment(start=4.0, text="   "),
        Segment(start=6.0, text="Second segment carries the actual claim we want."),
    ]
    [window] = window_segments(segments)
    grounded = grounded_excerpt(window, "Second segment carries the actual claim")
    assert grounded == (6.0, "Second segment carries the actual claim")


# --- the mock scorer -------------------------------------------------------


def test_mock_points_past_the_filler_and_stays_deterministic(sample: ResolvedEpisode) -> None:
    soul = (FIX / "souls" / "soul_investor.md").read_text(encoding="utf-8")
    first = _curate(sample, MockLLMClient(), soul)
    again = _curate(sample, MockLLMClient(), soul)
    assert [(h.segment_timestamp, h.quote) for h in first] == [
        (h.segment_timestamp, h.quote) for h in again
    ]
    assert first
    for h in first:
        assert not h.quote.startswith("Welcome back")
        assert citation_resolves(sample.transcript, h.segment_timestamp, h.quote)
