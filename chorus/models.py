"""Core data models. Pydantic v2; explicit, typed, no magic.

Data flow (Goal 1 portion):

    EpisodeInput[]  --ingest-->  ResolvedEpisode[] + SkippedEpisode[]
       (caller)                   (transcript loaded)   (graceful skip)
"""
from __future__ import annotations

import hashlib
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field, model_validator

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
MAX_GUID_CHARS = 500
# Length of the sha1 prefix used to build a stable id for RSS episodes
# (video_id-shaped, short enough to stay under MAX_VIDEO_ID_CHARS everywhere
# a resolved id is stored/logged).
RSS_ID_HASH_CHARS = 16


class Segment(BaseModel):
    """One transcript line with its start time (seconds). Timestamps are what
    citations resolve against, so they are load-bearing, not decorative."""

    start: float
    text: str


class Transcript(BaseModel):
    video_id: str
    segments: list[Segment]
    # Which provider produced this transcript (e.g. "fixture", "supadata",
    # "rss:json", "deepgram"). None only for transcripts built before Phase C
    # (kept optional so old cached payloads still validate).
    source: str | None = None

    @property
    def word_count(self) -> int:
        return sum(len(s.text.split()) for s in self.segments)


class EpisodeInput(BaseModel):
    """One episode as supplied by the calling agent (the §6 request shape).

    Either a YouTube identifier (`video_id`/`url`) or an RSS identifier
    (`feed_url` + `guid`, optionally `audio_url`) must be present."""

    url: str | None = Field(default=None, max_length=MAX_URL_CHARS)
    video_id: str | None = Field(default=None, max_length=MAX_VIDEO_ID_CHARS)
    show: str | None = Field(default=None, max_length=MAX_SHOW_CHARS)
    title: str | None = Field(default=None, max_length=MAX_TITLE_CHARS)
    # RSS / Podcasting 2.0 identification (Phase C): a podcast feed plus the
    # <guid> of one <item>. audio_url may be supplied directly (or resolved
    # from the feed's <enclosure>) so the Deepgram fallback has something to
    # transcribe.
    feed_url: str | None = Field(default=None, max_length=MAX_URL_CHARS)
    guid: str | None = Field(default=None, max_length=MAX_GUID_CHARS)
    audio_url: str | None = Field(default=None, max_length=MAX_URL_CHARS)

    def resolved_id(self) -> str:
        """A stable id to fetch/cache a transcript for. YouTube episodes use
        the video id; RSS episodes (no YouTube id) get a deterministic id
        derived from guid (falling back to audio_url) so the same episode
        always resolves to the same cache key."""
        if self.video_id:
            return self.video_id
        if self.url:
            from chorus.transcripts import extract_video_id

            return extract_video_id(self.url)
        if self.guid or self.audio_url:
            key = self.guid or self.audio_url
            assert key is not None  # narrows for mypy; guarded by the `or` above
            digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:RSS_ID_HASH_CHARS]
            return f"rss-{digest}"
        raise ValueError(
            "EpisodeInput requires one of: video_id, url, or (guid/audio_url)"
        )


class ResolvedEpisode(BaseModel):
    episode: EpisodeInput
    transcript: Transcript


class SkippedEpisode(BaseModel):
    episode: EpisodeInput
    reason: str


class IngestResult(BaseModel):
    resolved: list[ResolvedEpisode]
    skipped: list[SkippedEpisode]


# --- Episode profiles (Phase E, docs/DEVELOPMENT_PLAN.md §4): "like
# NotebookLM" means two hosts in conversation with an arc, reacting to each
# other — but the AGENT still writes every line. These models borrow the
# *structure* Podcastfy / Open Notebook use (episode profile, speaker
# profiles, engagement techniques as config), never their auto-writer.
# Single-voice (MONOLOGUE_PROFILE) stays the default everywhere a caller
# omits `profile`, so today's behavior is unchanged unless it is opted into.

