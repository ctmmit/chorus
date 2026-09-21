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

    start: float = Field(description="Start time of the transcript segment in seconds.")
    text: str = Field(description="Verbatim transcript text for this segment.")


class Transcript(BaseModel):
    video_id: str = Field(description="YouTube video identifier for the transcript.")
    segments: list[Segment] = Field(description="Timestamped transcript segments in source order.")

    @property
    def word_count(self) -> int:
        return sum(len(s.text.split()) for s in self.segments)


class EpisodeInput(BaseModel):
    """One episode as supplied by the calling agent (the §6 request shape)."""

    url: str | None = Field(
        default=None,
        max_length=MAX_URL_CHARS,
        description="YouTube episode URL; provide this or video_id.",
    )
    video_id: str | None = Field(
        default=None,
        max_length=MAX_VIDEO_ID_CHARS,
        description="YouTube video identifier; provide this or url.",
    )
    show: str | None = Field(
        default=None,
        max_length=MAX_SHOW_CHARS,
        description="Optional podcast or channel name used in the digest.",
    )
    title: str | None = Field(
        default=None,
        max_length=MAX_TITLE_CHARS,
        description="Optional episode title used in the digest.",
    )

    def resolved_id(self) -> str:
        """The YouTube id to fetch a transcript for. Caller may pass either."""
        if self.video_id:
            return self.video_id
        if self.url:
            from chorus.transcripts import extract_video_id

            return extract_video_id(self.url)
        raise ValueError("EpisodeInput requires either url or video_id")


class ResolvedEpisode(BaseModel):
    episode: EpisodeInput = Field(description="Caller-supplied episode metadata.")
    transcript: Transcript = Field(description="Resolved timestamped transcript.")


class SkippedEpisode(BaseModel):
    episode: EpisodeInput = Field(description="Episode that could not be ingested.")
    reason: str = Field(description="Explicit reason the episode was skipped.")


class IngestResult(BaseModel):
    resolved: list[ResolvedEpisode] = Field(description="Episodes with usable transcripts.")
    skipped: list[SkippedEpisode] = Field(description="Episodes skipped during transcript ingest.")


class DigestRequest(BaseModel):
    """The §6 request body for POST /digest."""

    # markdown persona / lens (any bootstrap tier produces this)
    soul: str = Field(
        min_length=1,
        max_length=MAX_SOUL_CHARS,
        description="Markdown persona and curation lens for the principal.",
    )
    # caller-assembled principal-context blob (may be empty)
    context: str = Field(
        max_length=MAX_CONTEXT_CHARS,
        description="Current projects, reading, and priorities that tune relevance this week.",
    )
    episodes: list[EpisodeInput] = Field(
        min_length=1,
        max_length=MAX_EPISODES,
        description="Episodes whose transcripts Chorus should curate.",
    )
    highlight_count: int = Field(
        default=4,
        ge=1,
        le=MAX_HIGHLIGHTS,
        description="Maximum highlights to surface per episode.",
    )
    soul_origin: str = Field(
        default="supplied",
        description="How the soul was created: supplied, derived:<adapter>, interview, or seed.",
    )


class SelectionRequest(BaseModel):
    """Layer-2 pick-and-choose: select by show name and/or explicit video ids."""

    soul: str = Field(
        min_length=1,
        max_length=MAX_SOUL_CHARS,
        description="Markdown persona and curation lens for the principal.",
    )
    context: str = Field(
        max_length=MAX_CONTEXT_CHARS,
        description="Current projects, reading, and priorities that tune relevance this week.",
    )
    shows: list[str] | None = Field(
        default=None,
        max_length=MAX_EPISODES,
        description="Catalog show names whose available episodes should be included.",
    )
    video_ids: list[str] | None = Field(
        default=None,
        max_length=MAX_EPISODES,
        description="Specific catalog video identifiers to include.",
    )
    highlight_count: int = Field(
        default=4,
        ge=1,
        le=MAX_HIGHLIGHTS,
        description="Maximum highlights to surface per episode.",
    )
    soul_origin: str = Field(
        default="supplied",
        description="How the soul was created: supplied, derived:<adapter>, interview, or seed.",
    )


