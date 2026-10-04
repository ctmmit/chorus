"""Episode outline: the segment plan the script is written against.

The shape follows open-notebook's podcast-creator (an outline of named
segments with a description and a relative size, then one script call per
segment). The writer is the editor: from every candidate source it decides
which ones get airtime, how much, and in what order. One source explored in
depth, several woven into one thread, or a quick run through a handful are
all valid plans.

Code enforces only what keeps an episode easy to follow by ear:

    intro -> one or more body segments -> close

The intro tells the listener what is coming, the close ends on purpose, at
least one body segment discusses a real source, and the plan stays short
enough to hold together. Whether a source is introduced before it is argued
with is a per-segment concern (chorus/script.py `segment_prompt`).
`validate_outline` returns violations as plain sentences, which the composer
feeds back to the model on its one retry.
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

# Fallback length budget (minutes), used when the writer names no length and
# by the offline mock: room for a source's setup, its moments and the take.
INTRO_MINUTES = 1.0
MINUTES_PER_SOURCE = 3.5
CONNECTION_MINUTES = 1.5
CLOSE_MINUTES = 1.0

# The range the writer chooses a length from when the principal set none.
WRITER_MIN_MINUTES = 4
WRITER_MAX_MINUTES = 15
# Each segment is one model call, and past this many the thread fragments.
MAX_SEGMENTS = 10

SPOKEN_WORDS_PER_MINUTE = 150
WORDS_PER_TURN = 35
MIN_TURNS_PER_SEGMENT = 2
SIZE_WEIGHTS = {"short": 1, "medium": 2, "long": 3}
# Segment kinds that carry the episode's substance. "source" and
# "connection" are the earlier fixed structure, still accepted.
BODY_KINDS = frozenset({"body", "source", "connection"})


class OutlineError(ValueError):
    """The model's outline was unusable; the message lists why."""


def budget_minutes(n_sources: int) -> int:
    """Spoken minutes an episode giving `n_sources` sources a full segment each needs."""
    if n_sources < 1:
        raise ValueError(f"n_sources must be >= 1, got {n_sources}")
    connection = CONNECTION_MINUTES if n_sources >= 2 else 0.0
    total = INTRO_MINUTES + n_sources * MINUTES_PER_SOURCE + connection + CLOSE_MINUTES
    return min(MAX_TARGET_MINUTES, math.ceil(total))


def length_bounds(fixed_minutes: int | None) -> tuple[int, int]:
    """(shortest, longest) the episode may run: the principal's fixed length,
    or the range the writer chooses from."""
    if fixed_minutes is not None:
        return fixed_minutes, fixed_minutes
    return WRITER_MIN_MINUTES, WRITER_MAX_MINUTES


def covered_ids(outline: EpisodeOutline) -> list[str]:
    """Source ids some body segment discusses, in order of first appearance."""
    seen: list[str] = []
    for seg in outline.segments:
        if seg.kind in BODY_KINDS:
            seen += [sid for sid in seg.source_ids if sid not in seen]
    return seen


def episode_minutes(outline: EpisodeOutline, fixed_minutes: int | None) -> int:
    """The length the script is written to: fixed, else the writer's choice,
    else a budget from how many sources the outline covers."""
    if fixed_minutes is not None:
        return fixed_minutes
    if outline.target_minutes is not None:
        return outline.target_minutes
    low, high = length_bounds(None)
    return min(high, max(low, budget_minutes(max(1, len(covered_ids(outline))))))


def segment_turn_targets(outline: EpisodeOutline, target_minutes: int) -> list[int]:
    """Spoken lines to aim for in each segment: the episode's total, split by
    each segment's relative size."""
    total_turns = target_minutes * SPOKEN_WORDS_PER_MINUTE / WORDS_PER_TURN
    weights = [SIZE_WEIGHTS[s.size] for s in outline.segments]
    total_weight = sum(weights) or 1
    return [max(MIN_TURNS_PER_SEGMENT, round(total_turns * w / total_weight)) for w in weights]


def validate_outline(
    outline: EpisodeOutline, candidate_ids: list[str], fixed_minutes: int | None = None
) -> list[str]:
    """Every way `outline` would be hard to follow, as sentences. Empty means
    usable. Which sources it covers, and how, is the writer's call. Pure."""
    segments = outline.segments
    if not segments:
        return ["the outline has no segments"]

    problems: list[str] = []
    kinds = [s.kind for s in segments]
    if kinds[0] != "intro":
        problems.append("the first segment must be the intro")
    if kinds[-1] != "close":
        problems.append("the last segment must be the close")
    if kinds.count("intro") != 1:
        problems.append("there must be exactly one intro segment")
    if kinds.count("close") != 1:
        problems.append("there must be exactly one close segment")
    if len(segments) > MAX_SEGMENTS:
        problems.append(f"there are {len(segments)} segments; use at most {MAX_SEGMENTS}")

    known = set(candidate_ids)
    for seg in segments:
        unknown = [sid for sid in seg.source_ids if sid not in known]
        if unknown:
            problems.append(f"segment {seg.name!r} names unknown source ids {unknown}")
    if not any(sid in known for sid in covered_ids(outline)):
        problems.append("no body segment discusses any of the sources")

    low, high = length_bounds(fixed_minutes)
    chosen = outline.target_minutes if fixed_minutes is None else None
    if chosen is not None and not low <= chosen <= high:
        problems.append(f"target_minutes must be between {low} and {high}")
    return problems


