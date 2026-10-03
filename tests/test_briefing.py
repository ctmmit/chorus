"""Source selection and brief validation (pure)."""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from chorus.briefing import (
    MAX_CANDIDATE_SOURCES,
    BriefError,
    brief_prompt,
    mock_brief,
    name_supported,
    parse_brief,
    select_sources,
)
from chorus.models import Digest, EpisodeDigest, Highlight


def _hl(eid: str, ts: float, score: float, excerpt: str = "x") -> Highlight:
    return Highlight(
        episode_id=eid,
        episode_title=eid,
        segment_timestamp=ts,
        quote=excerpt,
        relevance_score=score,
        why_surface=f"why {eid}",
        excerpt=excerpt,
    )


def _ep(eid: str, *scores: float, refused: bool = False) -> EpisodeDigest:
    return EpisodeDigest(
        episode_id=eid,
        episode_title=f"Title {eid}",
        highlights=[_hl(eid, 10.0 * i, s) for i, s in enumerate(scores)],
        refused=refused,
    )


def test_select_sources_ranks_by_top_highlights_and_caps() -> None:
    digest = Digest(
        soul_version="v",
        episodes=[_ep("one-lucky", 0.95), _ep("steady", 0.7, 0.7, 0.7), _ep("mid", 0.8, 0.5), _ep("x", 0.4)],
    )
    candidates, overflow = select_sources(digest, max_sources=2)
    assert [e.episode_id for e in candidates] == ["steady", "mid"]
    assert [e.episode_id for e in overflow] == ["one-lucky", "x"]


def test_every_source_is_a_candidate_up_to_the_brief_budget() -> None:
    many = Digest(soul_version="v", episodes=[_ep(f"e{i:02d}", 0.5) for i in range(MAX_CANDIDATE_SOURCES + 2)])
    candidates, overflow = select_sources(many)
    assert len(candidates) == MAX_CANDIDATE_SOURCES
    assert [e.episode_id for e in overflow] == [f"e{MAX_CANDIDATE_SOURCES}", f"e{MAX_CANDIDATE_SOURCES + 1}"]


def test_select_sources_skips_refused_and_empty() -> None:
    digest = Digest(soul_version="v", episodes=[_ep("r", refused=True), _ep("ok", 0.5)])
    featured, noted = select_sources(digest)
    assert [e.episode_id for e in featured] == ["ok"] and noted == []
    with pytest.raises(ValueError):
        select_sources(digest, max_sources=0)


EPISODE = EpisodeDigest(
    episode_id="ep1",
    episode_title="The Future of Venture",
    show="20VC",
    published_at=datetime(2026, 9, 30, tzinfo=UTC),
    description="Marc Andreessen joins Harry Stebbings. More at a16z.com.",
    intro_excerpt="[Speaker 0] Welcome back.",
    highlights=[_hl("ep1", 120.0, 0.9, "[Speaker 1] Margins are expanding.")],
)


def test_name_supported_requires_every_token_in_the_material() -> None:
    corpus = "marc andreessen joins harry stebbings"
    assert name_supported("Marc Andreessen", corpus)
    assert not name_supported("Marc Benioff", corpus)
    assert not name_supported("Speaker", corpus)


def test_parse_brief_drops_invented_people_and_unknown_timestamps() -> None:
    brief = parse_brief(
        {
            "people": [
                {"name": "Marc Andreessen", "role": "guest", "credential": "co-founder of a16z"},
                {"name": "Ben Horowitz", "role": "guest"},
                {"name": "Harry Stebbings", "role": "nobody"},  # invalid role
            ],
            "context": "A new fund.",
            "thesis": "Margins keep expanding.",
            "key_points": [
                {"text": "Margins expand", "timestamp": 120.3},
                {"text": "Invented", "timestamp": 999},
                {"text": "", "timestamp": 120},
            ],
        },
        EPISODE,
    )
    assert [p.name for p in brief.people] == ["Marc Andreessen"]
    assert [(p.text, p.segment_timestamp) for p in brief.key_points] == [("Margins expand", 120.0)]
    assert (brief.show, brief.title, brief.published_at) == ("20VC", "The Future of Venture", EPISODE.published_at)


def test_parse_brief_requires_context_and_thesis() -> None:
    with pytest.raises(BriefError):
        parse_brief({"context": "c", "thesis": " "}, EPISODE)
    with pytest.raises(BriefError):
        parse_brief("nope", EPISODE)


def test_brief_prompt_and_mock_brief_use_the_metadata() -> None:
    prompt = brief_prompt(EPISODE)
    assert "Show: 20VC" in prompt and "Published: 30 Sep 2026" in prompt
    assert "timestamp=120 (2:00)" in prompt and "Margins are expanding." in prompt

    brief = mock_brief(EPISODE)
    assert brief.context == "Marc Andreessen joins Harry Stebbings."
    assert brief.thesis == "why ep1"
    assert brief.key_points[0].segment_timestamp == 120.0
