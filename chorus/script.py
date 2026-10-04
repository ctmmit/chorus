"""Script synthesis: brief each source, outline the episode, write it segment by segment.

A listener needs context before commentary: who is talking, what the piece
is, when it aired, why it exists and what it argues, and only then the take.
The earlier composer handed the model an opaque episode id, a timestamp and
the first 28 words of a transcript window, and asked for a monologue in one
call. The result had no setup, no order and no transitions.

The pipeline now follows open-notebook's podcast-creator: an outline of
segments, then one model call per segment, in order. Each call sees the full
outline, every line written so far, the current segment, whether it is the
last one, and how many lines to aim for. Chorus adds two things on top:

1. A `SourceBrief` per candidate source (chorus/briefing.py), so the script
   can introduce whichever sources it uses accurately.
2. An outline in which the writer chooses which sources get airtime, how
   much, and the episode's length (chorus/outline.py). Code enforces only
   what keeps it followable by ear: an intro, body segments, a close.

Grounding (ENGINEERING_REVIEW §9.2) now has two kinds. A line that states
something about a source cites either a surfaced highlight (timestamp) or the
source's brief (no timestamp: who, what, when, thesis). A framing, transition
or opinion line may cite nothing, but then `validate_line` rejects it if it
carries a number or a name the source material doesn't contain. A segment
with invalid lines is re-asked once with the problems listed; lines that are
still invalid are dropped and logged.

Monologue and dialogue share this path. The profile only decides who speaks
(host alone, or host and cohost) and how (personas, tone).

R18: every `Script` carries `voices` (role -> voice id) for the renderer.
R20: `ScriptError` when the digest had highlights but no grounded line
survived. "Nothing cleared the bar" is reserved for a digest with nothing
to discuss.
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
from dataclasses import dataclass
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ValidationError

from chorus.briefing import (
    BRIEF_SYSTEM,
    BriefError,
    brief_prompt,
    format_timestamp,
    mock_brief,
    parse_brief,
    select_sources,
    source_material,
)
from chorus.errors import TerminalError
from chorus.models import (
    HOST_PERSONA_IS_SOUL,
    MONOLOGUE_PROFILE,
    TAKE_TYPES,
    Citation,
    ConversationStyle,
    Digest,
    EpisodeDigest,
    EpisodeOutline,
    EpisodeProfile,
    OutlineSegment,
    Script,
    SourceBrief,
    SpeakerProfile,
    Take,
    Turn,
)
from chorus.outline import (
    BODY_KINDS,
    SPOKEN_WORDS_PER_MINUTE,
    WORDS_PER_TURN,
    OutlineError,
    covered_ids,
    episode_minutes,
    length_bounds,
    mock_outline,
    outline_prompt,
    parse_outline,
    segment_turn_targets,
)

log = logging.getLogger("chorus.script")


class ScriptError(TerminalError):
    """Script synthesis produced nothing usable despite grounded material
    being available (digest had highlights, no grounded line survived) —
    a model-format/grounding failure, not an honest empty digest. Terminal:
    re-running the same digest through the same broken parse won't change
    the outcome; the caller's degrade path (chorus/pipeline.py) records it
    as "script synthesis failed" rather than silently rendering an empty
    episode."""


NOTHING_CLEARED = "Nothing cleared the bar this week."
SPEAKER_MOVES = ("setup", *TAKE_TYPES)
# A segment may run somewhat past its line target, never unboundedly.
LINE_CAP_SLACK = 1.5
# Capitalized words an uncited line may use without naming anyone.
_COMMON_CAPITALIZED = frozenset(
    ["i", "i'm", "i'd", "i've", "i'll", "ok", "okay", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday", "january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december"]
)
_WORD_RE = re.compile(r"[A-Za-z][\w'’-]*")
_DIGIT_RE = re.compile(r"\d")
_CLAUSE_SPLIT_RE = re.compile(r"(?<=[.!?:;])\s+|[\"“”(—]")


def _max_turns(target_minutes: int) -> int:
    """~150 wpm / ~35 words-per-turn, so a 5-minute episode is about 21 lines."""
    return max(1, round(target_minutes * SPOKEN_WORDS_PER_MINUTE / WORDS_PER_TURN))


def max_turns_for(profile: EpisodeProfile) -> int:
    """The most lines an episode under `profile` may run: its fixed length,
    or the longest the writer may choose."""
    return _max_turns(length_bounds(profile.style.target_minutes)[1])


def _speaker(profile: EpisodeProfile, role: str) -> SpeakerProfile:
    for s in profile.speakers:
        if s.role == role:
            return s
    raise ValueError(f"profile {profile.name!r} has no {role!r} speaker")


def _speaker_persona(speaker: SpeakerProfile, soul: str) -> str:
    """The host's persona defaults to the sentinel "the soul" (see
    chorus/models.py): resolve it against the request's actual soul text."""
    return soul if speaker.persona == HOST_PERSONA_IS_SOUL else speaker.persona


