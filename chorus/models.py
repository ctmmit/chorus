"""Core data models. Pydantic v2; explicit, typed, no magic.

Data flow (Goal 1 portion):

    EpisodeInput[]  --ingest-->  ResolvedEpisode[] + SkippedEpisode[]
       (caller)                   (transcript loaded)   (graceful skip)
"""
from __future__ import annotations

from pydantic import BaseModel


class Segment(BaseModel):
    """One transcript line with its start time (seconds). Timestamps are what
    citations resolve against, so they are load-bearing, not decorative."""

    start: float
    text: str


class Transcript(BaseModel):
    video_id: str
    segments: list[Segment]

    @property
    def word_count(self) -> int:
        return sum(len(s.text.split()) for s in self.segments)


class EpisodeInput(BaseModel):
    """One episode as supplied by the calling agent (the §6 request shape)."""

    url: str | None = None
    video_id: str | None = None
    show: str | None = None
    title: str | None = None

    def resolved_id(self) -> str:
        """The YouTube id to fetch a transcript for. Caller may pass either."""
        if self.video_id:
            return self.video_id
        if self.url:
            from chorus.transcripts import extract_video_id

            return extract_video_id(self.url)
        raise ValueError("EpisodeInput requires either url or video_id")


class ResolvedEpisode(BaseModel):
    episode: EpisodeInput
    transcript: Transcript


class SkippedEpisode(BaseModel):
    episode: EpisodeInput
    reason: str


class IngestResult(BaseModel):
    resolved: list[ResolvedEpisode]
    skipped: list[SkippedEpisode]


class DigestRequest(BaseModel):
    """The §6 request body for POST /digest."""

    soul: str  # markdown persona / lens (any bootstrap tier produces this)
    context: str  # caller-assembled principal-context blob
    episodes: list[EpisodeInput]
    highlight_count: int = 4


class Highlight(BaseModel):
    """One surfaced segment. `segment_timestamp` + `quote` must resolve against
    the source transcript (citation discipline); never fabricated."""

    episode_id: str
    episode_title: str | None
    segment_timestamp: float
    quote: str
    relevance_score: float
    why_surface: str


class EpisodeDigest(BaseModel):
    episode_id: str
    episode_title: str | None
    highlights: list[Highlight]
    refused: bool = False
    refusal_reason: str | None = None


class Digest(BaseModel):
    soul_version: str  # provenance: which lens produced this (content hash)
    episodes: list[EpisodeDigest]

    @property
    def highlights(self) -> list[Highlight]:
        return [h for ep in self.episodes for h in ep.highlights]
