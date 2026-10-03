"""Outline coherence rules, writer-chosen length and per-segment line targets (pure)."""
from __future__ import annotations

import pytest

from chorus.models import EpisodeOutline, OutlineSegment, SourceBrief
from chorus.outline import (
    MAX_SEGMENTS,
    WRITER_MAX_MINUTES,
    WRITER_MIN_MINUTES,
    OutlineError,
    budget_minutes,
    covered_ids,
    episode_minutes,
    length_bounds,
    mock_outline,
    outline_prompt,
    parse_outline,
    segment_turn_targets,
    validate_outline,
)


def _seg(kind: str, *ids: str, size: str = "medium") -> OutlineSegment:
    return OutlineSegment(name=kind, kind=kind, source_ids=list(ids), description="d", size=size)  # type: ignore[arg-type]


def _outline(*segments: OutlineSegment, minutes: int | None = None) -> EpisodeOutline:
    return EpisodeOutline(segments=list(segments), target_minutes=minutes)


FIVE = ["a", "b", "c", "d", "e"]


def test_one_deep_segment_out_of_five_candidates_is_fine() -> None:
    assert validate_outline(_outline(_seg("intro"), _seg("body", "c", size="long"), _seg("close")), FIVE) == []


def test_a_body_segment_may_span_several_sources_without_a_connection() -> None:
    outline = _outline(_seg("intro", "a"), _seg("body", "a", "b", "d"), _seg("body", "e"), _seg("close"))
    assert validate_outline(outline, FIVE) == []
    assert covered_ids(outline) == ["a", "b", "d", "e"]


def test_legacy_source_and_connection_kinds_still_validate() -> None:
    legacy = _outline(_seg("intro"), _seg("source", "a"), _seg("source", "b"), _seg("connection", "a", "b"), _seg("close"))
    assert validate_outline(legacy, ["a", "b"]) == []
    assert covered_ids(legacy) == ["a", "b"]


def test_missing_intro_or_close_is_rejected() -> None:
    problems = validate_outline(_outline(_seg("body", "a")), ["a"])
    assert any("intro" in p for p in problems) and any("close" in p for p in problems)


def test_an_outline_that_discusses_no_source_is_rejected() -> None:
    problems = validate_outline(_outline(_seg("intro", "a"), _seg("body"), _seg("close")), ["a"])
    assert any("no body segment discusses" in p for p in problems)


def test_unknown_source_ids_are_rejected() -> None:
    problems = validate_outline(_outline(_seg("intro", "zzz"), _seg("body", "a"), _seg("close")), ["a"])
    assert any("unknown source ids" in p for p in problems)


def test_too_many_segments_are_rejected() -> None:
    bodies = [_seg("body", "a") for _ in range(MAX_SEGMENTS - 1)]
    problems = validate_outline(_outline(_seg("intro"), *bodies, _seg("close")), ["a"])
    assert any(f"at most {MAX_SEGMENTS}" in p for p in problems)


def test_writer_length_must_fall_in_range_unless_fixed() -> None:
    base = (_seg("intro"), _seg("body", "a"), _seg("close"))
    assert validate_outline(_outline(*base, minutes=WRITER_MAX_MINUTES), ["a"]) == []
    too_long = validate_outline(_outline(*base, minutes=WRITER_MAX_MINUTES + 1), ["a"])
    assert any("target_minutes" in p for p in too_long)
    assert validate_outline(_outline(*base, minutes=2), ["a"], fixed_minutes=2) == []


def test_parse_outline_raises_with_every_problem() -> None:
    with pytest.raises(OutlineError, match="intro.*close"):
        parse_outline({"segments": [{"name": "s", "kind": "body", "source_ids": ["a"], "description": "d"}]}, ["a"], [])
    with pytest.raises(OutlineError):
        parse_outline(["not", "an", "object"], ["a"], [])
    with pytest.raises(OutlineError, match="whole number"):
        parse_outline({"target_minutes": "long", "segments": []}, ["a"], [])


def test_parse_outline_notes_uncovered_candidates_then_overflow() -> None:
    reply = _outline(_seg("intro"), _seg("body", "b"), _seg("close")).model_dump()
    reply["target_minutes"] = 7
    parsed = parse_outline(reply, ["a", "b", "c"], ["z"])
    assert parsed.also_noted == ["a", "c", "z"]
    assert parsed.target_minutes == 7


def test_a_fixed_length_overrides_the_writers_choice() -> None:
    reply = _outline(_seg("intro"), _seg("body", "a"), _seg("close")).model_dump()
    reply["target_minutes"] = 12
    assert parse_outline(reply, ["a"], [], fixed_minutes=3).target_minutes == 3


def test_length_comes_from_fixed_then_writer_then_budget() -> None:
    outline = _outline(_seg("intro"), _seg("body", "a"), _seg("body", "b"), _seg("close"))
    assert episode_minutes(outline, 3) == 3
    assert episode_minutes(_outline(*outline.segments, minutes=9), None) == 9
    assert episode_minutes(outline, None) == budget_minutes(2)
    assert length_bounds(None) == (WRITER_MIN_MINUTES, WRITER_MAX_MINUTES)
    assert length_bounds(6) == (6, 6)


def test_budget_grows_with_sources() -> None:
    assert budget_minutes(1) == 6
    assert budget_minutes(2) == 11
    assert budget_minutes(3) == 14
    with pytest.raises(ValueError):
        budget_minutes(0)


def _brief(eid: str) -> SourceBrief:
    return SourceBrief(episode_id=eid, title=f"T {eid}", context="c", thesis="t")


def test_mock_outline_covers_what_fits_and_notes_the_rest() -> None:
    briefs = [_brief(x) for x in FIVE]
    outline = mock_outline(briefs, ["z"])
    assert covered_ids(outline) == ["a", "b", "c"]  # budget_minutes(4) exceeds the writer's max
    assert outline.also_noted == ["d", "e", "z"]
    assert validate_outline(outline, FIVE) == []
    short = mock_outline(briefs, [], fixed_minutes=5)
    assert covered_ids(short) == ["a"] and short.target_minutes == 5


def test_outline_prompt_offers_choice_and_the_length_rule() -> None:
    prompt = outline_prompt([_brief("a")], [], None)
    assert "You choose what makes it in" in prompt
    assert f"from {WRITER_MIN_MINUTES} to {WRITER_MAX_MINUTES}" in prompt
    assert "set target_minutes to 8" in outline_prompt([_brief("a")], [], 8)


def test_segment_targets_follow_size_and_total() -> None:
    outline = _outline(_seg("intro", size="short"), _seg("body", "a", size="long"), _seg("close", size="short"))
    targets = segment_turn_targets(outline, 6)
    assert targets[1] > targets[0] == targets[2]
    assert abs(sum(targets) - round(6 * 150 / 35)) <= 1
    assert min(segment_turn_targets(outline, 1)) >= 2
