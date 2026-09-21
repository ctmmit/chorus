"""Curation: soul-conditioned scoring -> cited digest, with honest refusal.

Windows the transcript into coarse spans, scores each against the soul + context
via the LLM client, and surfaces those clearing the relevance threshold as
Highlights whose timestamp + quote resolve against the source transcript. An
episode where nothing clears the bar is REFUSED ("nothing cleared the relevance
bar"), never given an invented reason.
"""
from __future__ import annotations

import hashlib

from chorus.llm import LLMClient
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
REFUSAL = "nothing cleared the relevance bar"


class _Window:
    __slots__ = ("start", "text")

    def __init__(self, start: float, text: str) -> None:
        self.start = start
        self.text = text


def window_segments(segments: list[Segment], window: int = WINDOW_SECONDS) -> list[_Window]:
    out: list[_Window] = []
    cur: _Window | None = None
    for s in segments:
        if cur is None or s.start - cur.start >= window:
            cur = _Window(s.start, s.text)
            out.append(cur)
        else:
            cur.text += " " + s.text
    return out


def soul_version(soul: str) -> str:
    return hashlib.sha1(soul.encode("utf-8")).hexdigest()[:8]


def _quote(text: str) -> str:
    words = text.split()
    q = " ".join(words[:QUOTE_WORDS])
    return q + ("..." if len(words) > QUOTE_WORDS else "")


def citation_resolves(transcript: Transcript, timestamp: float, quote: str, tol: float = 1.0) -> bool:
    """A highlight resolves if a segment at ~timestamp exists and the quote's
    opening words appear in the transcript text near it."""
    head = " ".join(quote.replace("...", "").split()[:6]).lower()
    for s in transcript.segments:
        if abs(s.start - timestamp) <= tol:
            window_text = " ".join(
                seg.text for seg in transcript.segments if timestamp <= seg.start < timestamp + WINDOW_SECONDS
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
) -> EpisodeDigest:
    if max_highlights < 1:
        # A negative slice would silently drop the TOP-scored highlights.
        raise ValueError(f"max_highlights must be >= 1, got {max_highlights}")
    transcript = resolved.transcript
    title = resolved.episode.title
    duration = transcript.segments[-1].start if transcript.segments else None
    scored: list[tuple[float, str, _Window]] = []
    windows: list[WindowScore] = []
    spans = window_segments(transcript.segments)
    # One model call per episode (batched), not one per window.
    results = client.score_windows([w.text for w in spans], soul, context)
    if len(results) != len(spans):
        raise ValueError(f"scorer returned {len(results)} scores for {len(spans)} windows")
    for w, (score, reason) in zip(spans, results, strict=True):
        windows.append(WindowScore(start=w.start, score=round(score, 3)))
        if score >= threshold:
            scored.append((score, reason, w))

    if not scored:
        return EpisodeDigest(
            episode_id=transcript.video_id,
            episode_title=title,
            highlights=[],
            refused=True,
            refusal_reason=REFUSAL,
            duration_seconds=duration,
            windows=windows,
        )

    scored.sort(key=lambda t: t[0], reverse=True)
    highlights = [
        Highlight(
            episode_id=transcript.video_id,
            episode_title=title,
            segment_timestamp=w.start,
            quote=_quote(w.text),
            relevance_score=round(score, 3),
            why_surface=reason,
        )
        for score, reason, w in scored[:max_highlights]
    ]
    return EpisodeDigest(
        episode_id=transcript.video_id,
        episode_title=title,
        highlights=highlights,
        duration_seconds=duration,
        windows=windows,
    )


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