def _style_block(style: ConversationStyle, target_minutes: int) -> str:
    engagement = ", ".join(style.engagement) if style.engagement else "none specified"
    return (
        f"Tone: {style.tone or 'unspecified'}\n"
        f"Engagement techniques to use: {engagement}\n"
        f"Target length: ~{target_minutes} minute(s)"
    )


def _voices_for(profile: EpisodeProfile) -> dict[str, str | None]:
    """Role -> voice_id map (R18) so a renderer can look up each speaker's
    requested voice without threading the whole profile through render()."""
    return {s.role: s.voice_id for s in profile.speakers}


def _turns_transcript(turns: list[Turn]) -> str:
    """The readable transcript for a dialogue script — what `monologue` holds
    in dialogue format (spec: "HOST: ...\\n\\nCOHOST: ...")."""
    if not turns:
        return NOTHING_CLEARED
    return "\n\n".join(f"{t.speaker.upper()}: {t.text}" for t in turns)


# --- Episode plan -------------------------------------------------------------------


@dataclass(frozen=True)
class EpisodePlan:
    """The sources the writer may draw on (briefed, best first), those past
    the brief budget, and the principal's fixed length if they set one. Which
    candidates make the episode, and its length otherwise, is the outline's call."""

    candidates: list[EpisodeDigest]
    overflow: list[EpisodeDigest]
    fixed_minutes: int | None


def plan_episode(digest: Digest, profile: EpisodeProfile) -> EpisodePlan:
    """Pure."""
    candidates, overflow = select_sources(digest)
    return EpisodePlan(
        candidates=candidates, overflow=overflow, fixed_minutes=profile.style.target_minutes
    )


@dataclass(frozen=True)
class EpisodeCast:
    """What the outline chose: the sources it discusses (in order of first
    appearance), the ones it only mentions, and the length to write to."""

    featured: list[EpisodeDigest]
    also_noted: list[EpisodeDigest]
    target_minutes: int


def cast_episode(plan: EpisodePlan, outline: EpisodeOutline) -> EpisodeCast:
    """Pure."""
    by_id = {ep.episode_id: ep for ep in [*plan.candidates, *plan.overflow]}
    return EpisodeCast(
        featured=[by_id[sid] for sid in covered_ids(outline) if sid in by_id],
        also_noted=[by_id[sid] for sid in outline.also_noted if sid in by_id],
        target_minutes=episode_minutes(outline, plan.fixed_minutes),
    )


# --- Line validation ----------------------------------------------------------------


class DraftCite(BaseModel):
    episode_id: str
    timestamp: float | None = None


class DraftLine(BaseModel):
    """One line as the model returns it, before validation."""

    speaker: str
    text: str
    move: str | None = None
    cites: list[DraftCite] = []


@dataclass(frozen=True)
class LineContext:
    """What a line may rest on: who may speak, which sources and moments
    exist, and the vocabulary an uncited line may draw names from."""

    speakers: frozenset[str]
    citable_ids: frozenset[str]
    highlights: dict[tuple[str, int], float]
    vocabulary: frozenset[str]


def _normalize_word(word: str) -> str:
    word = word.lower().replace("’", "'")
    return word[:-2] if word.endswith("'s") else word


def vocabulary_of(*texts: str) -> frozenset[str]:
    return frozenset(_normalize_word(w) for text in texts for w in _WORD_RE.findall(text))