MAX_PERSONA_CHARS = 4_000
MAX_SPEAKER_NAME_CHARS = 100
MAX_TONE_CHARS = 200
MAX_ENGAGEMENT_TECHNIQUES = 10
MAX_ENGAGEMENT_TECHNIQUE_CHARS = 60
MAX_PROFILE_NAME_CHARS = 200
MIN_SPEAKERS = 1
MAX_SPEAKERS = 2
MIN_TARGET_MINUTES = 1
MAX_TARGET_MINUTES = 20
DEFAULT_TARGET_MINUTES = 5
# Sentinel persona value: "use the soul itself as this speaker's voice"
# (chorus/script.py resolves it against the request's `soul` text).
HOST_PERSONA_IS_SOUL = "the soul"


class SpeakerProfile(BaseModel):
    """One speaker in an episode. `persona` is markdown describing how this
    speaker talks (tone, stance), distinct from `soul` (what gets curated)."""

    role: Literal["host", "cohost"]
    name: str = Field(min_length=1, max_length=MAX_SPEAKER_NAME_CHARS)
    persona: str = Field(
        default=HOST_PERSONA_IS_SOUL,
        max_length=MAX_PERSONA_CHARS,
        description=(
            'Markdown persona for this speaker\'s voice. The literal value '
            '"the soul" (the host default) means: use the request\'s `soul` '
            "text itself as this speaker's persona."
        ),
    )
    voice_id: str | None = Field(
        default=None,
        description="ElevenLabs voice id override. Unset falls back to the env defaults.",
    )


class ConversationStyle(BaseModel):
    """Engagement techniques as explicit config (borrowed from Podcastfy /
    Open Notebook), honored by the composer's dialogue prompt — never left to
    an auto-writer to invent."""

    tone: str = Field(default="", max_length=MAX_TONE_CHARS)
    engagement: list[str] = Field(
        default_factory=list,
        max_length=MAX_ENGAGEMENT_TECHNIQUES,
        description='e.g. "interruptions", "callbacks", "disagreement", "concrete numbers".',
    )
    target_minutes: int = Field(
        default=DEFAULT_TARGET_MINUTES,
        ge=MIN_TARGET_MINUTES,
        le=MAX_TARGET_MINUTES,
        description="Target spoken length; caps how many turns the composer may write.",
    )


class EpisodeProfile(BaseModel):
    """What kind of episode to produce: who's speaking and how. `format`
    drives both the composer's second pass (chorus/script.py) and the audio
    renderer selection (chorus/audio.py)."""

    name: str = Field(min_length=1, max_length=MAX_PROFILE_NAME_CHARS)
    format: Literal["monologue", "dialogue"]
    speakers: list[SpeakerProfile] = Field(min_length=MIN_SPEAKERS, max_length=MAX_SPEAKERS)
    style: ConversationStyle

    @model_validator(mode="after")
    def _dialogue_requires_host_and_cohost(self) -> EpisodeProfile:
        if self.format == "dialogue":
            roles = {s.role for s in self.speakers}
            if roles != {"host", "cohost"}:
                raise ValueError(
                    'format="dialogue" requires exactly one "host" and one "cohost" speaker'
                )
        return self


# Today's behavior, made explicit as a profile: one host, voiced by the soul.
MONOLOGUE_PROFILE = EpisodeProfile(
    name="monologue",
    format="monologue",
    speakers=[SpeakerProfile(role="host", name="Host", persona=HOST_PERSONA_IS_SOUL)],
    style=ConversationStyle(tone="opinionated, direct", engagement=[]),
)

# Host = the soul; cohost = a configurable skeptical foil (docs/DEVELOPMENT_PLAN.md §4).
TWO_HOST_PROFILE = EpisodeProfile(
    name="two-host",
    format="dialogue",
    speakers=[
        SpeakerProfile(role="host", name="Host", persona=HOST_PERSONA_IS_SOUL),
        SpeakerProfile(
            role="cohost",
            name="Cohost",
            persona=(
                "A sharp, skeptical foil. You defend the guest's position against the "
                "host's takes and press for specifics: whenever the host makes a claim, "
                "ask for the number, the counterexample, or the mechanism. You are not "
                "hostile — you are the discipline the host's opinions need."
            ),
        ),
    ],
    style=ConversationStyle(
        tone="sharp, argumentative, fast-paced",
        engagement=["interruptions", "callbacks", "disagreement", "concrete numbers"],
    ),
)


