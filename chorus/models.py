"""Core data models. Pydantic v2; explicit, typed, no magic.

Data flow (Goal 1 portion):

    EpisodeInput[]  --ingest-->  ResolvedEpisode[] + SkippedEpisode[]
       (caller)                   (transcript loaded)   (graceful skip)
"""
from __future__ import annotations

import hashlib
from datetime import datetime
from enum import StrEnum
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, model_validator

# Request bounds. Every scoring call re-sends soul + context, so these cap the
# per-job provider spend; the API has no caller auth in dev, so they also cap
# what an anonymous caller can make us do. Fixture souls are ~1.6k chars.
MAX_SOUL_CHARS = 40_000
MAX_CONTEXT_CHARS = 40_000
MAX_CONTEXT_BLOCKS = 10
MAX_CONTEXT_ITEMS = 100
MAX_CONTEXT_ITEM_CHARS = 2_000
MAX_CONTEXT_SOURCE_CHARS = 80
MAX_EPISODES = 25
MAX_HIGHLIGHTS = 20
MAX_URL_CHARS = 2_048
MAX_VIDEO_ID_CHARS = 64
MAX_SHOW_CHARS = 200
MAX_TITLE_CHARS = 500
MAX_GUID_CHARS = 500
# Episode show notes are prompt input for the source brief; capped so one
# verbose feed can't dominate the script stage's context.
MAX_DESCRIPTION_CHARS = 4_000
# Length of the sha1 prefix used to build a stable id for RSS episodes
# (video_id-shaped, short enough to stay under MAX_VIDEO_ID_CHARS everywhere
# a resolved id is stored/logged).
RSS_ID_HASH_CHARS = 16

# Owner recorded on a Job/Subscription created before ownership existed, and
# on every job/subscription created by the master CHORUS_API_TOKEN (never an
# issued key's email — chorus.jobs.MASTER_OWNER is the same literal value;
# duplicated here rather than imported to avoid a models->jobs->models cycle).
DEFAULT_OWNER = "master"


def _canonical_feed_url(feed_url: str) -> str:
    """Canonical form used as the RSS identity's cache-key namespace (R11):
    scheme + host lowercased, path kept as-is, query kept as-is, fragment
    dropped. Two feed URLs that only differ in fragment or host casing must
    hash to the same identity; two different hosts must not collide."""
    parts = urlsplit(feed_url)
    host = (parts.hostname or "").lower()
    port = f":{parts.port}" if parts.port is not None else ""
    query = f"?{parts.query}" if parts.query else ""
    return f"{parts.scheme.lower()}://{host}{port}{parts.path}{query}"


class Segment(BaseModel):
    """One transcript line with its start time (seconds). Timestamps are what
    citations resolve against, so they are load-bearing, not decorative."""

    start: float = Field(description="Start time of the transcript segment in seconds.")
    text: str = Field(description="Verbatim transcript text for this segment.")
    speaker: str | None = Field(
        default=None,
        description=(
            "Who is speaking in this segment, when the source knows: a real name from a "
            'publisher transcript (VTT voice span, JSON speaker), or a diarization label such as '
            '"Speaker A" (AssemblyAI) or "Speaker 0" (Deepgram). None when the source carries no '
            "speaker information (YouTube captions, plain cues). Never part of `text`."
        ),
    )