def unknown_names(text: str, vocabulary: frozenset[str]) -> list[str]:
    """Capitalized words that are not at the start of a clause and do not
    appear in `vocabulary`: names an uncited line has no grounds to use."""
    found: list[str] = []
    for clause in _CLAUSE_SPLIT_RE.split(text):
        words = _WORD_RE.findall(clause)
        for word in words[1:]:
            norm = _normalize_word(word)
            if word[0].isupper() and norm not in vocabulary and norm not in _COMMON_CAPITALIZED:
                found.append(word)
    return found


def validate_line(line: DraftLine, ctx: LineContext) -> list[str]:
    """Every reason `line` can't go in the script, as phrases. Empty means
    valid. Pure."""
    problems: list[str] = []
    if not line.text.strip():
        problems.append("the text is empty")
    if line.speaker not in ctx.speakers:
        problems.append(f"speaker must be one of {sorted(ctx.speakers)}")
    for cite in line.cites:
        if cite.episode_id not in ctx.citable_ids:
            problems.append(f"cites unknown source {cite.episode_id!r}")
        elif cite.timestamp is not None and (cite.episode_id, round(cite.timestamp)) not in ctx.highlights:
            problems.append(
                f"cites timestamp {cite.timestamp:g}, which is not a surfaced moment of "
                f"{cite.episode_id}; use one of its listed timestamps or null"
            )
    if not line.cites:
        if _DIGIT_RE.search(line.text):
            problems.append("states a number without citing the source it comes from")
        names = unknown_names(line.text, ctx.vocabulary)
        if names:
            problems.append(
                f"uses {', '.join(names)} without a citation; cite the source or drop the name"
            )
    return problems


def _to_turn(line: DraftLine, ctx: LineContext) -> Turn:
    citations = [
        Citation(
            episode_id=c.episode_id,
            segment_timestamp=(
                ctx.highlights[(c.episode_id, round(c.timestamp))] if c.timestamp is not None else None
            ),
        )
        for c in line.cites
    ]
    anchor = next((c for c in citations if c.segment_timestamp is not None), None)
    episode_id = anchor.episode_id if anchor else (citations[0].episode_id if citations else None)
    speaker: Literal["host", "cohost"] = "cohost" if line.speaker == "cohost" else "host"
    return Turn(
        speaker=speaker,
        text=line.text.strip(),
        episode_id=episode_id,
        segment_timestamp=anchor.segment_timestamp if anchor else None,
        citations=citations,
        move=line.move if line.move in SPEAKER_MOVES else None,
    )


def json_object(body: str) -> Any:
    """The JSON object in a model reply, tolerating code fences or a stray
    sentence around it. Raises ValueError when there is none."""
    start, end = body.find("{"), body.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object in the reply")
    return json.loads(body[start : end + 1])


def check_lines(body: str, ctx: LineContext) -> tuple[list[Turn], list[str]]:
    """(valid turns, problems) for one segment reply."""
    try:
        data = json_object(body)
    except ValueError as err:
        return [], [f"the reply was not a JSON object ({err})"]
    raw_lines = data.get("lines") if isinstance(data, dict) else None
    if not isinstance(raw_lines, list):
        return [], ['the reply has no "lines" array']
    turns: list[Turn] = []
    problems: list[str] = []
    for i, raw in enumerate(raw_lines, start=1):
        try:
            line = DraftLine.model_validate(raw)
        except ValidationError:
            problems.append(f"line {i} does not have the required fields")
            continue
        issues = validate_line(line, ctx)
        if issues:
            problems.append(f"line {i} ({line.text[:60]!r}): {'; '.join(issues)}")
        else:
            turns.append(_to_turn(line, ctx))
    return turns, problems


# --- Segment requests ---------------------------------------------------------------


@dataclass(frozen=True)
class SegmentRequest:
    index: int
    segment: OutlineSegment
    outline: EpisodeOutline
    transcript: list[Turn]
    is_final: bool
    target_lines: int
    briefs: dict[str, SourceBrief]
    profile: EpisodeProfile

    @property
    def max_lines(self) -> int:
        return math.ceil(self.target_lines * LINE_CAP_SLACK)


