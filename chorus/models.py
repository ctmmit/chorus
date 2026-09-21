"""Core data models. Pydantic v2; explicit, typed, no magic.

Data flow (Goal 1 portion):

    EpisodeInput[]  --ingest-->  ResolvedEpisode[] + SkippedEpisode[]
       (caller)                   (transcript loaded)   (graceful skip)
"""
from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field

# Request bounds. Every scoring call re-sends soul + context, so these cap the
# per-job provider spend; the API has no caller auth in dev, so they also cap
# what an anonymous caller can make us do. Fixture souls are ~1.6k chars.
MAX_SOUL_CHARS = 40_000
MAX_CONTEXT_CHARS = 40_000
MAX_EPISODES = 25
MAX_HIGHLIGHTS = 20
MAX_URL_CHARS = 2_048
MAX_VIDEO_ID_CHARS = 64
MAX_SHOW_CHARS = 200
MAX_TITLE_CHARS = 500


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

    url: str | None = Field(default=None, max_length=MAX_URL_CHARS)
    video_id: str | None = Field(default=None, max_length=MAX_VIDEO_ID_CHARS)
    show: str | None = Field(default=None, max_length=MAX_SHOW_CHARS)
    title: str | None = Field(default=None, max_length=MAX_TITLE_CHARS)

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

    # markdown persona / lens (any bootstrap tier produces this)
    soul: str = Field(min_length=1, max_length=MAX_SOUL_CHARS)
    # caller-assembled principal-context blob (may be empty)
    context: str = Field(max_length=MAX_CONTEXT_CHARS)
    episodes: list[EpisodeInput] = Field(min_length=1, max_length=MAX_EPISODES)
    highlight_count: int = Field(default=4, ge=1, le=MAX_HIGHLIGHTS)
    soul_origin: str = "supplied"  # supplied | derived:<adapter> | interview | seed


class SelectionRequest(BaseModel):
    """Layer-2 pick-and-choose: select by show name and/or explicit video ids."""

    soul: str = Field(min_length=1, max_length=MAX_SOUL_CHARS)
    context: str = Field(max_length=MAX_CONTEXT_CHARS)
    shows: list[str] | None = Field(default=None, max_length=MAX_EPISODES)
    video_ids: list[str] | None = Field(default=None, max_length=MAX_EPISODES)
    highlight_count: int = Field(default=4, ge=1, le=MAX_HIGHLIGHTS)
    soul_origin: str = "supplied"


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
    soul_origin: str = "supplied"  # how the soul was bootstrapped (Q5)
    episodes: list[EpisodeDigest]

    @property
    def highlights(self) -> list[Highlight]:
        return [h for ep in self.episodes for h in ep.highlights]


# Take types from the reader-recap annotation taxonomy (ENGINEERING_REVIEW Q6).
TAKE_TYPES = ("idea", "pushback", "connection", "question", "cross_reference")


class Take(BaseModel):
    """One opinionated beat in the script, traceable to a surfaced highlight."""

    text: str
    take_type: str
    episode_id: str
    segment_timestamp: float


class Script(BaseModel):
    soul_version: str
    takes: list[Take]
    monologue: str  # the spine-floor single-voice script text


class JobStatus(str, Enum):
    queued = "queued"
    digest_ready = "digest_ready"
    done = "done"
    failed = "failed"


class Job(BaseModel):
    """The async job record (ENGINEERING_REVIEW Q3 lifecycle).

    `error` is set only on `failed`. `warnings` records non-fatal degradation
    on a `done` job (script or audio could not be produced) so a caller can
    tell "audio failed" from "audio not attempted".
    """

    job_id: str
    status: JobStatus
    digest: Digest | None = None
    script: Script | None = None
    audio_url: str | None = None
    error: str | None = None
    warnings: list[str] = Field(default_factory=list)