def parse_outline(
    data: Any,
    candidate_ids: list[str],
    overflow_ids: list[str],
    fixed_minutes: int | None = None,
) -> EpisodeOutline:
    """Validate a model's outline reply; raises OutlineError listing every
    problem. Candidates no segment covers, then the overflow, become the
    also-noted list."""
    if not isinstance(data, dict):
        raise OutlineError("reply is not a JSON object")
    raw_minutes = data.get("target_minutes")
    try:
        chosen = None if raw_minutes is None else int(raw_minutes)
    except (TypeError, ValueError) as err:
        raise OutlineError("target_minutes must be a whole number of minutes") from err
    try:
        outline = EpisodeOutline(
            segments=[OutlineSegment.model_validate(s) for s in data.get("segments") or []],
            target_minutes=fixed_minutes if fixed_minutes is not None else chosen,
        )
    except ValidationError as err:
        raise OutlineError(f"segment fields are invalid: {err}") from err
    problems = validate_outline(outline, candidate_ids, fixed_minutes)
    if problems:
        raise OutlineError("; ".join(problems))
    covered = set(covered_ids(outline))
    outline.also_noted = [sid for sid in candidate_ids if sid not in covered] + overflow_ids
    return outline


def outline_prompt(
    briefs: list[SourceBrief],
    overflow: list[EpisodeDigest],
    fixed_minutes: int | None,
    threads: str = "",
) -> str:
    """`threads` is chorus.threads.threads_brief's text: questions two or more
    sources speak to, offered as candidate body segments, not required ones."""
    thread_block = (
        "THREADS ACROSS SOURCES (questions two or more sources speak to, with each source's "
        "stance; a disagreement makes a strong body segment that puts those sources in "
        f"conversation, if it earns its time):\n{threads}\n\n"
        if threads
        else ""
    )
    briefs_json = json.dumps(
        [b.model_dump(mode="json", exclude_none=True) for b in briefs], indent=2
    )
    unbriefed = (
        "\n".join(f"- {ep.show or 'unknown show'}: {ep.episode_title or ep.episode_id}" for ep in overflow)
        or "(none)"
    )
    if fixed_minutes is not None:
        length = (
            f"The episode runs about {fixed_minutes} spoken minutes; fit the plan to that and "
            f"set target_minutes to {fixed_minutes}."
        )
    else:
        low, high = length_bounds(None)
        length = (
            f"Choose the length, a whole number of spoken minutes from {low} to {high}, to fit "
            "what you decide to cover. Don't pad a thin week or cram a rich one."
        )
    return (
        f"CANDIDATE SOURCES (briefs, most relevant to this listener first):\n{briefs_json}\n\n"
        f"NOT BRIEFED (more than could be prepared; at most a passing mention in the close):\n"
        f"{unbriefed}\n\n"
        f"{thread_block}"
        f"{length}\n\n"
        "Plan the episode. You choose what makes it in and how much each thing gets: one "
        "source explored in depth, a few set against each other, an idea followed across "
        "several, a quick run through a handful, or any mix. Leave out what doesn't earn its "
        "time; anything left out can get a one-line mention in the close.\n\n"
        "Segment kinds:\n"
        "- intro (first, exactly one): what this episode is about and why this listener "
        "should care.\n"
        f"- body (as many as the material deserves, at most {MAX_SEGMENTS - 2}): any number of "
        "source ids. A body segment can stay with one source, put two in conversation, or "
        "follow a thread across several.\n"
        "- close (last, exactly one): land the episode on purpose, and mention anything left "
        "out that is still worth knowing about.\n\n"
        "What keeps it easy to follow by ear:\n"
        "- One thread at a time, with a reason to move from each segment to the next.\n"
        "- A source is introduced the first time it is discussed, before anyone argues with it, "
        "so its first real appearance comes before any segment that leans on it.\n"
        "- Vary the pace. Size each segment by what it is worth (short, medium, long).\n\n"
        "Write each description as notes to yourself as the scriptwriter: the specific points, "
        "numbers, tensions and questions the segment covers, drawn from the briefs.\n\n"
        "Reply with ONLY a JSON object, no prose and no code fences:\n"
        '{"target_minutes": <minutes>, "segments": [{"name": "...", "kind": "intro|body|close", '
        '"source_ids": ["<episode_id>", ...], "description": "...", '
        '"size": "short|medium|long"}]}'
    )


def mock_outline(
    briefs: list[SourceBrief], overflow_ids: list[str], fixed_minutes: int | None = None
) -> EpisodeOutline:
    """Deterministic outline (no model): a body segment for each brief that
    fits the length, best first, then one tying them together."""
    _, longest = length_bounds(fixed_minutes)
    fitting = [n for n in range(1, len(briefs) + 1) if budget_minutes(n) <= longest]
    chosen = briefs[: max(fitting, default=1)]
    ids = [b.episode_id for b in chosen]
    segments = [
        OutlineSegment(
            name="Intro", kind="intro", source_ids=ids, description="What today covers.", size="short"
        )
    ]
    for b in chosen:
        segments.append(
            OutlineSegment(
                name=b.title or b.show or b.episode_id,
                kind="body",
                source_ids=[b.episode_id],
                description=f"Introduce the source, then its point: {b.thesis}",
                size="long",
            )
        )
    if len(chosen) >= 2:
        segments.append(
            OutlineSegment(
                name="Connections",
                kind="body",
                source_ids=ids,
                description="How these sources bear on each other.",
                size="medium",
            )
        )
    segments.append(
        OutlineSegment(name="Close", kind="close", source_ids=[], description="Wrap up.", size="short")
    )
    outline = EpisodeOutline(segments=segments)
    outline.target_minutes = episode_minutes(outline, fixed_minutes)
    outline.also_noted = [b.episode_id for b in briefs[len(chosen):]] + overflow_ids
    return outline