def _source_label(brief: SourceBrief) -> str:
    return " — ".join(x for x in (brief.show, brief.title) if x) or brief.episode_id


def segment_system(
    cast: EpisodeCast,
    briefs: list[SourceBrief],
    outline: EpisodeOutline,
    profile: EpisodeProfile,
    soul: str,
    context: str,
) -> str:
    """The part of every segment prompt that never changes within a script,
    so it can sit in one cached system block."""
    host = _speaker(profile, "host")
    if profile.format == "dialogue":
        cohost = _speaker(profile, "cohost")
        voices = (
            f'Two voices. "host" ({host.name}) and "cohost" ({cohost.name}) are both real '
            "people with views. Let them react to each other, press for specifics, disagree "
            "where the material supports it, and hand the thread back and forth naturally; "
            "no one speaks more than four sentences in a row.\n\n"
            f"HOST PERSONA:\n{_host_persona(host)}\n\n"
            f"COHOST PERSONA:\n{_speaker_persona(cohost, soul)}"
        )
    else:
        voices = (
            f'One voice: every line is spoken by "host" ({host.name}).\n\n'
            f"HOST PERSONA:\n{_host_persona(host)}"
        )
    briefs_json = json.dumps([b.model_dump(mode="json", exclude_none=True) for b in briefs], indent=2)
    material = "\n\n".join(source_material(ep) for ep in cast.featured)
    noted = "\n".join(
        f"- episode_id: {ep.episode_id} | {ep.show or 'unknown show'}: {ep.episode_title or ''}"
        for ep in cast.also_noted
    ) or "(none)"
    outline_json = json.dumps(outline.model_dump(mode="json"), indent=2)
    return f"""\
You write a podcast episode for an audience of one listener, one segment at a \
time. The outline below is your own plan. Within it you decide what to linger \
on, what to skip, where to start and how to get from one idea to the next.

LISTENER LENS (who this is for and how they think):
{soul}

LISTENER'S CURRENT CONTEXT:
{context or "(none given)"}

VOICES:
{voices}

STYLE:
{_style_block(profile.style, cast.target_minutes)}

SOURCE BRIEFS:
{briefs_json}

SOURCE MATERIAL (excerpts are cleaned transcript; speaker labels in brackets):
{material}

ALSO NOTED (worth a passing mention in the close at most, by show and title):
{noted}

EPISODE OUTLINE:
{outline_json}

WHAT MAKES IT WORTH HEARING
- Have a point of view. React, connect, disagree, notice what is surprising or \
funny. A tangent is fine if it earns its place.
- Not everything gets the same treatment. A deep dive can sit next to a quick \
aside; go where the material is richest for this listener.
- Sound like a person talking, not a summary being read.

WHAT KEEPS IT EASY TO FOLLOW BY EAR
1. The listener has heard none of these sources. The first time one comes up, \
give them enough to follow it: who is talking and what the piece is. A passing \
mention needs a clause; a deep dive earns more (why the speaker is worth \
hearing, when it aired, what prompted it). Never react to something the \
listener hasn't been given.
2. One thread at a time. Make each turn in the conversation audible, so a \
listener who drifted can rejoin.
3. Continue from where the transcript stops. Don't reintroduce a source or a \
speaker who has already been introduced, and don't repeat a point already made.
4. Stay inside the current segment; later segments are written separately.
5. Write for the ear: plain spoken sentences, no lists, no markdown, no stage \
directions, no sound effects.

GROUNDING (what the listener hears must be what the sources said)
6. Name things the way a listener would: by show, person and title. Never say \
an episode id, a timestamp, "segment" or "highlight".
7. Paraphrase. Quote only a short, clean sentence that appears in an excerpt, \
and say who said it.
8. Citations. A line that states anything about a source cites it: \
{{"episode_id": "...", "timestamp": <seconds>}} for a specific moment (use a \
listed timestamp exactly), or "timestamp": null for facts from its brief \
(who, what, when, context, thesis). A line with no citations is for framing, \
transitions and opinion only: it may not contain a number or a name."""


def _host_persona(host: SpeakerProfile) -> str:
    if host.persona == HOST_PERSONA_IS_SOUL:
        return "Speaks as the listener lens above: its interests, convictions and voice."
    return host.persona


