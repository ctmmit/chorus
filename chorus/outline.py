"""Episode outline: the segment plan the script is written against.

The shape follows open-notebook's podcast-creator (an outline of named
segments with a description and a relative size, then one script call per
segment), with the structure Chorus needs enforced in code rather than left
to the prompt:

    intro -> one `source` segment per featured source -> connection(s) -> close

A connection may only come after every source it connects has had its own
segment, so the listener always hears what a source says before hearing how
it relates to anything else. `validate_outline` returns the violations as
plain sentences, which the composer feeds back to the model on its one retry.
"""
from __future__ import annotations

import json
import math
from typing import Any

from pydantic import ValidationError

from chorus.models import (
    MAX_TARGET_MINUTES,
    EpisodeDigest,
    EpisodeOutline,
    OutlineSegment,
    SourceBrief,
)

# Length budget (minutes). A featured source needs room for its setup (who,
# what, context, point), its key moments and the take.
INTRO_MINUTES = 1.0
MINUTES_PER_SOURCE = 3.5
CONNECTION_MINUTES = 1.5
CLOSE_MINUTES = 1.0

SPOKEN_WORDS_PER_MINUTE = 150
WORDS_PER_TURN = 35
MIN_TURNS_PER_SEGMENT = 2
SIZE_WEIGHTS = {"short": 1, "medium": 2, "long": 3}


class OutlineError(ValueError):
    """The model's outline was unusable; the message lists why."""


def budget_minutes(n_sources: int) -> int:
    """Spoken minutes an episode featuring `n_sources` sources needs."""
    if n_sources < 1:
        raise ValueError(f"n_sources must be >= 1, got {n_sources}")
    connection = CONNECTION_MINUTES if n_sources >= 2 else 0.0
    total = INTRO_MINUTES + n_sources * MINUTES_PER_SOURCE + connection + CLOSE_MINUTES
    return min(MAX_TARGET_MINUTES, math.ceil(total))


def sources_for_budget(target_minutes: int, max_sources: int) -> int:
    """How many sources fit in `target_minutes` with proper setup each: the
    largest n whose budget fits, never fewer than one."""
    fitting = [n for n in range(1, max_sources + 1) if budget_minutes(n) <= target_minutes]
    return max(fitting, default=1)


def segment_turn_targets(outline: EpisodeOutline, target_minutes: int) -> list[int]:
    """Spoken lines to aim for in each segment: the episode's total, split by
    each segment's relative size."""
    total_turns = target_minutes * SPOKEN_WORDS_PER_MINUTE / WORDS_PER_TURN
    weights = [SIZE_WEIGHTS[s.size] for s in outline.segments]
    total_weight = sum(weights) or 1
    return [max(MIN_TURNS_PER_SEGMENT, round(total_turns * w / total_weight)) for w in weights]


def validate_outline(outline: EpisodeOutline, featured_ids: list[str]) -> list[str]:
    """Every way `outline` breaks the required structure, as sentences. Empty
    means usable. Pure."""
    problems: list[str] = []
    segments = outline.segments
    if not segments:
        return ["the outline has no segments"]

    kinds = [s.kind for s in segments]
    if kinds[0] != "intro":
        problems.append("the first segment must be the intro")
    if kinds[-1] != "close":
        problems.append("the last segment must be the close")
    if kinds.count("intro") != 1:
        problems.append("there must be exactly one intro segment")
    if kinds.count("close") != 1:
        problems.append("there must be exactly one close segment")

    known = set(featured_ids)
    for seg in segments:
        unknown = [sid for sid in seg.source_ids if sid not in known]
        if unknown:
            problems.append(f"segment {seg.name!r} names unknown source ids {unknown}")

    covered: set[str] = set()
    for seg in segments:
        if seg.kind == "source":
            if len(seg.source_ids) != 1:
                problems.append(f"source segment {seg.name!r} must cover exactly one source id")
                continue
            sid = seg.source_ids[0]
            if sid in covered:
                problems.append(f"source {sid} has more than one source segment")
            covered.add(sid)
        elif seg.kind == "connection":
            if len(seg.source_ids) < 2:
                problems.append(f"connection segment {seg.name!r} must name at least two sources")
            early = [sid for sid in seg.source_ids if sid in known and sid not in covered]
            if early:
                problems.append(
                    f"connection segment {seg.name!r} comes before the source segment of {early}; "
                    "a source must be introduced before it is connected to anything"
                )

    missing = [sid for sid in featured_ids if sid not in covered]
    if missing:
        problems.append(f"sources {missing} have no source segment")
    if len(featured_ids) >= 2 and "connection" not in kinds:
        problems.append("with two or more sources there must be at least one connection segment")
    return problems