class Transcript(BaseModel):
    video_id: str = Field(description="Stable episode identifier (YouTube id or rss-<hash>).")
    segments: list[Segment] = Field(description="Timestamped transcript segments in source order.")
    # None only for transcripts built before Phase C (old cached payloads still validate).
    source: str | None = Field(
        default=None,
        description=(
            'Provider that produced it: "fixture", "supadata", "rss:json", "rss:vtt", '
            '"rss:srt", "assemblyai", "deepgram"...'
        ),
    )
    # Dynamic ad insertion means two downloads of one enclosure can differ, which
    # shifts every timestamp after the first inserted ad; recording the exact URL
    # a speech-to-text provider transcribed lets a citation be traced back to it.
    source_audio_url: str | None = Field(
        default=None,
        description=(
            "The exact audio URL a speech-to-text provider transcribed (AssemblyAI, Deepgram). "
            "Timestamps index that file, which can differ from the audio a listener streams when "
            "the publisher inserts ads dynamically. None for publisher transcripts and captions."
        ),
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
    published_at: datetime | None = Field(
        default=None,
        description="Publication time when known (feed pubDate); lets the script say when it aired.",
    )
    description: str | None = Field(
        default=None,
        max_length=MAX_DESCRIPTION_CHARS,
        description="Plain-text show notes when known; the script uses them to introduce the source.",
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

    @model_validator(mode="after")
    def _one_identity_family(self) -> EpisodeInput:
        """R11 (docs/REVIEW_WAVE1.md #11): exactly one source identity family,
        never mixed. Mixing a YouTube id with attacker-controlled RSS fields
        is exactly the cross-feed poisoning shape the review describes (a
        victim video_id paired with an attacker feed_url/guid) — refusing the
        combination at construction closes it before any provider ever runs."""
        has_youtube = bool(self.video_id or self.url)
        has_rss = bool(self.feed_url or self.guid or self.audio_url)
        if has_youtube and has_rss:
            raise ValueError(
                "EpisodeInput must not mix YouTube fields (video_id/url) with "
                "RSS fields (feed_url/guid/audio_url)"
            )
        if not has_youtube and not has_rss:
            raise ValueError(
                "EpisodeInput requires one of: video_id, url, or "
                "(feed_url+guid | feed_url+audio_url | audio_url)"
            )
        if has_rss and not (self.audio_url or (self.feed_url and self.guid)):
            # feed_url alone (no guid, no audio_url) cannot identify an item.
            raise ValueError(
                "EpisodeInput feed_url requires guid or audio_url to identify an episode"
            )
        return self

    def resolved_id(self) -> str:
        """A stable id to fetch/cache a transcript for. YouTube episodes use
        the video id. RSS episodes get a deterministic id derived from the
        *canonical feed URL* plus guid (falling back to audio_url) so the
        same episode always resolves to the same cache key and, critically,
        so an identical guid in two different feeds never collides (R11):
        without the feed URL in the hash, `guid="ep-1"` in feed A and feed B
        would hash to the same id and share a cache entry."""
        if self.video_id:
            return self.video_id
        if self.url:
            from chorus.transcripts import extract_video_id

            return extract_video_id(self.url)
        if self.feed_url and (self.guid or self.audio_url):
            key = self.guid or self.audio_url
            assert key is not None  # narrows for mypy; guarded by the `or` above
            canonical = _canonical_feed_url(self.feed_url)
            digest = hashlib.sha1(f"{canonical}\n{key}".encode()).hexdigest()
            return f"rss-{digest[:RSS_ID_HASH_CHARS]}"
        if self.audio_url:
            digest = hashlib.sha1(self.audio_url.encode("utf-8")).hexdigest()
            return f"rss-{digest[:RSS_ID_HASH_CHARS]}"
        raise ValueError(
            "EpisodeInput requires one of: video_id, url, or "
            "(feed_url+guid | feed_url+audio_url | audio_url)"
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
# Explicit default for callers that want a number; a profile whose style
# leaves target_minutes unset lets the writer choose the length in its outline
# (chorus/outline.py `WRITER_MIN_MINUTES`..`WRITER_MAX_MINUTES`).
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
    target_minutes: int | None = Field(
        default=None,
        ge=MIN_TARGET_MINUTES,
        le=MAX_TARGET_MINUTES,
        description=(
            "Target spoken length; caps how many turns the composer may write. Unset budgets "
            "the length from the number of featured sources (about 3-4 minutes each)."
        ),
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


class ContextItem(BaseModel):
    """One thing the principal is reading, working on or meeting about."""

    text: str = Field(
        min_length=1,
        max_length=MAX_CONTEXT_ITEM_CHARS,
        description="The item itself: a highlight, a project's one-line goal, a meeting title.",
    )
    label: str | None = Field(
        default=None,
        max_length=MAX_TITLE_CHARS,
        description="Where it sits, such as the book or note it came from.",
    )


class ContextBlock(BaseModel):
    """One source's contribution to the principal's context (chorus/context.py)."""

    source: str = Field(
        min_length=1,
        max_length=MAX_CONTEXT_SOURCE_CHARS,
        description='The source, as the principal would name it ("Readwise highlights").',
    )
    items: list[ContextItem] = Field(
        max_length=MAX_CONTEXT_ITEMS, description="Its items, most relevant or newest first."
    )
    as_of: datetime | None = Field(default=None, description="When the items were gathered.")


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
    remember: bool = Field(
        default=True,
        description=(
            "Use and update the owner's memory of earlier digests (repeat penalty, threads "
            "that refer back). False keeps this run out of memory entirely."
        ),
    )
    context_blocks: list[ContextBlock] = Field(
        default_factory=list,
        max_length=MAX_CONTEXT_BLOCKS,
        description=(
            "Structured context by source (the chorus-context skill's recipes). Rendered into "
            "`context` on arrival, within the same size budget, and then cleared."
        ),
    )
    context_sources: list[str] = Field(
        default_factory=list,
        description="Sources whose blocks made it into `context` (set by Chorus).",
    )

    @model_validator(mode="after")
    def _render_context_blocks(self) -> DigestRequest:
        if self.context_blocks:
            from chorus.context import render_context

            self.context, sources = render_context(self.context, self.context_blocks)
            self.context_sources = [*self.context_sources, *sources]
            self.context_blocks = []
        return self


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
    show: str | None = Field(default=None, description="Podcast or channel name when known.")
    excerpt: str = Field(
        default="",
        description=(
            "The whole surfaced window with filler removed and speaker labels kept: what the "
            "script reads to understand the moment. `quote` stays the verbatim citation."
        ),
    )
    highlight_id: str = Field(
        default="",
        description=(
            "Stable short id from episode and timestamp, so a rating survives reruns of the "
            "same episode. Filled in automatically."
        ),
    )

    @model_validator(mode="after")
    def _fill_highlight_id(self) -> Highlight:
        if not self.highlight_id:
            self.highlight_id = highlight_id(self.episode_id, self.segment_timestamp)
        return self


HIGHLIGHT_ID_CHARS = 12


def highlight_id(episode_id: str, segment_timestamp: float) -> str:
    """Hash of the episode and the timestamp to a tenth of a second."""
    key = f"{episode_id}@{segment_timestamp:.1f}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:HIGHLIGHT_ID_CHARS]


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
    show: str | None = Field(default=None, description="Podcast or channel name when known.")
    published_at: datetime | None = Field(default=None, description="Publication time when known.")
    description: str | None = Field(default=None, description="Plain-text show notes when known.")
    url: str | None = Field(
        default=None,
        description="Where a listener can open the source (YouTube watch page or audio file).",
    )
    intro_excerpt: str = Field(
        default="",
        description=(
            "The cleaned, speaker-labeled opening of the transcript, where hosts usually name "
            "themselves and their guest. Input to the script stage's source brief."
        ),
    )


class ThreadMember(BaseModel):
    """One highlight's place in a cross-source thread."""

    highlight_id: str = Field(description="The highlight this member is (Highlight.highlight_id).")
    episode_id: str = Field(description="The highlight's source episode.")
    stance: Literal["agrees", "disagrees", "adds"] = Field(
        description="How this highlight answers the thread's question relative to the others."
    )
    remembered_at: datetime | None = Field(
        default=None,
        description=(
            "Set when the member is a claim surfaced in an earlier digest (chorus/memory.py) "
            "rather than this week's: when it was surfaced."
        ),
    )
    quote: str | None = Field(
        default=None, description="A remembered member's quote (this week's are on the digest)."
    )
    source: str | None = Field(default=None, description="A remembered member's show or title.")


class Thread(BaseModel):
    """A question several sources speak to this week (chorus/threads.py).
    Every member is a real highlight, and a thread always spans at least two
    sources, so it is grounded and cross-source by construction."""

    question: str = Field(description="The shared question, in plain words.")
    members: list[ThreadMember] = Field(description="The highlights that speak to it.")

    @property
    def disagreement(self) -> bool:
        return any(m.stance == "disagrees" for m in self.members)


class Digest(BaseModel):
    soul_version: str = Field(description="Content hash identifying the lens used for curation.")
    soul_origin: str = Field(
        default="supplied",
        description="How the soul was bootstrapped for provenance.",
    )
    episodes: list[EpisodeDigest] = Field(description="Per-episode curation results.")
    threads: list[Thread] = Field(
        default_factory=list,
        description="Questions two or more sources speak to, disagreements first.",
    )

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


class Citation(BaseModel):
    """What a script line rests on: one surfaced highlight (timestamp set) or
    a featured source's brief as a whole (timestamp None) — who the people
    are, what the piece is, when it aired, its thesis."""

    episode_id: str = Field(description="Episode identifier of the cited source.")
    segment_timestamp: float | None = Field(
        default=None,
        description="Timestamp of the cited highlight in seconds; None cites the source brief.",
    )


class Person(BaseModel):
    """Someone speaking in (or authoring) a source, as named by the source."""

    name: str = Field(description="Name as it appears in the source material.")
    role: Literal["host", "guest", "author", "other"] = Field(description="Role in the source.")
    credential: str | None = Field(
        default=None, description="Why they are worth hearing, as stated in the source material."
    )


class BriefPoint(BaseModel):
    """One key point the source makes, tied to the highlight that shows it."""

    text: str = Field(description="The point, paraphrased plainly.")
    segment_timestamp: float = Field(description="Timestamp of the supporting highlight in seconds.")


class SourceBrief(BaseModel):
    """Context the listener needs before any commentary on a source: who is
    talking, what the piece is, why it exists, and what it argues."""

    episode_id: str = Field(description="Source episode's stable identifier.")
    show: str | None = Field(default=None, description="Podcast or channel name.")
    title: str | None = Field(default=None, description="Episode title.")
    published_at: datetime | None = Field(default=None, description="Publication time when known.")
    people: list[Person] = Field(default_factory=list, description="Hosts, guests, authors.")
    context: str = Field(description="Why this conversation exists and what prompted it.")
    thesis: str = Field(description="The core argument in one or two sentences.")
    key_points: list[BriefPoint] = Field(
        default_factory=list, description="The source's key points, each tied to a highlight."
    )


class OutlineSegment(BaseModel):
    """One planned stretch of the episode. The script is written one segment
    at a time, in order, each call seeing everything written before it."""

    name: str = Field(description="Short segment label.")
    # "source" and "connection" are the earlier fixed structure; outlines
    # stored with them still parse and are treated as body segments.
    kind: Literal["intro", "body", "close", "source", "connection"] = Field(
        description="intro, a body segment (any number of sources), or the close."
    )
    source_ids: list[str] = Field(
        default_factory=list, description="Episode ids this segment covers."
    )
    description: str = Field(description="What the segment must establish and argue, in order.")
    size: Literal["short", "medium", "long"] = Field(
        default="medium", description="Relative airtime."
    )


class EpisodeOutline(BaseModel):
    segments: list[OutlineSegment] = Field(description="Segments in broadcast order.")
    also_noted: list[str] = Field(
        default_factory=list,
        description="Episode ids that had highlights but no segment covers; mentioned in the close.",
    )
    target_minutes: int | None = Field(
        default=None,
        description="Spoken length the writer chose, or the principal's fixed length.",
    )


class Turn(BaseModel):
    """One spoken line. A line that states something about a source carries
    citations (a highlight or the source brief); a framing or transition line
    may carry none, and then must not introduce facts (chorus/script.py
    `validate_line`). `episode_id`/`segment_timestamp` mirror the line's
    first citation for callers that read a single anchor."""

    speaker: Literal["host", "cohost"] = Field(description="Which speaker says this line.")
    text: str = Field(description="The spoken line.")
    episode_id: str | None = Field(
        default=None, description="Episode identifier of the line's first citation, if any."
    )
    segment_timestamp: float | None = Field(
        default=None,
        description="Timestamp of the line's first highlight citation in seconds, if any.",
    )
    citations: list[Citation] = Field(
        default_factory=list, description="Highlights or source briefs this line rests on."
    )
    move: str | None = Field(
        default=None, description='Rhetorical move: "setup" or one of TAKE_TYPES.'
    )
    segment_index: int | None = Field(
        default=None, description="Index of the outline segment this line was written for."
    )


class Script(BaseModel):
    soul_version: str = Field(description="Content hash identifying the lens used for this script.")
    takes: list[Take] = Field(description="Grounded opinionated beats composed into the episode.")
    monologue: str = Field(
        description="Readable script text: the single voice, or the HOST/COHOST transcript.",
    )
    turns: list[Turn] = Field(
        default_factory=list,
        description="Spoken lines in order (host only for monologue), each with its citations.",
    )
    format: Literal["monologue", "dialogue"] = Field(
        default="monologue", description="Episode format the audio renderer dispatches on."
    )
    voices: dict[str, str | None] = Field(
        default_factory=dict,
        description=(
            "Speaker role -> ElevenLabs voice id (e.g. profile.speakers[*].voice_id), so "
            "rendering honors a per-speaker voice the request asked for instead of the "
            "renderer's env/default voice."
        ),
    )
    briefs: list[SourceBrief] = Field(
        default_factory=list, description="Context for each featured source, in outline order."
    )
    outline: EpisodeOutline | None = Field(
        default=None, description="The segment plan the script was written against."
    )


class Chapter(BaseModel):
    """One chapter of the rendered episode (chorus/chapters.py). Start times
    are estimated from each section's share of the spoken text; the source
    link is exact, taken from the section's first cited highlight."""

    start_seconds: float = Field(description="Where the chapter starts in the episode audio.")
    title: str = Field(description="Chapter title: the outline segment name or the source.")
    episode_id: str | None = Field(
        default=None, description="Source episode the chapter first cites, if any."
    )
    source_timestamp: float | None = Field(
        default=None, description="Time in the source of the chapter's first cited highlight."
    )
    url: str | None = Field(
        default=None, description="Link that opens the source at source_timestamp when known."
    )


class QuickReason(BaseModel):
    """One reason behind a quick take's verdict, resting on one highlight."""

    text: str = Field(description="The reason, in a sentence.")
    highlight_id: str = Field(description="The highlight it rests on.")
    segment_timestamp: float = Field(description="Where that highlight is in the episode.")
    quote: str = Field(description="The highlight's verbatim quote.")


class QuickTake(BaseModel):
    """Is this one episode worth the principal's time? (chorus/quick_take.py)"""

    episode_id: str = Field(description="The episode judged.")
    title: str | None = Field(default=None, description="Its title when known.")
    show: str | None = Field(default=None, description="Its show when known.")
    verdict: Literal["listen", "skim", "skip"] = Field(
        description="listen: worth the whole thing; skim: worth the cited moments; skip: not for you."
    )
    reasons: list[QuickReason] = Field(
        default_factory=list, description="Why, each citing a highlight; empty on skip."
    )
    monologue: str = Field(description="The 60 to 90 second spoken take.")


class JobStatus(StrEnum):
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
    context_sources: list[str] = Field(
        default_factory=list,
        description="Context sources that contributed to this run (DigestRequest.context_blocks).",
    )
    skipped: list[SkippedEpisode] = Field(
        default_factory=list,
        description="Episodes ingest could not resolve a transcript for, with the reason.",
    )
    llm_tokens: LLMTokens | None = Field(
        default=None,
        description="Model tokens spent by curation, when the client meters them.",
    )
    brain: str | None = Field(
        default=None,
        description='Who scored and scripted: "host" (the calling agent\'s own model) or unset.',
    )
    brain_model: str | None = Field(
        default=None,
        description="The model the host agent reported using, when brain is host.",
    )
    rubric_version: str | None = Field(
        default=None,
        description="Version of the scoring and script instructions Chorus served to the brain.",
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
    owner: str = Field(
        default=DEFAULT_OWNER,
        description=(
            '"master" (the master CHORUS_API_TOKEN) or the email an issued key was '
            'issued to. Defaults to "master" for jobs created before ownership existed. '
            "Only the owner (or master) may read this job or its artifact."
        ),
    )
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
    artifact_token: str | None = Field(
        default=None,
        description=(
            "Random unguessable token minted when this job's audio is uploaded to a "
            "non-private object store (chorus.artifacts.VercelBlobStore); folded into "
            "the stored object's name so the URL alone cannot be guessed from job_id."
        ),
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
    chapters: list[Chapter] = Field(
        default_factory=list,
        description="Chapters written into the episode MP3, with links to each source moment.",
    )
    quick_take: QuickTake | None = Field(
        default=None,
        description="Set when this job is a quick take on one episode (POST /quick-take).",
    )


class CurateResult(BaseModel):
    """What `chorus.pipeline.stage_curate_episode` hands back: the digest plus
    the token spend that produced it (R14) — a per-call return instead of a
    shared client-level counter, so concurrent jobs never attribute each
    other's usage."""

    digest: EpisodeDigest = Field(description="Curation result for one resolved episode.")
    tokens: LLMTokens = Field(
        description="Model tokens spent scoring this episode's windows, isolated to this call."
    )
