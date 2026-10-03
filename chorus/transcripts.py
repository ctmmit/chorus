"""Transcript resolution: a provider ladder behind one Protocol.

The ladder (reports/Podcast transcript sources.md, 02 Oct 2026), each rung
trying the next on failure: fixtures (tests/dev) -> publisher Podcasting 2.0
`podcast:transcript` tag -> AssemblyAI speech-to-text on the RSS enclosure ->
Deepgram speech-to-text (backup) -> Supadata native YouTube captions (last
resort). `ChainTranscriptProvider` is assembled by
`chorus.config_env.build_transcript_chain`, the single builder behind both
`chorus.config_env.build_deps` and `chorus.pipeline.default_deps`.

Every provider implements `TranscriptProvider.get(episode) -> Transcript`,
raises `TranscriptNotFound` when the source has nothing for this episode, and
raises `TranscriptProviderError` on a transport/auth failure it cannot
recover from itself (explicit, never swallowed) — the chain interprets both
as "try the next provider" but records the reason.

R10/R15/R16 (docs/REVIEW_WAVE1.md): every outbound URL a caller can influence
(feed_url, audio_url, a transcript URL or enclosure advertised inside a feed)
is validated through chorus.netguard.safe_url before it is fetched, with
redirects re-validated per hop; every fetch is byte-capped and every provider
payload is parsed through a typed Pydantic boundary model, so a malformed or
oversized response becomes an explicit TranscriptProviderError instead of an
unbounded read or a bare KeyError/TypeError escaping the provider taxonomy.
"""
from __future__ import annotations

import html
import json
import logging
import re
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, cast, runtime_checkable
from urllib.parse import urljoin

import httpx
from pydantic import BaseModel, Field, ValidationError

from chorus import paths
from chorus.errors import RetryableError, TerminalError
from chorus.models import EpisodeInput, Segment, Transcript
from chorus.netguard import MAX_REDIRECT_HOPS, Resolver, UnsafeURLError, safe_url

log = logging.getLogger("chorus.transcripts")

# `[?&]v=` (not a bare `v=`) so a query param like `?nav=...` cannot match;
# `/live/` added alongside the existing shorts/youtu.be/embed forms.
_ID_RE = re.compile(r"(?:[?&]v=|/shorts/|youtu\.be/|/embed/|/live/)([A-Za-z0-9_-]{11})")
_BARE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")

# fixtures/transcripts/ lives at the project root, two levels up from this file.
FIXTURE_TRANSCRIPTS_DIR = paths.fixtures_dir() / "transcripts"

# Podcasting 2.0 namespace (podcastindex.org/namespace/1.0) — <podcast:transcript>.
PODCAST_NS = "https://podcastindex.org/namespace/1.0"
_TRANSCRIPT_TAG = f"{{{PODCAST_NS}}}transcript"

# podcast:transcript @type handling. Only formats that carry cue timestamps are
# usable for citations, so the preference runs JSON -> VTT -> SRT. The
# namespace spec's enum documents `application/srt`; Buzzsprout emits the
# de-facto `application/x-subrip` instead (research report, 02 Oct 2026).
_MIME_JSON = "application/json"
_MIME_VTT = "text/vtt"
_SRT_MIMES = frozenset({"application/srt", "application/x-subrip", "text/srt"})
# `text/plain` and `text/html` transcripts carry no timestamps at all (Acquired
# on Transistor publishes `.txt` with speakers but no times), so they cannot
# back a citation. They are skipped, never parsed as if timed.
_UNTIMED_MIMES = frozenset({"text/plain", "text/html"})
# Parse order within a tag set: (label used in Transcript.source, accepted mimes).
_TRANSCRIPT_FORMAT_PREFERENCE: tuple[tuple[str, frozenset[str]], ...] = (
    ("json", frozenset({_MIME_JSON})),
    ("vtt", frozenset({_MIME_VTT})),
    ("srt", _SRT_MIMES),
)

# Supadata, verified 02 Oct 2026: `mode` (native | auto | generate, default auto)
# exists on the universal GET /v1/transcript endpoint
# (https://docs.supadata.ai/api-reference/endpoint/transcript/transcript), not on
# /v1/youtube/transcript. `native` returns only captions that already exist;
# `auto` silently falls back to AI transcription billed at 2 credits/minute.
SUPADATA_BASE_URL = "https://api.supadata.ai/v1"
SUPADATA_TIMEOUT_S = 20.0
SUPADATA_MODE = "native"
SUPADATA_YOUTUBE_WATCH_URL = "https://www.youtube.com/watch?v="
# Long videos (>20 min) answer 202 + {"jobId"} and must be polled at
# GET /v1/transcript/{jobId} (statuses queued/active/completed/failed). Kept
# well inside one Vercel function / Inngest step.
SUPADATA_POLL_INTERVAL_S = 2.0
SUPADATA_MAX_WAIT_S = 60.0
_SUPADATA_UNAVAILABLE_CODES = frozenset({"transcript-unavailable", "not-found"})
_SUPADATA_IN_PROGRESS = frozenset({"queued", "active"})

RSS_TIMEOUT_S = 20.0

DEEPGRAM_LISTEN_URL = "https://api.deepgram.com/v1/listen"
# `diarize_model` both enables diarization and selects the model; the older
# `diarize=true` is deprecated and a request setting both is rejected
# (https://developers.deepgram.com/docs/diarization, verified 02 Oct 2026).
DEEPGRAM_PARAMS = {
    "model": "nova-3",
    "smart_format": "true",
    "utterances": "true",
    "diarize_model": "latest",
}
DEEPGRAM_TIMEOUT_S = 120.0
# Fallback path (no utterances in the response): group word timings into
# ~10s pseudo-segments so downstream windowing still has something to chew on.
DEEPGRAM_WORD_GROUP_SECONDS = 10.0