def parse_outline(data: Any, featured_ids: list[str], also_noted: list[str]) -> EpisodeOutline:
    """Validate a model's outline reply; raises OutlineError listing every problem."""
    if not isinstance(data, dict):
        raise OutlineError("reply is not a JSON object")
    try:
        outline = EpisodeOutline(
            segments=[OutlineSegment.model_validate(s) for s in data.get("segments") or []],
            also_noted=also_noted,
        )
    except ValidationError as err:
        raise OutlineError(f"segment fields are invalid: {err}") from err
    problems = validate_outline(outline, featured_ids)
    if problems:
        raise OutlineError("; ".join(problems))
    return outline


def outline_prompt(
    briefs: list[SourceBrief], also_noted: list[EpisodeDigest], target_minutes: int
) -> str:
    briefs_json = json.dumps(
        [b.model_dump(mode="json", exclude_none=True) for b in briefs], indent=2
    )
    noted = (
        "\n".join(f"- {ep.show or 'unknown show'}: {ep.episode_title or ep.episode_id}" for ep in also_noted)
        or "(none)"
    )
    connection_rule = (
        "- connection: after the source segments it connects. Name the specific link or "
        "tension between the sources (the same claim seen from two sides, a shared "
        "assumption, a contradiction) and what the listener should make of it.\n"
        if len(briefs) >= 2
        else ""
    )
    return (
        f"FEATURED SOURCES (briefs):\n{briefs_json}\n\n"
        f"ALSO NOTED (one line each in the close, no discussion):\n{noted}\n\n"
        f"Plan an episode of about {target_minutes} spoken minutes. The listener has heard "
        "none of these sources, so each must be introduced before anyone comments on it. "
        "Use these segment kinds, in this order:\n"
        "- intro (short): what today's episode covers and why it matters to this listener. "
        "No commentary yet.\n"
        "- source, one per featured source, exactly one source id each. The description must "
        "follow this arc in order: who is speaking and why they are worth hearing; what the "
        "piece is and when it aired; the context; the core point; the key moments in the "
        "order they occur; then our take.\n"
        f"{connection_rule}"
        "- close (short): the synthesis in a sentence or two, then the also-noted mentions.\n\n"
        "Order the source segments so the connections read naturally. Write each description "
        "as instructions to the scriptwriter: the specific points, numbers and questions the "
        "segment must cover, drawn from the briefs.\n\n"
        "Reply with ONLY a JSON object, no prose and no code fences:\n"
        '{"segments": [{"name": "...", "kind": "intro|source|connection|close", '
        '"source_ids": ["<episode_id>", ...], "description": "...", '
        '"size": "short|medium|long"}]}'
    )


def mock_outline(briefs: list[SourceBrief], also_noted: list[str]) -> EpisodeOutline:
    """Deterministic outline (no model) with the required structure."""
    ids = [b.episode_id for b in briefs]
    segments = [
        OutlineSegment(
            name="Intro", kind="intro", source_ids=ids, description="What today covers.", size="short"
        )
    ]
    for b in briefs:
        segments.append(
            OutlineSegment(
                name=b.title or b.show or b.episode_id,
                kind="source",
                source_ids=[b.episode_id],
                description=f"Introduce the source, then its point: {b.thesis}",
                size="long",
            )
        )
    if len(briefs) >= 2:
        segments.append(
            OutlineSegment(
                name="Connections",
                kind="connection",
                source_ids=ids,
                description="How these sources bear on each other.",
                size="medium",
            )
        )
    segments.append(
        OutlineSegment(name="Close", kind="close", source_ids=[], description="Wrap up.", size="short")
    )
    return EpisodeOutline(segments=segments, also_noted=also_noted)