def introduced_before(outline: EpisodeOutline, index: int) -> set[str]:
    """Source ids some body segment before `index` has discussed. A passing
    mention in the intro doesn't introduce a source."""
    return {
        sid
        for seg in outline.segments[:index]
        if seg.kind in BODY_KINDS
        for sid in seg.source_ids
    }


def segment_prompt(req: SegmentRequest) -> str:
    if req.transcript:
        so_far = "\n".join(f"{t.speaker.upper()}: {t.text}" for t in req.transcript)
    else:
        so_far = "(nothing yet: this segment opens the episode)"
    introduced = introduced_before(req.outline, req.index)
    notes: list[str] = []
    for sid in req.segment.source_ids:
        brief = req.briefs.get(sid)
        if brief is None:
            continue
        if sid in introduced:
            notes.append(f"{_source_label(brief)} has already been introduced; don't reintroduce it.")
        elif req.segment.kind in BODY_KINDS:
            notes.append(
                f"{_source_label(brief)} has not been introduced yet. The first time it comes up, "
                "make sure the listener knows who is talking and what the piece is before your "
                "take; how much setup it needs is your call."
            )
    if req.is_final:
        notes.append("This is the final segment: close the episode.")
    else:
        notes.append("Stop at the end of this segment; the next segment continues from here.")
    speakers = "host" if req.profile.format != "dialogue" else "host|cohost"
    segment_json = json.dumps(req.segment.model_dump(mode="json"), indent=2)
    return (
        f"TRANSCRIPT SO FAR:\n{so_far}\n\n"
        f"NOW WRITE SEGMENT {req.index + 1} OF {len(req.outline.segments)}:\n{segment_json}\n\n"
        f"Write about {req.target_lines} lines, at most {req.max_lines}. "
        + " ".join(notes)
        + "\n\nReply with ONLY a JSON object, no prose and no code fences:\n"
        f'{{"lines": [{{"speaker": "{speakers}", "text": "...", '
        f'"move": "{"|".join(SPEAKER_MOVES)}", '
        '"cites": [{"episode_id": "...", "timestamp": <seconds or null>}]}]}'
    )


def repair_prompt(problems: list[str]) -> str:
    listed = "\n".join(f"- {p}" for p in problems)
    return (
        f"Some lines can't be used:\n{listed}\n\n"
        "Rewrite the whole segment with these fixed, keeping everything else. Reply with ONLY "
        'the corrected JSON object ({"lines": [...]}).'
    )


def line_context(
    cast: EpisodeCast, briefs: list[SourceBrief], profile: EpisodeProfile, soul: str, context: str
) -> LineContext:
    sources = [*cast.featured, *cast.also_noted]
    highlights = {
        (h.episode_id, round(h.segment_timestamp)): h.segment_timestamp
        for ep in sources
        for h in ep.highlights
    }
    speakers = frozenset(s.role for s in profile.speakers)
    texts = [
        soul,
        context,
        *(s.name for s in profile.speakers),
        *(s.persona for s in profile.speakers),
        *(b.model_dump_json() for b in briefs),
        *(source_material(ep) for ep in sources),
    ]
    return LineContext(
        speakers=speakers,
        citable_ids=frozenset(ep.episode_id for ep in sources),
        highlights=highlights,
        vocabulary=vocabulary_of(*texts),
    )


def _takes_from(turns: list[Turn]) -> list[Take]:
    """`Script.takes` (kept for API and email callers): every line anchored to a highlight."""
    return [
        Take(
            text=t.text,
            take_type=t.move if t.move in TAKE_TYPES else "idea",
            episode_id=t.episode_id,
            segment_timestamp=t.segment_timestamp,
        )
        for t in turns
        if t.episode_id is not None and t.segment_timestamp is not None
    ]


def _briefs_on_air(briefs: list[SourceBrief], outline: EpisodeOutline) -> list[SourceBrief]:
    """The briefs of the sources the outline discusses, in the order they first come up."""
    by_id = {b.episode_id: b for b in briefs}
    return [by_id[sid] for sid in covered_ids(outline) if sid in by_id]


# --- Composers ----------------------------------------------------------------------