# AssemblyAI, verified 02 Oct 2026 against the API reference
# (https://www.assemblyai.com/docs/api-reference/transcripts/submit and
# .../transcripts/get), the model guide
# (https://www.assemblyai.com/docs/pre-recorded-audio/select-the-speech-model)
# and the diarization guide
# (https://www.assemblyai.com/docs/pre-recorded-audio/label-speakers):
#   POST https://api.assemblyai.com/v2/transcript   header `authorization: <key>`
#     {"audio_url": str, "speaker_labels": bool, "speech_models": [str, ...]}
#   GET  https://api.assemblyai.com/v2/transcript/{id}
#     status: queued | processing | completed | error (+ `error` message string)
#     utterances[]: {speaker "A"|"B"..., text, start, end (ms), words[]}
#     words[]: {text, speaker, start, end (ms), confidence}
# `speech_models` is the current parameter (priority list; singular
# `speech_model` is deprecated). "universal-3-5-pro" is marked Recommended and
# "universal-2" is the documented fallback; both support speaker_labels.
ASSEMBLYAI_BASE_URL = "https://api.assemblyai.com/v2"
ASSEMBLYAI_SPEECH_MODELS = ["universal-3-5-pro", "universal-2"]
ASSEMBLYAI_REQUEST_TIMEOUT_S = 20.0
ASSEMBLYAI_POLL_INTERVAL_S = 3.0
# Submit + poll must fit one Vercel function / one Inngest step. ~37 s per audio
# hour (report) leaves ample headroom for a three-hour episode; on timeout the
# provider raises TranscriptProviderError so Inngest retries the step.
ASSEMBLYAI_MAX_WAIT_S = 240.0
_ASSEMBLYAI_IN_PROGRESS = frozenset({"queued", "processing"})
# Words regrouped into segments break on a speaker change or after this many seconds.
ASSEMBLYAI_WORD_GROUP_SECONDS = 10.0
_MS_PER_SECOND = 1000.0

# R15 (docs/REVIEW_WAVE1.md #15): hard byte ceilings per response kind. A
# caller-controlled endpoint (feed_url, a feed's own transcript/enclosure
# URLs, audio_url handed to Deepgram) must never be able to force us to
# buffer an unbounded body.
# Feeds list newest episodes first, and popular shows publish very large
# feeds (measured 02 Oct 2026: Odd Lots 6.2 MiB, a large Simplecast show
# 8.5 MiB, Tim Ferriss 29.1 MiB). Read a bounded prefix and parse the
# episodes that arrived complete; fall back to a full read, capped at
# MAX_FEED_FULL_BYTES, only when the prefix can't answer the question.
FEED_PREFIX_BYTES = 2 * 1024 * 1024
MAX_FEED_FULL_BYTES = 64 * 1024 * 1024
# Largest feed body ever buffered (name kept for importers).
MAX_FEED_BYTES = MAX_FEED_FULL_BYTES
MAX_TRANSCRIPT_BYTES = 10 * 1024 * 1024
MAX_STT_RESPONSE_BYTES = 20 * 1024 * 1024
# Cap on <item> elements scanned per feed — independent of byte size, since a
# feed can stay under MAX_FEED_BYTES while still listing an enormous number
# of items to linearly scan for a guid/enclosure match.
MAX_FEED_ITEMS = 2_000
# Caps applied to every resolved transcript regardless of provider, so a
# pathological (or hostile) response can't blow up downstream LLM cost.
MAX_SEGMENTS = 20_000
MAX_TRANSCRIPT_CHARS = 1_500_000
MAX_DURATION_SECONDS = 6 * 60 * 60


class TranscriptNotFound(TerminalError):
    """No transcript could be resolved for an episode (missing/removed/no
    captions). Terminal: retrying without a different input won't help."""


class TranscriptProviderError(RetryableError):
    """A provider's transport or auth failed (timeout, 401/403, 5xx), or its
    response was malformed/oversized. Distinct from TranscriptNotFound: the
    source may well have a transcript, we just couldn't reach or trust it.
    The chain treats this as "try the next provider" too, but it is never
    silently discarded — callers see the reason, and it is retryable at the
    job level when every provider in the chain failed this way."""


def extract_video_id(url_or_id: str) -> str:
    match = _ID_RE.search(url_or_id)
    if match:
        return match.group(1)
    if _BARE_ID_RE.match(url_or_id):
        return url_or_id
    raise ValueError(f"cannot parse a video id from: {url_or_id!r}")


@runtime_checkable
class TranscriptProvider(Protocol):
    def get(self, episode: EpisodeInput) -> Transcript: ...


class FixtureTranscriptProvider:
    """Reads `<resolved_id>.json` from the fixtures directory. Always first in
    the chain so tests and `path_test` never depend on a live third party."""

    def __init__(self, directory: Path | None = None) -> None:
        self.directory = directory or FIXTURE_TRANSCRIPTS_DIR

    def get(self, episode: EpisodeInput) -> Transcript:
        video_id = episode.resolved_id()
        path = self.directory / f"{video_id}.json"
        if not path.exists():
            raise TranscriptNotFound(f"no transcript fixture for {video_id} at {path}")
        data = json.loads(path.read_text(encoding="utf-8"))
        return Transcript(
            video_id=data.get("video_id", video_id),
            segments=data["segments"],
            source="fixture",
        )


# --- R16: typed boundary models for third-party payloads --------------------
#
# Every provider response is parsed through one of these before its fields
# are trusted. json.JSONDecodeError/ValidationError/KeyError/TypeError while
# decoding or validating are translated into TranscriptProviderError — never
# NotFound (a malformed 200 is not "no transcript", it's "couldn't parse
# one") and never a bare exception escaping the provider's documented
# NotFound/ProviderError taxonomy.


class _SupadataContentItem(BaseModel):
    text: str | None = None
    offset: float
    duration: float | None = None


class _SupadataResponse(BaseModel):
    """A 200 transcript body, or a polled job result (which also carries
    `status`, and an `error` object when the job failed)."""

    content: list[_SupadataContentItem] = Field(default_factory=list)
    status: str | None = None
    error: Any = None


class _SupadataJob(BaseModel):
    jobId: str


class _Pc20Segment(BaseModel):
    body: str | None = None
    startTime: float | None = None
    # Buzzsprout emits real speaker names; tolerate a numeric id too.
    speaker: str | int | None = None


class _Pc20Transcript(BaseModel):
    segments: list[_Pc20Segment] = Field(default_factory=list)


class _DeepgramUtterance(BaseModel):
    start: float
    transcript: str | None = None
    speaker: int | None = None


class _DeepgramWord(BaseModel):
    word: str
    start: float
    speaker: int | None = None


class _DeepgramAlternative(BaseModel):
    words: list[_DeepgramWord] = Field(default_factory=list)


class _DeepgramChannel(BaseModel):
    alternatives: list[_DeepgramAlternative] = Field(default_factory=list)


class _DeepgramResults(BaseModel):
    utterances: list[_DeepgramUtterance] | None = None
    channels: list[_DeepgramChannel] = Field(default_factory=list)


