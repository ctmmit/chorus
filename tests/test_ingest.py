"""Goal 1 — ingest, tested against the real fixtures.

Maps to ROADMAP Goal 1 exit: episodes load; missing-transcript skips with a
logged reason; all-fail returns failed (here: raises AllEpisodesFailed).
"""
from __future__ import annotations

import pytest

from chorus.ingest import AllEpisodesFailed, ingest
from chorus.models import EpisodeInput
from chorus.transcripts import FixtureTranscriptProvider

# Real fixture video ids (see fixtures/episodes.json).
CLEAN = ["gs39QFYIbBY", "c4tvVKDhpiY", "wAnDWfEIwoE", "xKZ_8ULR91Y", "2Ryr95iiYNk"]
MISSING = "KhZfxZ-C-2g"  # Goldman "The New AI Trades" — no captions on purpose
UNGROUNDED = "IAgmW_gTxls"  # scrambled eggs — resolves fine; off-topic, not a load failure


@pytest.fixture
def provider() -> FixtureTranscriptProvider:
    return FixtureTranscriptProvider()


def test_clean_episodes_resolve(provider: FixtureTranscriptProvider) -> None:
    result = ingest([EpisodeInput(video_id=v) for v in CLEAN], provider)
    assert len(result.resolved) == 5
    assert result.skipped == []
    assert all(r.transcript.segments for r in result.resolved)
    assert all(r.transcript.word_count > 0 for r in result.resolved)


def test_missing_transcript_skips_gracefully(provider: FixtureTranscriptProvider) -> None:
    episodes = [EpisodeInput(video_id=v) for v in CLEAN] + [EpisodeInput(video_id=MISSING)]
    result = ingest(episodes, provider)
    assert len(result.resolved) == 5
    assert len(result.skipped) == 1
    assert result.skipped[0].episode.video_id == MISSING
    assert MISSING in result.skipped[0].reason  # structured, identifies the episode


def test_ungrounded_episode_still_loads(provider: FixtureTranscriptProvider) -> None:
    # Off-topic != unloadable. Relevance refusal is a curation concern (Goal 2),
    # not an ingest failure.
    result = ingest([EpisodeInput(video_id=UNGROUNDED)], provider)
    assert len(result.resolved) == 1


def test_all_failed_raises(provider: FixtureTranscriptProvider) -> None:
    episodes = [EpisodeInput(video_id=MISSING), EpisodeInput(video_id="ZZZZZZZZZZZ")]
    with pytest.raises(AllEpisodesFailed):
        ingest(episodes, provider)


def test_empty_input_is_not_a_failure(provider: FixtureTranscriptProvider) -> None:
    result = ingest([], provider)
    assert result.resolved == [] and result.skipped == []


def test_url_is_accepted_and_parsed() -> None:
    ep = EpisodeInput(url="https://www.youtube.com/watch?v=gs39QFYIbBY")
    assert ep.resolved_id() == "gs39QFYIbBY"