@runtime_checkable
class ScriptComposer(Protocol):
    def write_briefs(
        self, digest: Digest, soul: str, context: str, profile: EpisodeProfile | None = None
    ) -> list[SourceBrief]: ...

    def write_outline(
        self,
        digest: Digest,
        briefs: list[SourceBrief],
        soul: str,
        context: str,
        profile: EpisodeProfile | None = None,
    ) -> EpisodeOutline: ...

    def write_script(
        self,
        digest: Digest,
        soul: str,
        context: str,
        profile: EpisodeProfile | None = None,
        *,
        briefs: list[SourceBrief] | None = None,
        outline: EpisodeOutline | None = None,
    ) -> Script: ...


class _SegmentedComposer:
    """The brief -> outline -> segment loop. Subclasses supply how each brief,
    the outline and each segment's lines are produced; the orchestration,
    budgeting and assembly are shared so the mock and the real composer can't
    drift apart."""

    # -- hooks -------------------------------------------------------------------

    def _brief(self, episode: EpisodeDigest) -> SourceBrief:
        raise NotImplementedError

    def _outline(
        self,
        briefs: list[SourceBrief],
        plan: EpisodePlan,
        soul: str,
        context: str,
        profile: EpisodeProfile,
    ) -> EpisodeOutline:
        raise NotImplementedError

    def _segment(self, req: SegmentRequest, system: str, ctx: LineContext) -> list[Turn]:
        raise NotImplementedError

    # -- public API ----------------------------------------------------------------

    def write_briefs(
        self, digest: Digest, soul: str, context: str, profile: EpisodeProfile | None = None
    ) -> list[SourceBrief]:
        plan = plan_episode(digest, profile or MONOLOGUE_PROFILE)
        return [self._brief(ep) for ep in plan.candidates]

    def write_outline(
        self,
        digest: Digest,
        briefs: list[SourceBrief],
        soul: str,
        context: str,
        profile: EpisodeProfile | None = None,
    ) -> EpisodeOutline:
        profile = profile or MONOLOGUE_PROFILE
        plan = plan_episode(digest, profile)
        if not briefs:
            return EpisodeOutline(
                segments=[],
                also_noted=[ep.episode_id for ep in [*plan.candidates, *plan.overflow]],
            )
        return self._outline(briefs, plan, soul, context, profile)

    def write_script(
        self,
        digest: Digest,
        soul: str,
        context: str,
        profile: EpisodeProfile | None = None,
        *,
        briefs: list[SourceBrief] | None = None,
        outline: EpisodeOutline | None = None,
    ) -> Script:
        profile = profile or MONOLOGUE_PROFILE
        voices = _voices_for(profile)
        plan = plan_episode(digest, profile)
        if not plan.candidates:
            return Script(
                soul_version=digest.soul_version,
                takes=[],
                monologue=NOTHING_CLEARED,
                format=profile.format,
                voices=voices,
            )
        if briefs is None:
            briefs = self.write_briefs(digest, soul, context, profile)
        if outline is None:
            outline = self.write_outline(digest, briefs, soul, context, profile)
        cast = cast_episode(plan, outline)
        briefs = _briefs_on_air(briefs, outline)

        ctx = line_context(cast, briefs, profile, soul, context)
        system = segment_system(cast, briefs, outline, profile, soul, context)
        by_id = {b.episode_id: b for b in briefs}
        targets = segment_turn_targets(outline, cast.target_minutes)
        turns: list[Turn] = []
        for i, (segment, target) in enumerate(zip(outline.segments, targets, strict=True)):
            req = SegmentRequest(
                index=i,
                segment=segment,
                outline=outline,
                transcript=list(turns),
                is_final=i == len(outline.segments) - 1,
                target_lines=target,
                briefs=by_id,
                profile=profile,
            )
            turns.extend(
                t.model_copy(update={"segment_index": i})
                for t in self._segment(req, system, ctx)[: req.max_lines]
            )

        if not any(t.citations for t in turns):
            # R20: there was grounded material, but nothing grounded survived.
            raise ScriptError(
                f"script: {len(digest.highlights)} highlight(s) available but no grounded "
                "line survived validation"
            )
        if profile.format == "dialogue":
            text = _turns_transcript(turns)
        else:
            text = "\n\n".join(t.text for t in turns)
        return Script(
            soul_version=digest.soul_version,
            takes=_takes_from(turns),
            monologue=text,
            turns=turns,
            format=profile.format,
            voices=voices,
            briefs=briefs,
            outline=outline,
        )


