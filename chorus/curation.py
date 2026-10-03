"""Curation: soul-conditioned scoring -> cited digest, with honest refusal.

Windows the transcript into coarse spans, scores each against the soul + context
via the LLM client, and surfaces those clearing the relevance threshold as
Highlights whose timestamp + quote resolve against the source transcript. An
episode where nothing clears the bar is REFUSED ("nothing cleared the relevance
bar"), never given an invented reason.

A highlight's quote is the scorer's excerpt when that excerpt occurs verbatim
in the window (whitespace-normalised), timestamped at the segment where it
starts; otherwise it is the window's opening words. Either way the quote is
cut from transcript text, so it stays verifiable.
"""
from __future__ import annotations

import hashlib
import logging
import re
from typing import Any

from chorus.llm import LLMClient, ScoredWindow, TokenUsage
from chorus.models import (
    Digest,
    EpisodeDigest,
    Highlight,
    IngestResult,
    ResolvedEpisode,
    Segment,
    Transcript,
    WindowScore,
)

RELEVANCE_THRESHOLD = 0.35
WINDOW_SECONDS = 90
QUOTE_WORDS = 28
# citation_resolves matches a quote by its opening words, so an excerpt shorter
# than that is too weak a pointer to trust.
CITATION_HEAD_WORDS = 6
EXCERPT_MIN_WORDS = CITATION_HEAD_WORDS
REFUSAL = "nothing cleared the relevance bar"
ELLIPSIS = "..."

log = logging.getLogger("chorus.curation")

# Wrapping a model may add around an otherwise verbatim span: quote marks and
# ellipses. Stripped before matching; never stripped from inside the span.
_EXCERPT_EDGES = re.compile(r"^(?:\.\.\.|…|[\"'“”‘’\s])+|(?:\.\.\.|…|[\"'“”‘’\s])+$")


# The opening of an episode is where hosts name themselves and their guest;
# the script stage's source brief reads this much of it.
BRIEF_INTRO_SECONDS = 300
MAX_INTRO_EXCERPT_CHARS = 6_000

# Spoken filler that carries no meaning in a written excerpt. Deliberately
# narrow: "like" and "you know" are sometimes load-bearing.
_FILLER_RE = re.compile(r"\b(?:u+m+|u+h+|e+r+m+|h+m+|m+h*m+)\b[,.]?\s*", re.IGNORECASE)
_TURN_MARKER_RE = re.compile(r">>+")
_REPEATED_WORD_RE = re.compile(r"\b(\w+)(?:\s+\1\b)+", re.IGNORECASE)
_SPACE_BEFORE_PUNCT_RE = re.compile(r"\s+([,.;:!?])")


def clean_spoken_text(text: str) -> str:
    """Make transcript text readable without changing what was said: drop
    filler ("um", "uh"), caption turn markers (">>") and stuttered repeats
    ("the the"), and normalize spacing. Pure; used for excerpts the script
    reads, never for `quote` (which must match the transcript verbatim)."""
    text = _TURN_MARKER_RE.sub(" ", text)
    text = _FILLER_RE.sub("", text)
    text = _REPEATED_WORD_RE.sub(r"\1", text)
    text = " ".join(text.split())
    return _SPACE_BEFORE_PUNCT_RE.sub(r"\1", text)


def labeled_text(segments: list[Segment]) -> str:
    """Join segments, prefixing a `[speaker]` label whenever the speaker
    changes (when the source knows who is speaking), then clean the result."""
    parts: list[str] = []
    current: str | None = None
    for seg in segments:
        if seg.speaker and seg.speaker != current:
            parts.append(f"[{seg.speaker}]")
            current = seg.speaker
        parts.append(seg.text)
    return clean_spoken_text(" ".join(parts))


def intro_excerpt(segments: list[Segment], seconds: int = BRIEF_INTRO_SECONDS) -> str:
    text = labeled_text([s for s in segments if s.start < seconds])
    return text[:MAX_INTRO_EXCERPT_CHARS]


class _Window:
    __slots__ = ("start", "text", "segments")

    def __init__(self, start: float, segment: Segment) -> None:
        self.start = start
        self.text = segment.text
        self.segments = [segment]

    def add(self, segment: Segment) -> None:
        self.text += " " + segment.text
        self.segments.append(segment)


def window_segments(segments: list[Segment], window: int = WINDOW_SECONDS) -> list[_Window]:
    out: list[_Window] = []
    cur: _Window | None = None
    for s in segments:
        if cur is None or s.start - cur.start >= window:
            cur = _Window(s.start, s)
            out.append(cur)
        else:
            cur.add(s)
    return out


def soul_version(soul: str) -> str:
    return hashlib.sha1(soul.encode("utf-8")).hexdigest()[:8]


def _normalise(text: str) -> str:
    return " ".join(text.split())


def _quote(text: str) -> str:
    words = text.split()
    q = " ".join(words[:QUOTE_WORDS])
    return q + (ELLIPSIS if len(words) > QUOTE_WORDS else "")