class _DeepgramResponse(BaseModel):
    results: _DeepgramResults = Field(default_factory=_DeepgramResults)


class _AssemblyAIWord(BaseModel):
    text: str
    start: float  # milliseconds
    speaker: str | None = None


class _AssemblyAIUtterance(BaseModel):
    text: str | None = None
    start: float  # milliseconds
    speaker: str | None = None


class _AssemblyAISubmitResponse(BaseModel):
    id: str
    status: str
    error: str | None = None


class _AssemblyAITranscript(BaseModel):
    status: str
    error: str | None = None
    utterances: list[_AssemblyAIUtterance] | None = None
    words: list[_AssemblyAIWord] | None = None


@dataclass(frozen=True)
class _TimedWord:
    """A provider-neutral word: `start` in seconds, `speaker` already a
    display label (or None)."""

    text: str
    start: float
    speaker: str | None


def _speaker_label(raw: str | int | None) -> str | None:
    """Diarization ids ("A", 0) -> "Speaker A" / "Speaker 0". Publisher-supplied
    names are passed through untouched by their own parsers, never here."""
    if raw is None or raw == "":
        return None
    return f"Speaker {raw}"


def _group_timed_words(words: list[_TimedWord], window_s: float) -> list[Segment]:
    """Group word timings into segments, breaking on a speaker change or once
    `window_s` seconds have elapsed since the group began."""
    segments: list[Segment] = []
    buf: list[str] = []
    window_start = 0.0
    current_speaker: str | None = None
    for w in words:
        if buf and (w.speaker != current_speaker or w.start - window_start >= window_s):
            segments.append(Segment(start=window_start, text=" ".join(buf), speaker=current_speaker))
            buf = []
        if not buf:
            window_start = w.start
            current_speaker = w.speaker
        buf.append(w.text)
    if buf:
        segments.append(Segment(start=window_start, text=" ".join(buf), speaker=current_speaker))
    return segments


def _resolve_safe_audio_url(
    episode: EpisodeInput,
    rss_provider: RssTranscriptProvider,
    resolver: Resolver | None,
    *,
    what: str,
) -> str:
    """Shared by both speech-to-text providers: the episode's direct
    `audio_url`, else the RSS enclosure, validated through netguard before the
    vendor is asked to fetch it (defense in depth — the GET happens on the
    vendor's side, not ours)."""
    audio_url = episode.audio_url or rss_provider.enclosure_audio_url(episode)
    if not audio_url:
        raise TranscriptNotFound(f"{what}: episode has no resolvable audio_url")
    try:
        safe_url(audio_url, resolver=resolver)
    except UnsafeURLError as err:
        raise TranscriptProviderError(f"{what}: refused unsafe audio_url {audio_url!r}: {err}") from err
    return audio_url


# Exceptions a malformed/unexpected-shape payload can raise while we decode
# or validate it — every provider's payload handling catches exactly this set.
_PAYLOAD_ERRORS = (json.JSONDecodeError, ValidationError, KeyError, TypeError)


def _enforce_transcript_limits(segments: list[Segment], *, what: str) -> None:
    """R15: reject (never silently truncate) a transcript whose segment
    count, total characters, or implied duration exceeds the configured
    caps — a pathological or hostile response should fail loudly, not get
    quietly cut down and passed on to the LLM anyway."""
    if len(segments) > MAX_SEGMENTS:
        raise TranscriptProviderError(
            f"{what}: {len(segments)} segments exceeds MAX_SEGMENTS={MAX_SEGMENTS}"
        )
    total_chars = sum(len(s.text) for s in segments)
    if total_chars > MAX_TRANSCRIPT_CHARS:
        raise TranscriptProviderError(
            f"{what}: {total_chars} characters exceeds MAX_TRANSCRIPT_CHARS={MAX_TRANSCRIPT_CHARS}"
        )
    if segments:
        duration = max(s.start for s in segments)
        if duration > MAX_DURATION_SECONDS:
            raise TranscriptProviderError(
                f"{what}: duration {duration:.0f}s exceeds MAX_DURATION_SECONDS={MAX_DURATION_SECONDS}"
            )