class MockScriptComposer(_SegmentedComposer):
    """Deterministic, offline. Same loop as the real composer: an intro, a
    setup line the first time each source comes up citing its brief, one line
    per highlight citing it (a cohost follow-up on each in dialogue), a
    connection line where a segment spans sources, and a close."""

    def _brief(self, episode: EpisodeDigest) -> SourceBrief:
        return mock_brief(episode)

    def _outline(
        self,
        briefs: list[SourceBrief],
        plan: EpisodePlan,
        soul: str,
        context: str,
        profile: EpisodeProfile,
    ) -> EpisodeOutline:
        return mock_outline(briefs, [ep.episode_id for ep in plan.overflow], plan.fixed_minutes)

    def _segment(self, req: SegmentRequest, system: str, ctx: LineContext) -> list[Turn]:
        seg = req.segment
        briefs = [req.briefs[sid] for sid in seg.source_ids if sid in req.briefs]
        brief_cites = [Citation(episode_id=b.episode_id) for b in briefs]
        if seg.kind == "intro":
            labels = "; ".join(_source_label(b) for b in briefs)
            return [Turn(speaker="host", text=f"Today: {labels}.", citations=brief_cites, move="setup",
                         episode_id=briefs[0].episode_id if briefs else None)]
        if seg.kind == "close":
            return [Turn(speaker="host", text="That's the episode.")]
        introduced = introduced_before(req.outline, req.index)
        connection = [Turn(speaker="host", text="What connects these is worth a minute.",
                           citations=brief_cites, move="connection",
                           episode_id=briefs[0].episode_id if briefs else None)]
        if len(briefs) >= 2 and all(b.episode_id in introduced for b in briefs):
            return connection
        out: list[Turn] = []
        for brief in briefs:
            if brief.episode_id not in introduced:
                out.append(self._setup_line(brief))
            out += self._moments(brief, req.profile)
        return out + (connection if len(briefs) >= 2 else [])

    @staticmethod
    def _setup_line(brief: SourceBrief) -> Turn:
        published = brief.published_at.strftime("%d %b %Y") if brief.published_at else "recently"
        return Turn(
            speaker="host",
            text=f"From {_source_label(brief)}, published {published}. {brief.context} "
            f"The core point: {brief.thesis}",
            episode_id=brief.episode_id,
            citations=[Citation(episode_id=brief.episode_id)],
            move="setup",
        )

    @staticmethod
    def _moments(brief: SourceBrief, profile: EpisodeProfile) -> list[Turn]:
        out: list[Turn] = []
        for i, point in enumerate(brief.key_points):
            take_type = TAKE_TYPES[i % len(TAKE_TYPES)]
            cite = Citation(episode_id=brief.episode_id, segment_timestamp=point.segment_timestamp)
            out.append(
                Turn(
                    speaker="host",
                    text=f"[{take_type}] At {format_timestamp(point.segment_timestamp)}: {point.text}",
                    episode_id=brief.episode_id,
                    segment_timestamp=point.segment_timestamp,
                    citations=[cite],
                    move=take_type,
                )
            )
            if profile.format == "dialogue":
                out.append(
                    Turn(
                        speaker="cohost",
                        text="Where's the number on that?",
                        episode_id=brief.episode_id,
                        segment_timestamp=point.segment_timestamp,
                        citations=[cite],
                        move="pushback",
                    )
                )
        return out