def grounded_excerpt(window: _Window, excerpt: str) -> tuple[float, str] | None:
    """`(segment_timestamp, quote)` when `excerpt` occurs verbatim in the
    window, else None.

    Verbatim means a case-sensitive match after whitespace normalisation that
    starts on a word boundary and does not end mid-word. The timestamp is the
    start of the segment the excerpt begins in; the quote is the excerpt capped
    at QUOTE_WORDS like any other quote."""
    needle = _normalise(_EXCERPT_EDGES.sub("", excerpt))
    if len(needle.split()) < EXCERPT_MIN_WORDS:
        return None
    pieces = [(s.start, text) for s in window.segments if (text := _normalise(s.text))]
    match = re.search(r"(?<!\S)" + re.escape(needle) + r"(?!\w)", " ".join(t for _, t in pieces))
    if match is None:
        return None
    offset = 0
    for start, text in pieces:
        offset += len(text) + 1  # the joining space
        if match.start() < offset:
            return start, _quote(needle)
    raise AssertionError("match offset past the end of the window")  # unreachable


def _cite(window: _Window, excerpt: str | None) -> tuple[float, str]:
    """The highlight's timestamp and quote: the scorer's excerpt if it is
    grounded in this window, the window's opening words otherwise."""
    if excerpt is not None:
        grounded = grounded_excerpt(window, excerpt)
        if grounded is not None:
            return grounded
        log.info("curation: excerpt not verbatim in window at %.1fs; using its opening", window.start)
    return window.start, _quote(window.text)


def _unpack(result: ScoredWindow) -> tuple[float, str, str | None]:
    if len(result) == 3:
        return result[0], result[1], result[2]
    return result[0], result[1], None


def citation_resolves(transcript: Transcript, timestamp: float, quote: str, tol: float = 1.0) -> bool:
    """A highlight resolves if a segment at ~timestamp exists and the quote's
    opening words appear in the transcript text from there on.

    Only a trailing ellipsis is ours (the QUOTE_WORDS cap); one inside the quote
    is transcript text and must match as such."""
    head = " ".join(quote.removesuffix(ELLIPSIS).split()[:CITATION_HEAD_WORDS]).lower()
    for s in transcript.segments:
        if abs(s.start - timestamp) <= tol:
            window_text = _normalise(
                " ".join(
                    seg.text
                    for seg in transcript.segments
                    if timestamp <= seg.start < timestamp + WINDOW_SECONDS
                )
            ).lower()
            return head in window_text
    return False


def curate_episode(
    resolved: ResolvedEpisode,
    soul: str,
    context: str,
    client: LLMClient,
    threshold: float = RELEVANCE_THRESHOLD,
    max_highlights: int = 4,
    meter: TokenUsage | None = None,
) -> EpisodeDigest:
    if max_highlights < 1:
        # A negative slice would silently drop the TOP-scored highlights.
        raise ValueError(f"max_highlights must be >= 1, got {max_highlights}")
    transcript = resolved.transcript
    title = resolved.episode.title
    show = resolved.episode.show
    duration = transcript.segments[-1].start if transcript.segments else None
    scored: list[tuple[float, str, _Window, str | None]] = []
    windows: list[WindowScore] = []
    spans = window_segments(transcript.segments)
    # One model call per episode (batched), not one per window. `meter`
    # (R14), when given, isolates this episode's token spend instead of
    # reading a client-level counter shared across concurrent jobs.
    results = client.score_windows([w.text for w in spans], soul, context, meter=meter)
    if len(results) != len(spans):
        raise ValueError(f"scorer returned {len(results)} scores for {len(spans)} windows")
    for w, result in zip(spans, results, strict=True):
        score, reason, excerpt = _unpack(result)
        windows.append(WindowScore(start=w.start, score=round(score, 3)))
        if score >= threshold:
            scored.append((score, reason, w, excerpt))

    if not scored:
        return EpisodeDigest(
            episode_id=transcript.video_id,
            episode_title=title,
            highlights=[],
            refused=True,
            refusal_reason=REFUSAL,
            duration_seconds=duration,
            windows=windows,
            **_source_metadata(resolved),
        )

    scored.sort(key=lambda t: t[0], reverse=True)
    highlights: list[Highlight] = []
    for score, reason, w, excerpt in scored[:max_highlights]:
        timestamp, quote = _cite(w, excerpt)
        highlights.append(
            Highlight(
                episode_id=transcript.video_id,
                episode_title=title,
                segment_timestamp=timestamp,
                quote=quote,
                relevance_score=round(score, 3),
                why_surface=reason,
                show=show,
                excerpt=labeled_text(w.segments),
            )
        )
    return EpisodeDigest(
        episode_id=transcript.video_id,
        episode_title=title,
        highlights=highlights,
        duration_seconds=duration,
        windows=windows,
        **_source_metadata(resolved),
    )


def _source_metadata(resolved: ResolvedEpisode) -> dict[str, Any]:
    """What the script stage needs to introduce this source before commenting on it."""
    episode = resolved.episode
    return {
        "show": episode.show,
        "published_at": episode.published_at,
        "description": episode.description,
        "intro_excerpt": intro_excerpt(resolved.transcript.segments),
    }


def build_digest(
    ingest: IngestResult,
    soul: str,
    context: str,
    client: LLMClient,
    highlight_count: int = 4,
    soul_origin: str = "supplied",
) -> Digest:
    episodes = [
        curate_episode(r, soul, context, client, max_highlights=highlight_count)
        for r in ingest.resolved
    ]
    return Digest(soul_version=soul_version(soul), soul_origin=soul_origin, episodes=episodes)
