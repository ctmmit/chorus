"""Outline structure, length budget and per-segment line targets (pure)."""
from __future__ import annotations

import pytest

from chorus.models import EpisodeOutline, OutlineSegment
from chorus.outline import (
    OutlineError,
    budget_minutes,
    parse_outline,
    segment_turn_targets,
    sources_for_budget,
    validate_outline,
)


def _seg(kind: str, *ids: str, size: str = "medium") -> OutlineSegment:
    return OutlineSegment(name=kind, kind=kind, source_ids=list(ids), description="d", size=size)  # type: ignore[arg-type]


def _outline(*segments: OutlineSegment) -> EpisodeOutline:
    return EpisodeOutline(segments=list(segments))


GOOD = _outline(_seg("intro"), _seg("source", "a"), _seg("source", "b"), _seg("connection", "a", "b"), _seg("close"))


def test_well_formed_outline_has_no_problems() -> None:
    assert validate_outline(GOOD, ["a", "b"]) == []
    assert validate_outline(_outline(_seg("intro"), _seg("source", "a"), _seg("close")), ["a"]) == []


def test_missing_intro_or_close_is_rejected() -> None:
    problems = validate_outline(_outline(_seg("source", "a")), ["a"])
    assert any("intro" in p for p in problems) and any("close" in p for p in problems)


def test_missing_or_duplicated_source_is_rejected() -> None:
    missing = validate_outline(_outline(_seg("intro"), _seg("source", "a"), _seg("close")), ["a", "b"])
    assert any("['b'] have no source segment" in p for p in missing)
    dup = validate_outline(
        _outline(_seg("intro"), _seg("source", "a"), _seg("source", "a"), _seg("close")), ["a"]
    )
    assert any("more than one source segment" in p for p in dup)


def test_two_sources_need_a_connection() -> None:
    problems = validate_outline(
        _outline(_seg("intro"), _seg("source", "a"), _seg("source", "b"), _seg("close")), ["a", "b"]
    )
    assert any("connection" in p for p in problems)


def test_connection_before_a_source_is_introduced_is_rejected() -> None:
    problems = validate_outline(
        _outline(_seg("intro"), _seg("source", "a"), _seg("connection", "a", "b"), _seg("source", "b"), _seg("close")),
        ["a", "b"],
    )
    assert any("introduced before it is connected" in p for p in problems)


def test_unknown_source_ids_are_rejected() -> None:
    problems = validate_outline(_outline(_seg("intro", "zzz"), _seg("source", "a"), _seg("close")), ["a"])
    assert any("unknown source ids" in p for p in problems)


def test_parse_outline_raises_with_every_problem() -> None:
    with pytest.raises(OutlineError, match="intro.*close"):
        parse_outline({"segments": [{"name": "s", "kind": "source", "source_ids": ["a"], "description": "d"}]}, ["a"], [])
    with pytest.raises(OutlineError):
        parse_outline(["not", "an", "object"], ["a"], [])
    parsed = parse_outline(GOOD.model_dump(), ["a", "b"], ["c"])
    assert parsed.also_noted == ["c"]


def test_budget_grows_with_sources_and_fits_back() -> None:
    assert budget_minutes(1) == 6
    assert budget_minutes(2) == 11
    assert budget_minutes(3) == 14
    assert sources_for_budget(14, 3) == 3
    assert sources_for_budget(11, 3) == 2
    assert sources_for_budget(5, 3) == 1  # never zero
    with pytest.raises(ValueError):
        budget_minutes(0)


def test_segment_targets_follow_size_and_total() -> None:
    outline = _outline(_seg("intro", size="short"), _seg("source", "a", size="long"), _seg("close", size="short"))
    targets = segment_turn_targets(outline, 6)
    assert targets[1] > targets[0] == targets[2]
    assert abs(sum(targets) - round(6 * 150 / 35)) <= 1
    assert min(segment_turn_targets(outline, 1)) >= 2