class ManagedCaptionsProvider:
    """Existing YouTube captions via the Supadata API, last rung of the ladder.

    Verified 02 Oct 2026 against Supadata's docs
    (https://docs.supadata.ai/api-reference/endpoint/transcript/transcript and
    .../transcript/transcript-get):

        GET https://api.supadata.ai/v1/transcript?url=<watch url>&mode=native
        header: x-api-key: <key>
        200 -> {"content": [{"text": str, "offset": ms, "duration": ms,
                              "lang": str}], "lang": str, "availableLangs": [...]}
        202 -> {"jobId": str}   (videos over ~20 min; poll the job below)
        206 -> transcript unavailable (no captions exist)
        404 -> video doesn't exist / is private
        401/403 -> auth required or access restricted
        GET /v1/transcript/{jobId} -> {"status": queued|active|completed|failed,
                                       "content": [...], "error": {...}}

    `mode=native` is deliberate: the default `auto` silently falls back to AI
    transcription at 2 credits per audio minute (about 150 credits for a
    75-minute episode with no captions). Missing captions must surface as
    TranscriptNotFound so the ladder, not Supadata, decides what to pay for.
    The older /v1/youtube/transcript endpoint has no `mode` parameter, which is
    why this provider uses the universal endpoint. YouTube captions breach
    YouTube's terms, fail from cloud IPs unless a vendor absorbs it, and carry
    no speaker labels, which is why this provider runs last.

    `offset`/`duration` are milliseconds; Segment.start wants seconds.
    """

    def __init__(
        self,
        api_key: str,
        base_url: str = SUPADATA_BASE_URL,
        *,
        poll_interval_s: float = SUPADATA_POLL_INTERVAL_S,
        max_wait_s: float = SUPADATA_MAX_WAIT_S,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url
        self.poll_interval_s = poll_interval_s
        self.max_wait_s = max_wait_s
        # Injectable so tests never actually wait.
        self._sleep = sleep
        self._clock = clock

    def _request(self, path: str, video_id: str, params: dict[str, str] | None = None) -> Any:
        try:
            resp = httpx.get(
                f"{self.base_url}{path}",
                params=params,
                headers={"x-api-key": self.api_key},
                timeout=SUPADATA_TIMEOUT_S,
            )
        except httpx.TimeoutException as err:
            raise TranscriptProviderError(f"supadata: timeout fetching {video_id}: {err}") from err
        except httpx.HTTPError as err:
            raise TranscriptProviderError(
                f"supadata: transport error fetching {video_id}: {err}"
            ) from err
        if len(resp.content) > MAX_TRANSCRIPT_BYTES:
            raise TranscriptProviderError(
                f"supadata: response for {video_id} exceeds MAX_TRANSCRIPT_BYTES={MAX_TRANSCRIPT_BYTES}"
            )
        return resp

    @staticmethod
    def _error_code(error: Any) -> str | None:
        if isinstance(error, str):
            return error
        if isinstance(error, dict):
            code = error.get("error") or error.get("code")
            return str(code) if code else None
        return None

    def _await_job(self, job_id: str, video_id: str) -> _SupadataResponse:
        deadline = self._clock() + self.max_wait_s
        while True:
            resp = self._request(f"/transcript/{job_id}", video_id)
            if resp.status_code in (401, 403):
                raise TranscriptProviderError(f"supadata: auth failed ({resp.status_code})")
            if resp.status_code == 404:
                raise TranscriptProviderError(f"supadata: job {job_id} for {video_id} not found")
            if resp.status_code != 200:
                raise TranscriptProviderError(
                    f"supadata: unexpected status {resp.status_code} polling job {job_id}"
                )
            try:
                job = _SupadataResponse.model_validate(resp.json())
            except _PAYLOAD_ERRORS as err:
                raise TranscriptProviderError(
                    f"supadata: malformed job result for {video_id}: {err}"
                ) from err
            if job.status == "completed":
                return job
            if job.status == "failed":
                code = self._error_code(job.error)
                if code in _SUPADATA_UNAVAILABLE_CODES:
                    raise TranscriptNotFound(f"supadata: no native captions for {video_id} ({code})")
                raise TranscriptProviderError(
                    f"supadata: job {job_id} for {video_id} failed: {job.error!r}"
                )
            if job.status not in _SUPADATA_IN_PROGRESS:
                raise TranscriptProviderError(
                    f"supadata: job {job_id} for {video_id} has unexpected status {job.status!r}"
                )
            if self._clock() >= deadline:
                raise TranscriptProviderError(
                    f"supadata: job {job_id} for {video_id} still {job.status} after "
                    f"{self.max_wait_s:.0f}s"
                )
            self._sleep(self.poll_interval_s)

    def get(self, episode: EpisodeInput) -> Transcript:
        try:
            video_id = episode.resolved_id()
        except ValueError as err:
            raise TranscriptNotFound(f"supadata: episode has no id ({err})") from err
        if video_id.startswith("rss-"):
            # Not a YouTube episode; nothing this provider can do.
            raise TranscriptNotFound("supadata: episode is not a YouTube episode")

        resp = self._request(
            "/transcript",
            video_id,
            params={
                "url": f"{SUPADATA_YOUTUBE_WATCH_URL}{video_id}",
                "mode": SUPADATA_MODE,
                "text": "false",
            },
        )
        if resp.status_code in (404, 206):
            raise TranscriptNotFound(
                f"supadata: no native captions for {video_id} (status {resp.status_code})"
            )
        if resp.status_code in (401, 403):
            raise TranscriptProviderError(f"supadata: auth failed ({resp.status_code})")
        if resp.status_code >= 500:
            raise TranscriptProviderError(f"supadata: server error {resp.status_code}")

        try:
            if resp.status_code == 202:
                job_id = _SupadataJob.model_validate(resp.json()).jobId
                parsed = self._await_job(job_id, video_id)
            elif resp.status_code == 200:
                parsed = _SupadataResponse.model_validate(resp.json())
            else:
                raise TranscriptProviderError(f"supadata: unexpected status {resp.status_code}")
        except _PAYLOAD_ERRORS as err:
            raise TranscriptProviderError(
                f"supadata: malformed response for {video_id}: {err}"
            ) from err

        if not parsed.content:
            raise TranscriptNotFound(f"supadata: empty transcript for {video_id}")
        segments = [
            Segment(start=item.offset / _MS_PER_SECOND, text=item.text)
            for item in parsed.content
            if item.text
        ]
        if not segments:
            raise TranscriptNotFound(f"supadata: empty transcript for {video_id}")
        _enforce_transcript_limits(segments, what=f"supadata:{video_id}")
        return Transcript(video_id=video_id, segments=segments, source="supadata")


_REDIRECT_STATUSES = {301, 302, 303, 307, 308}


class _BoundedResponse:
    """Just enough of httpx.Response's surface for existing status-code /
    body-parsing call sites to stay unchanged: `.status_code`, `.headers`,
    `.content`, `.text`, `.json()` — backed by bytes already read under a
    hard ceiling by `_fetch_bounded` below."""

    def __init__(
        self,
        status_code: int,
        headers: httpx.Headers,
        content: bytes,
        truncated: bool = False,
    ) -> None:
        self.status_code = status_code
        self.headers = headers
        self.content = content
        # True when `_fetch_bounded(..., truncate_ok=True)` stopped reading at
        # `max_bytes`: `content` is a prefix of the body, not all of it.
        self.truncated = truncated

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", errors="replace")

    def json(self) -> object:
        return json.loads(self.content)


def _fetch_bounded(
    method: str,
    url: str,
    *,
    timeout_s: float,
    what: str,
    max_bytes: int,
    resolver: Resolver | None = None,
    truncate_ok: bool = False,
    **httpx_kwargs: Any,
) -> _BoundedResponse:
    """`truncate_ok=True` turns the byte ceiling from an error into a stop:
    the first `max_bytes` bytes come back with `.truncated = True` and the
    rest of the body is never read (bounded-prefix feed reads).

    R10 + R15: fetch `url` behind `chorus.netguard.safe_url` (every hop,
    not just the first — a first-hop-safe URL can 3xx to an unsafe one) with
    `follow_redirects=False` and a hard byte ceiling. An over-limit
    Content-Length is rejected before any body is read; a body that grows
    past `max_bytes` while streaming aborts the read rather than
    materializing an unbounded payload. Only raises directly for SSRF
    refusal, too many redirects, an oversized body, or a transport failure —
    a plain non-2xx status is returned to the caller (mirroring the
    unmodified httpx.Response contract) so each provider keeps its own
    documented status-code taxonomy."""
    current = url
    hops = 0
    while True:
        try:
            safe = safe_url(current, resolver=resolver)
        except UnsafeURLError as err:
            raise TranscriptProviderError(f"{what}: refused unsafe url {current!r}: {err}") from err
        try:
            with httpx.stream(
                method, safe, timeout=timeout_s, follow_redirects=False, **httpx_kwargs
            ) as resp:
                if resp.status_code in _REDIRECT_STATUSES:
                    hops += 1
                    if hops > MAX_REDIRECT_HOPS:
                        raise TranscriptProviderError(f"{what}: too many redirects fetching {url}")
                    location = resp.headers.get("location")
                    if not location:
                        raise TranscriptProviderError(
                            f"{what}: redirect with no Location fetching {safe}"
                        )
                    current = urljoin(safe, location)
                    continue

                content_length = resp.headers.get("content-length")
                if content_length is not None and not truncate_ok:
                    try:
                        over_limit = int(content_length) > max_bytes
                    except ValueError:
                        over_limit = False
                    if over_limit:
                        raise TranscriptProviderError(
                            f"{what}: content-length {content_length} exceeds "
                            f"{max_bytes} bytes at {safe}"
                        )

                body = bytearray()
                for chunk in resp.iter_bytes():
                    body.extend(chunk)
                    if len(body) > max_bytes:
                        if truncate_ok:
                            return _BoundedResponse(
                                resp.status_code, resp.headers, bytes(body[:max_bytes]), True
                            )
                        raise TranscriptProviderError(
                            f"{what}: response exceeded {max_bytes} bytes at {safe}"
                        )
                return _BoundedResponse(resp.status_code, resp.headers, bytes(body))
        except httpx.TimeoutException as err:
            raise TranscriptProviderError(f"{what}: timeout fetching {url}: {err}") from err
        except httpx.HTTPError as err:
            raise TranscriptProviderError(f"{what}: transport error fetching {url}: {err}") from err


def _is_item(tag: str) -> bool:
    return tag.rsplit("}", 1)[-1] == "item"


def parse_feed_document(content: bytes, *, truncated: bool) -> ET.Element:
    """Parse an RSS document, or the prefix of one. A prefix is parsed
    incrementally and every <item> whose closing tag never arrived is pruned,
    so callers see a well-formed tree of channel metadata plus only complete
    items. Raises ET.ParseError when the bytes that did arrive are malformed."""
    if not truncated:
        return ET.fromstring(content)
    parser: ET.XMLPullParser = ET.XMLPullParser(events=("start", "end"))
    parser.feed(content)
    root: ET.Element | None = None
    complete: set[int] = set()
    # typeshed types read_events() as a union over every event kind; with
    # events=("start", "end") each record is (event, Element).
    for event, element in cast(Iterator[tuple[str, Any]], parser.read_events()):
        if not isinstance(element, ET.Element):
            continue
        if root is None and event == "start":
            root = element
        if event == "end" and _is_item(element.tag):
            complete.add(id(element))
    if root is None:
        raise ET.ParseError("feed prefix contained no XML elements")
    for parent in list(root.iter()):
        for child in list(parent):
            if _is_item(child.tag) and id(child) not in complete:
                parent.remove(child)
    return root


def _parse_pc20_json(text: str) -> list[Segment]:
    """Podcasting 2.0 JSON transcript: {"segments": [{"startTime": s, "body": t}]}
    (https://github.com/Podcastindex-org/podcast-namespace/blob/main/docs/examples/transcripts/transcripts.md,
    verified 21 Sep 2026). Timestamps are already seconds."""
    try:
        parsed = _Pc20Transcript.model_validate(json.loads(text))
    except _PAYLOAD_ERRORS as err:
        raise TranscriptProviderError(f"rss: malformed podcast:transcript JSON: {err}") from err
    out: list[Segment] = []
    for seg in parsed.segments:
        if seg.body is None or seg.startTime is None:
            continue
        speaker = str(seg.speaker).strip() if seg.speaker is not None else ""
        out.append(Segment(start=float(seg.startTime), text=seg.body, speaker=speaker or None))
    return out


# WebVTT and SRT cues share the same shape (an optional index/identifier line,
# a "start --> end" timestamp line, one or more text lines, a blank line) and
# differ only in the decimal separator (VTT '.', SRT ','), which this regex
# accepts either way — so one parser covers both formats.
#
# The hour is optional (WebVTT allows `00:05.120`) and may be a single digit:
# Omny's VTT writes `0:00:00.450` (research report, 02 Oct 2026). Anchored to
# the start of the line so cue text that happens to contain a clock time and
# an arrow can't be mistaken for a timing line.
_CUE_TS_RE = re.compile(r"^\s*(?:(\d+):)?(\d{1,2}):(\d{2})[.,](\d{1,3})\s*-->")
# WebVTT voice span `<v Speaker Name>` (optionally `<v.class Name>`), closed by `</v>`.
_VOICE_TAG_RE = re.compile(r"<v(?:\.[^\s>]*)*\s+([^>]*)>", re.IGNORECASE)
# Any other cue markup: `<c.yellow>`, `<i>`, `</v>`, inline timestamps `<00:00:01.000>`.
_CUE_MARKUP_RE = re.compile(r"</?[A-Za-z][^>]*>|<\d+:\d{2}(?::\d{2})?[.,]\d{3}>")
_SECONDS_PER_MINUTE = 60
_SECONDS_PER_HOUR = 3600
_MS_DIGITS = 3


def _cue_start_seconds(hours: str | None, minutes: str, seconds: str, fraction: str) -> float:
    ms = int(fraction.ljust(_MS_DIGITS, "0"))
    return (
        int(hours or 0) * _SECONDS_PER_HOUR
        + int(minutes) * _SECONDS_PER_MINUTE
        + int(seconds)
        + ms / _MS_PER_SECOND
    )


def _clean_cue_text(raw: str) -> tuple[str, str | None]:
    """(plain text, first voice-span speaker) for one cue's joined text lines.
    The `<v Name>` tag is removed from the text and its name kept separately
    so downstream windowing never sees markup."""
    voice = _VOICE_TAG_RE.search(raw)
    speaker = html.unescape(voice.group(1)).strip() if voice else ""
    text = html.unescape(_CUE_MARKUP_RE.sub("", raw))
    return " ".join(text.split()), speaker or None


def _parse_cues(text: str) -> list[Segment]:
    segments: list[Segment] = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        match = _CUE_TS_RE.match(lines[i])
        if match:
            hours, minutes, seconds, fraction = match.groups()
            start = _cue_start_seconds(hours, minutes, seconds, fraction)
            i += 1
            body: list[str] = []
            while i < len(lines) and lines[i].strip():
                body.append(lines[i].strip())
                i += 1
            cue_text, speaker = _clean_cue_text(" ".join(body))
            if cue_text:
                segments.append(Segment(start=start, text=cue_text, speaker=speaker))
        i += 1
    return segments


def _normalize_mime(raw: str | None) -> str:
    """`Text/VTT; charset=utf-8` -> `text/vtt`."""
    return (raw or "").split(";", 1)[0].strip().lower()


class RssTranscriptProvider:
    """Podcasting 2.0 `<podcast:transcript>` tags: fetch the feed, find the
    item by <guid> (or, failing that, by <enclosure url>), prefer the JSON
    transcript, then VTT, then SRT."""

    def __init__(self, timeout_s: float = RSS_TIMEOUT_S, resolver: Resolver | None = None) -> None:
        self.timeout_s = timeout_s
        # Injectable DNS resolver (R10): production leaves this None (real
        # DNS); tests pass a fixed hostname->address map.
        self.resolver = resolver

    def _fetch_feed(self, feed_url: str, *, full: bool = False) -> tuple[ET.Element, bool]:
        """(root, truncated). By default only a FEED_PREFIX_BYTES prefix is
        read; `full=True` reads up to MAX_FEED_FULL_BYTES."""
        resp = _fetch_bounded(
            "GET",
            feed_url,
            timeout_s=self.timeout_s,
            what="rss:feed",
            max_bytes=MAX_FEED_FULL_BYTES if full else FEED_PREFIX_BYTES,
            resolver=self.resolver,
            truncate_ok=not full,
        )
        if resp.status_code >= 500:
            raise TranscriptProviderError(f"rss: server error {resp.status_code} fetching {feed_url}")
        if resp.status_code != 200:
            raise TranscriptNotFound(f"rss: feed unavailable ({resp.status_code}) at {feed_url}")
        truncated = getattr(resp, "truncated", False)
        try:
            return parse_feed_document(resp.content, truncated=truncated), truncated
        except ET.ParseError as err:
            raise TranscriptProviderError(f"rss: malformed feed at {feed_url}: {err}") from err

    def _locate_item(
        self, feed_url: str, guid: str | None, audio_url: str | None
    ) -> ET.Element | None:
        """Look in the newest-first prefix; read the whole feed only when the
        item isn't there and the prefix was cut short (an older episode)."""
        root, truncated = self._fetch_feed(feed_url)
        item = self._find_item(root, guid, audio_url)
        if item is None and truncated:
            root, _ = self._fetch_feed(feed_url, full=True)
            item = self._find_item(root, guid, audio_url)
        return item

    @staticmethod
    def _find_item(root: ET.Element, guid: str | None, audio_url: str | None) -> ET.Element | None:
        items = list(root.iter("item"))[:MAX_FEED_ITEMS]
        if guid:
            for item in items:
                guid_el = item.find("guid")
                if guid_el is not None and (guid_el.text or "").strip() == guid:
                    return item
        if audio_url:
            for item in items:
                enclosure = item.find("enclosure")
                if enclosure is not None and enclosure.get("url") == audio_url:
                    return item
        return None

    def _fetch_transcript_file(self, url: str) -> _BoundedResponse:
        resp = _fetch_bounded(
            "GET",
            url,
            timeout_s=self.timeout_s,
            what="rss:transcript",
            max_bytes=MAX_TRANSCRIPT_BYTES,
            resolver=self.resolver,
        )
        if resp.status_code >= 500:
            raise TranscriptProviderError(f"rss: server error {resp.status_code} fetching {url}")
        if resp.status_code != 200:
            raise TranscriptNotFound(f"rss: transcript unavailable ({resp.status_code}) at {url}")
        return resp

    def get(self, episode: EpisodeInput) -> Transcript:
        if not episode.feed_url or not (episode.guid or episode.audio_url):
            raise TranscriptNotFound("rss: episode has no feed_url + guid/audio_url")

        item = self._locate_item(episode.feed_url, episode.guid, episode.audio_url)
        if item is None:
            raise TranscriptNotFound(
                f"rss: no item matched guid/audio_url in {episode.feed_url}"
            )

        # Raw ElementTree pass (not feedparser, which keeps only the LAST repeated
        # podcast:transcript tag and so loses a VTT's speakers when an SRT follows):
        # every tag is kept, in feed order, and the preference below picks.
        tags = [
            (_normalize_mime(tag.get("type")), url)
            for tag in item.findall(_TRANSCRIPT_TAG)
            if (url := tag.get("url"))
        ]
        if not tags:
            raise TranscriptNotFound(f"rss: no podcast:transcript tag in {episode.feed_url}")

        video_id = episode.resolved_id()
        skipped: list[str] = []
        for label, mimes in _TRANSCRIPT_FORMAT_PREFERENCE:
            for mime, url in tags:
                if mime not in mimes:
                    continue
                try:
                    resp = self._fetch_transcript_file(url)
                except TranscriptNotFound as err:
                    skipped.append(f"{mime} unavailable ({err})")
                    continue
                segments = (
                    _parse_pc20_json(resp.text) if label == "json" else _parse_cues(resp.text)
                )
                if segments:
                    _enforce_transcript_limits(segments, what=f"rss:{video_id}")
                    return Transcript(video_id=video_id, segments=segments, source=f"rss:{label}")
                skipped.append(f"{mime} parsed to no segments")

        untimed = sorted({mime for mime, _ in tags if mime in _UNTIMED_MIMES})
        if untimed:
            skipped.append(
                f"skipped untimed {', '.join(untimed)} transcript(s): no timestamps, "
                "not usable for citations"
            )
        unsupported = sorted(
            {
                mime or "(no type)"
                for mime, _ in tags
                if mime not in _UNTIMED_MIMES
                and not any(mime in mimes for _, mimes in _TRANSCRIPT_FORMAT_PREFERENCE)
            }
        )
        if unsupported:
            skipped.append(f"unsupported transcript type(s) {', '.join(unsupported)}")
        raise TranscriptNotFound(
            f"rss: no usable timed transcript among podcast:transcript tags in "
            f"{episode.feed_url} ({'; '.join(skipped)})"
        )

    def enclosure_audio_url(self, episode: EpisodeInput) -> str | None:
        """The <enclosure url> for the matched item, so DeepgramTranscriptProvider
        has something to transcribe when the episode only carries feed_url+guid."""
        if episode.audio_url:
            return episode.audio_url
        if not episode.feed_url or not episode.guid:
            return None
        try:
            item = self._locate_item(episode.feed_url, episode.guid, episode.audio_url)
        except (TranscriptNotFound, TranscriptProviderError) as err:
            log.warning("rss: could not resolve enclosure for %s: %s", episode.feed_url, err)
            return None
        if item is None:
            return None
        enclosure = item.find("enclosure")
        return enclosure.get("url") if enclosure is not None else None


class DeepgramTranscriptProvider:
    """Backup speech-to-text for episodes with an audio file (direct
    `audio_url`, or one resolved from the RSS `<enclosure>` via
    `RssTranscriptProvider`). Runs when AssemblyAI is unconfigured or failed;
    a different model lineage hedges against correlated failure.

    Verified 21 Sep 2026 against Deepgram's published reference
    (https://developers.deepgram.com/reference/speech-to-text/listen-pre-recorded)
    and 02 Oct 2026 for diarization (https://developers.deepgram.com/docs/diarization):

        POST https://api.deepgram.com/v1/listen?model=nova-3&smart_format=true
             &utterances=true&diarize_model=latest
        header: Authorization: Token <key>
        body: {"url": "<audio url>"}
        200 -> {"results": {
                  "utterances": [{"start": s, "end": s, "transcript": str,
                                  "speaker": int, ...}],
                  "channels": [{"alternatives": [{"words": [
                      {"word": str, "start": s, "end": s, "speaker": int, ...}]}]}]}}

    `utterances` is preferred (already segment-shaped); if the response has
    none (utterances can come back empty on some audio), word timings are
    grouped into ~DEEPGRAM_WORD_GROUP_SECONDS pseudo-segments instead.

    `audio_url` is caller-influenced (directly, or via a feed's own
    <enclosure>), so it is validated through chorus.netguard.safe_url (R10)
    before we ask Deepgram to fetch it — defense in depth even though the
    actual GET happens on Deepgram's side, not ours.
    """

    def __init__(
        self,
        api_key: str,
        rss_provider: RssTranscriptProvider | None = None,
        resolver: Resolver | None = None,
    ) -> None:
        self.api_key = api_key
        # When no rss_provider is supplied, build one sharing this provider's
        # resolver — otherwise the default RssTranscriptProvider()'s real-DNS
        # resolver would silently override an injected test/production
        # resolver for the enclosure lookup below.
        self.rss_provider = rss_provider or RssTranscriptProvider(resolver=resolver)
        self.resolver = resolver

    def get(self, episode: EpisodeInput) -> Transcript:
        audio_url = _resolve_safe_audio_url(
            episode, self.rss_provider, self.resolver, what="deepgram"
        )

        resp = _fetch_bounded(
            "POST",
            DEEPGRAM_LISTEN_URL,
            timeout_s=DEEPGRAM_TIMEOUT_S,
            what="deepgram",
            max_bytes=MAX_STT_RESPONSE_BYTES,
            resolver=self.resolver,
            params=DEEPGRAM_PARAMS,
            headers={
                "Authorization": f"Token {self.api_key}",
                "Content-Type": "application/json",
            },
            json={"url": audio_url},
        )

        if resp.status_code in (401, 403):
            raise TranscriptProviderError(f"deepgram: auth failed ({resp.status_code})")
        if resp.status_code == 404:
            raise TranscriptNotFound(f"deepgram: audio not found at {audio_url}")
        if resp.status_code >= 500:
            raise TranscriptProviderError(f"deepgram: server error {resp.status_code}")
        if resp.status_code != 200:
            raise TranscriptProviderError(f"deepgram: unexpected status {resp.status_code}")

        try:
            parsed = _DeepgramResponse.model_validate(resp.json())
        except _PAYLOAD_ERRORS as err:
            raise TranscriptProviderError(f"deepgram: malformed response for {audio_url}: {err}") from err

        if parsed.results.utterances:
            segments = [
                Segment(start=u.start, text=u.transcript, speaker=_speaker_label(u.speaker))
                for u in parsed.results.utterances
                if u.transcript
            ]
        else:
            alternatives = parsed.results.channels[0].alternatives if parsed.results.channels else []
            words = alternatives[0].words if alternatives else []
            segments = _group_timed_words(
                [_TimedWord(w.word, w.start, _speaker_label(w.speaker)) for w in words],
                DEEPGRAM_WORD_GROUP_SECONDS,
            )

        if not segments:
            raise TranscriptNotFound(f"deepgram: empty transcript for {audio_url}")

        video_id = episode.resolved_id()
        _enforce_transcript_limits(segments, what=f"deepgram:{video_id}")
        return Transcript(
            video_id=video_id, segments=segments, source="deepgram", source_audio_url=audio_url
        )


class AssemblyAITranscriptProvider:
    """Primary speech-to-text on the RSS enclosure: AssemblyAI Universal-3.5 Pro
    with diarization (about $0.23 per audio hour, 3.1% independent WER, best
    vendor-run diarization score; reports/Podcast transcript sources.md).

    API shape verified 02 Oct 2026; see the ASSEMBLYAI_* constants for the
    request fields, `speech_models` parameter, status values and response
    shape, and the URLs they were checked against.

    Flow: validate the audio URL (netguard), POST the job, then poll until
    `completed`/`error`. Polling is bounded by `max_wait_s` (240 s) so a call
    fits one Vercel function or one Inngest step; on timeout it raises
    TranscriptProviderError so Inngest retries. A retry submits a NEW job (the
    API has no idempotency key), so a timed-out job is paid for twice; a
    webhook plus `step.waitForEvent` is the planned long-term shape.

    `utterances[]` (ms) map to Segments in seconds with a "Speaker A" label;
    if a completed transcript has no utterances, `words[]` are grouped by
    speaker change / ASSEMBLYAI_WORD_GROUP_SECONDS like the Deepgram fallback.
    `Transcript.source_audio_url` records the exact URL transcribed, since
    dynamic ad insertion can shift timestamps between two downloads.
    """

    def __init__(
        self,
        api_key: str,
        *,
        rss_provider: RssTranscriptProvider | None = None,
        resolver: Resolver | None = None,
        base_url: str = ASSEMBLYAI_BASE_URL,
        poll_interval_s: float = ASSEMBLYAI_POLL_INTERVAL_S,
        max_wait_s: float = ASSEMBLYAI_MAX_WAIT_S,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.api_key = api_key
        # Share this provider's resolver with the enclosure lookup (see Deepgram).
        self.rss_provider = rss_provider or RssTranscriptProvider(resolver=resolver)
        self.resolver = resolver
        self.base_url = base_url
        self.poll_interval_s = poll_interval_s
        self.max_wait_s = max_wait_s
        # Injectable so tests never actually wait.
        self._sleep = sleep
        self._clock = clock

    def _call(self, method: str, path: str, what: str, **kwargs: Any) -> _BoundedResponse:
        resp = _fetch_bounded(
            method,
            f"{self.base_url}{path}",
            timeout_s=ASSEMBLYAI_REQUEST_TIMEOUT_S,
            what=what,
            max_bytes=MAX_STT_RESPONSE_BYTES,
            resolver=self.resolver,
            headers={"authorization": self.api_key, "content-type": "application/json"},
            **kwargs,
        )
        if resp.status_code in (401, 403):
            raise TranscriptProviderError(f"{what}: auth failed ({resp.status_code})")
        if resp.status_code == 429:
            raise TranscriptProviderError(f"{what}: rate limited (429)")
        if resp.status_code >= 500:
            raise TranscriptProviderError(f"{what}: server error {resp.status_code}")
        if resp.status_code != 200:
            raise TranscriptProviderError(f"{what}: unexpected status {resp.status_code}")
        return resp

    def _submit(self, audio_url: str) -> str:
        resp = self._call(
            "POST",
            "/transcript",
            "assemblyai:submit",
            json={
                "audio_url": audio_url,
                "speaker_labels": True,
                "speech_models": ASSEMBLYAI_SPEECH_MODELS,
            },
        )
        try:
            submitted = _AssemblyAISubmitResponse.model_validate(resp.json())
        except _PAYLOAD_ERRORS as err:
            raise TranscriptProviderError(
                f"assemblyai: malformed submit response for {audio_url}: {err}"
            ) from err
        if submitted.status == "error":
            raise TranscriptProviderError(
                f"assemblyai: job {submitted.id} rejected: {submitted.error or 'no message'}"
            )
        return submitted.id

    def _await(self, transcript_id: str, audio_url: str) -> _AssemblyAITranscript:
        deadline = self._clock() + self.max_wait_s
        while True:
            resp = self._call("GET", f"/transcript/{transcript_id}", "assemblyai:poll")
            try:
                result = _AssemblyAITranscript.model_validate(resp.json())
            except _PAYLOAD_ERRORS as err:
                raise TranscriptProviderError(
                    f"assemblyai: malformed transcript {transcript_id} for {audio_url}: {err}"
                ) from err
            if result.status == "completed":
                return result
            if result.status == "error":
                raise TranscriptProviderError(
                    f"assemblyai: job {transcript_id} failed: {result.error or 'no message'}"
                )
            if result.status not in _ASSEMBLYAI_IN_PROGRESS:
                raise TranscriptProviderError(
                    f"assemblyai: job {transcript_id} has unexpected status {result.status!r}"
                )
            if self._clock() >= deadline:
                raise TranscriptProviderError(
                    f"assemblyai: job {transcript_id} still {result.status} after "
                    f"{self.max_wait_s:.0f}s for {audio_url}"
                )
            self._sleep(self.poll_interval_s)

    @staticmethod
    def _segments(result: _AssemblyAITranscript) -> list[Segment]:
        if result.utterances:
            return [
                Segment(
                    start=u.start / _MS_PER_SECOND,
                    text=u.text.strip(),
                    speaker=_speaker_label(u.speaker),
                )
                for u in result.utterances
                if u.text and u.text.strip()
            ]
        words = [
            _TimedWord(w.text, w.start / _MS_PER_SECOND, _speaker_label(w.speaker))
            for w in result.words or []
        ]
        return _group_timed_words(words, ASSEMBLYAI_WORD_GROUP_SECONDS)

    def get(self, episode: EpisodeInput) -> Transcript:
        audio_url = _resolve_safe_audio_url(
            episode, self.rss_provider, self.resolver, what="assemblyai"
        )
        transcript_id = self._submit(audio_url)
        result = self._await(transcript_id, audio_url)
        segments = self._segments(result)
        if not segments:
            raise TranscriptNotFound(f"assemblyai: empty transcript for {audio_url}")

        video_id = episode.resolved_id()
        _enforce_transcript_limits(segments, what=f"assemblyai:{video_id}")
        return Transcript(
            video_id=video_id,
            segments=segments,
            source="assemblyai",
            source_audio_url=audio_url,
        )


class ChainTranscriptProvider:
    """Tries each provider in order.

    R11: a provider result whose `Transcript.video_id` does not equal the
    requested `episode.resolved_id()` is never returned — it is logged and
    treated as that provider's error, so a misbehaving/compromised provider
    can't poison the pipeline (or the cache, via CachingTranscriptProvider)
    with a transcript labeled under the wrong identity.

    R16: `TranscriptNotFound` falls through to the next provider silently;
    `TranscriptProviderError` falls through too but is remembered. If no
    provider resolves anything AND at least one raised `ProviderError`, the
    chain raises an aggregated `TranscriptProviderError` (retryable) instead
    of `TranscriptNotFound` — an all-providers-outage is a transient failure
    the caller should retry, not a terminal "nothing exists" verdict.
    """

    def __init__(self, providers: list[TranscriptProvider]) -> None:
        self.providers = providers

    def get(self, episode: EpisodeInput) -> Transcript:
        try:
            expected_id = episode.resolved_id()
        except ValueError as err:
            raise TranscriptNotFound(f"episode has no resolvable identity: {err}") from err

        reasons: list[str] = []
        saw_provider_error = False
        for provider in self.providers:
            name = type(provider).__name__
            try:
                transcript = provider.get(episode)
            except TranscriptNotFound as err:
                reasons.append(f"{name}: not found ({err})")
                continue
            except TranscriptProviderError as err:
                reasons.append(f"{name}: error ({err})")
                saw_provider_error = True
                continue

            if transcript.video_id != expected_id:
                msg = (
                    f"returned video_id {transcript.video_id!r} != requested "
                    f"{expected_id!r}; refusing possibly-mismatched transcript"
                )
                log.error("transcripts: %s: %s", name, msg)
                reasons.append(f"{name}: error ({msg})")
                saw_provider_error = True
                continue

            return transcript

        if not reasons:
            reasons.append("no providers configured")
        message = f"no provider resolved a transcript: {'; '.join(reasons)}"
        if saw_provider_error:
            raise TranscriptProviderError(message)
        raise TranscriptNotFound(message)
