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

    start: float = Field(description="Start time of the transcript segment in seconds.")
    text: str = Field(description="Verbatim transcript text for this segment.")


class Transcript(BaseModel):
    video_id: str = Field(description="Stable episode identifier (YouTube id or rss-<hash>).")
    segments: list[Segment] = Field(description="Timestamped transcript segments in source order.")
    # None only for transcripts built before Phase C (old cached payloads still validate).
    source: str | None = Field(
        default=None,
        description='Provider that produced it: "fixture", "supadata", "rss:json", "deepgram"...',
    )

    @property
    def word_count(self) -> int:
        return sum(len(s.text.split()) for s in self.segments)


class EpisodeInput(BaseModel):
    """One episode as supplied by the calling agent (the §6 request shape).

    Either a YouTube identifier (`video_id`/`url`) or an RSS identifier
    (`feed_url` + `guid`, optionally `audio_url`) must be present."""

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
    # RSS / Podcasting 2.0 identification (Phase C): a podcast feed plus the
    # <guid> of one <item>. audio_url may be supplied directly (or resolved
    # from the feed's <enclosure>) so the Deepgram fallback has something to
    # transcribe.
    feed_url: str | None = Field(
        default=None,
        max_length=MAX_URL_CHARS,
        description="Podcast RSS feed URL; pair with guid for a non-YouTube episode.",
    )
    guid: str | None = Field(
        default=None,
        max_length=MAX_GUID_CHARS,
        description="The <guid> of the feed <item> to digest.",
    )
    audio_url: str | None = Field(
        default=None,
        max_length=MAX_URL_CHARS,
        description="Direct audio URL (enclosure); used to match the item or to transcribe.",
    )

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
    episode: EpisodeInput = Field(description="Caller-supplied episode metadata.")
    transcript: Transcript = Field(description="Resolved timestamped transcript.")


class SkippedEpisode(BaseModel):
    episode: EpisodeInput = Field(description="Episode that could not be ingested.")
    reason: str = Field(description="Explicit reason the episode was skipped.")


class IngestResult(BaseModel):
    resolved: list[ResolvedEpisode] = Field(description="Episodes with usable transcripts.")
    skipped: list[SkippedEpisode] = Field(description="Episodes skipped during transcript ingest.")


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

    role: Literal["host", "cohost"] = Field(description="Speaker role in the episode.")
    name: str = Field(
        min_length=1, max_length=MAX_SPEAKER_NAME_CHARS, description="Display name of the speaker."
    )
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

    tone: str = Field(default="", max_length=MAX_TONE_CHARS, description="Overall conversational tone.")
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

    name: str = Field(
        min_length=1, max_length=MAX_PROFILE_NAME_CHARS, description="Profile name, e.g. two-host."
    )
    format: Literal["monologue", "dialogue"] = Field(description="Single voice or two hosts.")
    speakers: list[SpeakerProfile] = Field(
        min_length=MIN_SPEAKERS, max_length=MAX_SPEAKERS, description="One host, optionally a cohost."
    )
    style: ConversationStyle = Field(description="Tone, engagement techniques, target length.")

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
    profile: EpisodeProfile | None = Field(
        default=None,
        description="Episode format and speakers; omit for the single-voice monologue default.",
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
    profile: EpisodeProfile | None = Field(
        default=None,
        description="Episode format and speakers; omit for the single-voice monologue default.",
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


class Turn(BaseModel):
    """One line of dialogue in a two-host episode, traceable to a surfaced
    highlight exactly like `Take` — grounding is per-turn, not per-episode."""

    speaker: Literal["host", "cohost"] = Field(description="Which speaker says this line.")
    text: str = Field(description="The spoken line, grounded in one highlight.")
    episode_id: str = Field(description="Episode identifier of the supporting highlight.")
    segment_timestamp: float = Field(description="Timestamp of the supporting highlight in seconds.")


class Script(BaseModel):
    soul_version: str = Field(description="Content hash identifying the lens used for this script.")
    takes: list[Take] = Field(description="Grounded opinionated beats composed into the episode.")
    monologue: str = Field(
        description="Readable script text: the single voice, or the HOST/COHOST transcript.",
    )
    turns: list[Turn] = Field(
        default_factory=list,
        description="Dialogue turns (pass 2, dialogue format only). Empty for monologue.",
    )
    format: Literal["monologue", "dialogue"] = Field(
        default="monologue", description="Episode format the audio renderer dispatches on."
    )


class JobStatus(str, Enum):
    queued = "queued"
    digest_ready = "digest_ready"
    done = "done"
    failed = "failed"


class JobUsage(BaseModel):
    """Run telemetry (§8 row C). Nothing here is load-bearing for the
    lifecycle; it is the cost/observability meter a caller (or a human) can
    read to see what a job actually did — previously thrown away."""

    stage_seconds: dict[str, float] = Field(
        default_factory=dict,
        description='Wall-clock seconds per stage: "ingest", "curate", "script", "audio".',
    )
    transcript_sources: dict[str, str] = Field(
        default_factory=dict,
        description="Resolved episode id -> provider that produced its transcript.",
    )
    skipped: list[SkippedEpisode] = Field(
        default_factory=list,
        description="Episodes ingest could not resolve a transcript for, with the reason.",
    )
    llm_tokens: LLMTokens | None = Field(
        default=None,
        description="Model tokens spent by curation, when the client meters them.",
    )


class LLMTokens(BaseModel):
    """Token counts for one job, split so the prompt-cache hit rate is visible:
    a healthy batched run has cache_read >> input after the first call."""

    calls: int = Field(default=0, description="Model calls made during curation.")
    input_tokens: int = Field(default=0, description="Uncached input tokens billed at full price.")
    output_tokens: int = Field(default=0, description="Output tokens generated.")
    cache_read_tokens: int = Field(default=0, description="Input tokens served from prompt cache.")
    cache_write_tokens: int = Field(default=0, description="Input tokens written to prompt cache.")


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
    usage: JobUsage | None = Field(
        default=None,
        description="Run telemetry: stage seconds, transcript sources, skipped episodes, tokens.",
    )