class KeyRequest(BaseModel):
    """Unauthenticated request for a self-serve API key delivered by email."""

    email: str = Field(
        min_length=3,
        max_length=320,
        pattern=r"^[^\s@]+@[^\s@]+\.[^\s@]+$",
        description="Email address that will receive the new Chorus API key.",
    )


class Highlight(BaseModel):
    """One surfaced segment. `segment_timestamp` + `quote` must resolve against
    the source transcript (citation discipline); never fabricated."""

    episode_id: str = Field(description="Source episode's stable video identifier.")
    episode_title: str | None = Field(description="Source episode title when available.")
    segment_timestamp: float = Field(description="Start time in seconds for the cited segment.")
    quote: str = Field(description="Verbatim source quote at segment_timestamp.")
    relevance_score: float = Field(description="Lens-conditioned relevance score for the segment.")
    why_surface: str = Field(description="Reason the segment cleared the principal's relevance bar.")


class WindowScore(BaseModel):
    """Relevance of one scored transcript window. Every window is reported,
    surfaced or not, so a viewer can draw the whole episode and a caller can
    audit what the lens rejected."""

    start: float = Field(description="Start time in seconds for the scored transcript window.")
    score: float = Field(description="Lens-conditioned relevance score for this window.")


class EpisodeDigest(BaseModel):
    episode_id: str = Field(description="Source episode's stable video identifier.")
    episode_title: str | None = Field(description="Source episode title when available.")
    highlights: list[Highlight] = Field(description="Grounded highlights that cleared the lens.")
    refused: bool = Field(
        default=False,
        description="True when no transcript segment cleared the relevance bar.",
    )
    refusal_reason: str | None = Field(
        default=None,
        description="Reason no highlights were returned when refused is true.",
    )
    duration_seconds: float | None = Field(
        default=None,
        description="Approximate episode duration from the final transcript timestamp.",
    )
    windows: list[WindowScore] = Field(
        default_factory=list,
        description="Scores for every transcript window, including rejected windows.",
    )


class Digest(BaseModel):
    soul_version: str = Field(description="Content hash identifying the lens used for curation.")
    soul_origin: str = Field(
        default="supplied",
        description="How the soul was bootstrapped for provenance.",
    )
    episodes: list[EpisodeDigest] = Field(description="Per-episode curation results.")

    @property
    def highlights(self) -> list[Highlight]:
        return [h for ep in self.episodes for h in ep.highlights]


# Take types from the reader-recap annotation taxonomy (ENGINEERING_REVIEW Q6).
TAKE_TYPES = ("idea", "pushback", "connection", "question", "cross_reference")


class Take(BaseModel):
    """One opinionated beat in the script, traceable to a surfaced highlight."""

    text: str = Field(description="Opinionated script beat grounded in a highlight.")
    take_type: str = Field(description="Annotation category for this take.")
    episode_id: str = Field(description="Episode identifier supporting this take.")
    segment_timestamp: float = Field(description="Timestamp of the supporting highlight in seconds.")


class Script(BaseModel):
    soul_version: str = Field(description="Content hash identifying the lens used for this script.")
    takes: list[Take] = Field(description="Grounded opinionated beats composed into the episode.")
    monologue: str = Field(description="Complete single-voice audio script.")


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

    job_id: str = Field(description="Opaque identifier used to poll this asynchronous job.")
    status: JobStatus = Field(description="Current lifecycle state of the digest job.")
    digest: Digest | None = Field(
        default=None,
        description="Text digest, available from digest_ready onward.",
    )
    script: Script | None = Field(
        default=None,
        description="Audio script when synthesis succeeded.",
    )
    audio_url: str | None = Field(
        default=None,
        description="Authenticated relative URL for rendered audio when available.",
    )
    error: str | None = Field(
        default=None,
        description="Terminal failure reason when status is failed.",
    )
    warnings: list[str] = Field(
        default_factory=list,
        description="Non-fatal script or audio degradation messages.",
    )