class DigestRequest(BaseModel):
    """The §6 request body for POST /digest."""

    # markdown persona / lens (any bootstrap tier produces this)
    soul: str = Field(min_length=1, max_length=MAX_SOUL_CHARS)
    # caller-assembled principal-context blob (may be empty)
    context: str = Field(max_length=MAX_CONTEXT_CHARS)
    episodes: list[EpisodeInput] = Field(min_length=1, max_length=MAX_EPISODES)
    highlight_count: int = Field(default=4, ge=1, le=MAX_HIGHLIGHTS)
    soul_origin: str = "supplied"  # supplied | derived:<adapter> | interview | seed
    # None -> MONOLOGUE_PROFILE (today's single-voice behavior, unchanged).
    profile: EpisodeProfile | None = None


class SelectionRequest(BaseModel):
    """Layer-2 pick-and-choose: select by show name and/or explicit video ids."""

    soul: str = Field(min_length=1, max_length=MAX_SOUL_CHARS)
    context: str = Field(max_length=MAX_CONTEXT_CHARS)
    shows: list[str] | None = Field(default=None, max_length=MAX_EPISODES)
    video_ids: list[str] | None = Field(default=None, max_length=MAX_EPISODES)
    highlight_count: int = Field(default=4, ge=1, le=MAX_HIGHLIGHTS)
    soul_origin: str = "supplied"
    profile: EpisodeProfile | None = None


class Highlight(BaseModel):
    """One surfaced segment. `segment_timestamp` + `quote` must resolve against
    the source transcript (citation discipline); never fabricated."""

    episode_id: str
    episode_title: str | None
    segment_timestamp: float
    quote: str
    relevance_score: float
    why_surface: str


class WindowScore(BaseModel):
    """Relevance of one scored transcript window. Every window is reported,
    surfaced or not, so a viewer can draw the whole episode and a caller can
    audit what the lens rejected."""

    start: float
    score: float


class EpisodeDigest(BaseModel):
    episode_id: str
    episode_title: str | None
    highlights: list[Highlight]
    refused: bool = False
    refusal_reason: str | None = None
    duration_seconds: float | None = None  # start of the last transcript segment
    windows: list[WindowScore] = Field(default_factory=list)


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


class Turn(BaseModel):
    """One line of dialogue in a two-host episode, traceable to a surfaced
    highlight exactly like `Take` — grounding is per-turn, not per-episode."""

    speaker: Literal["host", "cohost"]
    text: str
    episode_id: str
    segment_timestamp: float


class Script(BaseModel):
    soul_version: str
    takes: list[Take]
    monologue: str  # the spine-floor single-voice script text
    # Dialogue turns (pass 2, dialogue format only). Empty for monologue.
    turns: list[Turn] = Field(default_factory=list)
    format: Literal["monologue", "dialogue"] = "monologue"


class JobStatus(str, Enum):
    queued = "queued"
    digest_ready = "digest_ready"
    done = "done"
    failed = "failed"


class JobUsage(BaseModel):
    """Run telemetry (§8 row C). Nothing here is load-bearing for the
    lifecycle; it is the cost/observability meter a caller (or a human) can
    read to see what a job actually did — previously thrown away."""

    # Wall-clock seconds per pipeline stage: "ingest" | "curate" | "script" | "audio".
    stage_seconds: dict[str, float] = Field(default_factory=dict)
    # resolved episode id -> which TranscriptProvider produced it (Transcript.source).
    transcript_sources: dict[str, str] = Field(default_factory=dict)
    # Episodes ingest() could not resolve a transcript for, and why.
    skipped: list[SkippedEpisode] = Field(default_factory=list)
    # Model tokens spent by this job (curation scorer), when the client reports them.
    llm_tokens: LLMTokens | None = None


class LLMTokens(BaseModel):
    """Token counts for one job, split so the prompt-cache hit rate is visible:
    a healthy batched run has cache_read >> input after the first call."""

    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


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
    usage: JobUsage | None = None