class AnthropicScriptComposer(_SegmentedComposer):
    """Real script (Claude Sonnet). Activated when a key is present. `client`
    is injectable so every parsing and validation path is unit-testable
    without a key (same pattern as chorus.llm.AnthropicLLMClient)."""

    MODEL = "claude-sonnet-4-6"
    BRIEF_MAX_TOKENS = 1500
    OUTLINE_MAX_TOKENS = 2500
    SEGMENT_MAX_TOKENS = 4000

    def __init__(self, api_key: str | None = None, client: Any | None = None) -> None:
        if client is None:
            import anthropic  # type: ignore[import-not-found]  # optional dep; only when a key is used

            client = anthropic.Anthropic(api_key=api_key)
        self._client: Any = client

    def _create(self, system: str, messages: list[dict[str, str]], max_tokens: int) -> str:
        msg = self._client.messages.create(
            model=self.MODEL,
            max_tokens=max_tokens,
            # Cached: identical across every segment call of one script.
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=messages,
        )
        return "".join(b.text for b in msg.content if b.type == "text")

    def _ask_with_retry(self, system: str, user: str, max_tokens: int, parse: Any) -> Any:
        """One call, and one retry that shows the model why its reply failed.
        Raises the parse error if the retry fails too."""
        messages = [{"role": "user", "content": user}]
        body = self._create(system, messages, max_tokens)
        try:
            return parse(json_object(body))
        except ValueError as err:
            log.info("script: reply unusable, retrying once with feedback (%s)", err)
            messages += [
                {"role": "assistant", "content": body},
                {
                    "role": "user",
                    "content": f"That reply can't be used: {err}. Reply again with ONLY the "
                    "corrected JSON object.",
                },
            ]
            return parse(json_object(self._create(system, messages, max_tokens)))

    def _brief(self, episode: EpisodeDigest) -> SourceBrief:
        try:
            brief: SourceBrief = self._ask_with_retry(
                BRIEF_SYSTEM,
                brief_prompt(episode),
                self.BRIEF_MAX_TOKENS,
                lambda data: parse_brief(data, episode),
            )
            return brief
        except (ValueError, BriefError) as err:
            log.warning("script: brief for %s unusable twice (%s); using metadata", episode.episode_id, err)
            return mock_brief(episode)

    def _outline(
        self,
        briefs: list[SourceBrief],
        plan: EpisodePlan,
        soul: str,
        context: str,
        profile: EpisodeProfile,
    ) -> EpisodeOutline:
        candidate_ids = [b.episode_id for b in briefs]
        overflow_ids = [ep.episode_id for ep in plan.overflow]
        system = (
            "You are the editor of a podcast for an audience of one listener. From this week's "
            "sources you decide what is worth their time, how much of it, and in what order. "
            "Nothing has to make it in and nothing has to get equal time. The plan only has to "
            "make an episode that is easy to follow by ear and good to listen to."
            f"\n\nLISTENER LENS:\n{soul}\n\nLISTENER'S CURRENT CONTEXT:\n"
            f"{context or '(none given)'}"
        )
        try:
            outline: EpisodeOutline = self._ask_with_retry(
                system,
                outline_prompt(briefs, plan.overflow, plan.fixed_minutes),
                self.OUTLINE_MAX_TOKENS,
                lambda data: parse_outline(data, candidate_ids, overflow_ids, plan.fixed_minutes),
            )
            return outline
        except (ValueError, OutlineError) as err:
            log.warning("script: outline unusable twice (%s); using the default structure", err)
            return mock_outline(briefs, overflow_ids, plan.fixed_minutes)

    def _segment(self, req: SegmentRequest, system: str, ctx: LineContext) -> list[Turn]:
        messages = [{"role": "user", "content": segment_prompt(req)}]
        body = self._create(system, messages, self.SEGMENT_MAX_TOKENS)
        turns, problems = check_lines(body, ctx)
        if not problems:
            return turns
        log.info("script: segment %d had %d problem(s); asking once for a fix", req.index + 1, len(problems))
        messages += [
            {"role": "assistant", "content": body},
            {"role": "user", "content": repair_prompt(problems)},
        ]
        repaired, still = check_lines(self._create(system, messages, self.SEGMENT_MAX_TOKENS), ctx)
        for problem in still:
            log.info("script: segment %d dropped after repair: %s", req.index + 1, problem)
        # A repair that came back unparseable must not erase the valid first draft.
        return repaired if repaired else turns


def get_script_composer() -> ScriptComposer:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if key:
        return AnthropicScriptComposer(key)
    log.warning("script: ANTHROPIC_API_KEY absent — using MockScriptComposer")
    return MockScriptComposer()
