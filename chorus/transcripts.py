"""Transcript resolution: a provider ladder behind one Protocol.

Phase C replaces the fixture-only resolver with a chain (§8 row C):
fixtures (tests/dev) -> Supadata managed YouTube captions -> Podcasting 2.0
RSS transcript tags -> Deepgram STT fallback, each trying the next on failure.
`ChainTranscriptProvider` is assembled by `chorus.pipeline.default_deps`.

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

import json
import logging
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Protocol, runtime_checkable
from urllib.parse import urljoin

import httpx
from pydantic import BaseModel, Field, ValidationError

from chorus.errors import RetryableError, TerminalError
from chorus.models import EpisodeInput, Segment, Transcript
from chorus.netguard import MAX_REDIRECT_HOPS, Resolver, UnsafeURLError, safe_url

log = logging.getLogger("chorus.transcripts")

# `[?&]v=` (not a bare `v=`) so a query param like `?nav=...` cannot match;
# `/live/` added alongside the existing shorts/youtu.be/embed forms.
_ID_RE = re.compile(r"(?:[?&]v=|/shorts/|youtu\.be/|/embed/|/live/)([A-Za-z0-9_-]{11})")
_BARE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")

# fixtures/transcripts/ lives at the project root, two levels up from this file.
FIXTURE_TRANSCRIPTS_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "transcripts"

# Podcasting 2.0 namespace (podcastindex.org/namespace/1.0) — <podcast:transcript>.
PODCAST_NS = "https://podcastindex.org/namespace/1.0"
_TRANSCRIPT_TAG = f"{{{PODCAST_NS}}}transcript"

# Preference order for podcast:transcript @type (richest/cheapest to parse first).
_TRANSCRIPT_MIME_PREFERENCE = ("application/json", "text/vtt", "application/srt")

SUPADATA_BASE_URL = "https://api.supadata.ai/v1"
SUPADATA_TIMEOUT_S = 20.0

RSS_TIMEOUT_S = 20.0

DEEPGRAM_LISTEN_URL = "https://api.deepgram.com/v1/listen"
DEEPGRAM_PARAMS = {"model": "nova-3", "smart_format": "true", "utterances": "true"}
DEEPGRAM_TIMEOUT_S = 120.0
# Fallback path (no utterances in the response): group word timings into
# ~10s pseudo-segments so downstream windowing still has something to chew on.
DEEPGRAM_WORD_GROUP_SECONDS = 10.0

# R15 (docs/REVIEW_WAVE1.md #15): hard byte ceilings per response kind. A
# caller-controlled endpoint (feed_url, a feed's own transcript/enclosure
# URLs, audio_url handed to Deepgram) must never be able to force us to
# buffer an unbounded body.
MAX_FEED_BYTES = 5 * 1024 * 1024
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
    content: list[_SupadataContentItem] = Field(default_factory=list)


class _Pc20Segment(BaseModel):
    body: str | None = None
    startTime: float | None = None


class _Pc20Transcript(BaseModel):
    segments: list[_Pc20Segment] = Field(default_factory=list)


class _DeepgramUtterance(BaseModel):
    start: float
    transcript: str | None = None


class _DeepgramWord(BaseModel):
    word: str
    start: float


class _DeepgramAlternative(BaseModel):
    words: list[_DeepgramWord] = Field(default_factory=list)


class _DeepgramChannel(BaseModel):
    alternatives: list[_DeepgramAlternative] = Field(default_factory=list)


class _DeepgramResults(BaseModel):
    utterances: list[_DeepgramUtterance] | None = None
    channels: list[_DeepgramChannel] = Field(default_factory=list)


class _DeepgramResponse(BaseModel):
    results: _DeepgramResults = Field(default_factory=_DeepgramResults)


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
    """YouTube captions via the Supadata API.

    Verified 21 Sep 2026 against Supadata's published docs
    (https://docs.supadata.ai/get-transcript, YouTube-specific endpoint
    confirmed via https://supadata.ai/youtube-transcript-api and the
    supadata-ai/supadata-docs reference):

        GET https://api.supadata.ai/v1/youtube/transcript?videoId=<id>
        header: x-api-key: <key>
        200 -> {"content": [{"text": str, "offset": ms, "duration": ms,
                              "lang": str}], "lang": str, "availableLangs": [...]}
        404 -> video doesn't exist / is private (no transcript)
        401/403 -> auth required or access restricted
        5xx -> transport failure

    `offset`/`duration` are milliseconds; Segment.start wants seconds.
    """

    def __init__(self, api_key: str, base_url: str = SUPADATA_BASE_URL) -> None:
        self.api_key = api_key
        self.base_url = base_url

    def get(self, episode: EpisodeInput) -> Transcript:
        try:
            video_id = episode.resolved_id()
        except ValueError as err:
            raise TranscriptNotFound(f"supadata: episode has no id ({err})") from err
        if video_id.startswith("rss-"):
            # Not a YouTube episode; nothing this provider can do.
            raise TranscriptNotFound("supadata: episode is not a YouTube episode")

        try:
            resp = httpx.get(
                f"{self.base_url}/youtube/transcript",
                params={"videoId": video_id},
                headers={"x-api-key": self.api_key},
                timeout=SUPADATA_TIMEOUT_S,
            )
        except httpx.TimeoutException as err:
            raise TranscriptProviderError(f"supadata: timeout fetching {video_id}: {err}") from err
        except httpx.HTTPError as err:
            raise TranscriptProviderError(
                f"supadata: transport error fetching {video_id}: {err}"
            ) from err

        if resp.status_code == 404:
            raise TranscriptNotFound(f"supadata: no transcript for {video_id}")
        if resp.status_code in (401, 403):
            raise TranscriptProviderError(f"supadata: auth failed ({resp.status_code})")
        if resp.status_code >= 500:
            raise TranscriptProviderError(f"supadata: server error {resp.status_code}")
        if resp.status_code != 200:
            raise TranscriptProviderError(f"supadata: unexpected status {resp.status_code}")
        if len(resp.content) > MAX_TRANSCRIPT_BYTES:
            raise TranscriptProviderError(
                f"supadata: response for {video_id} exceeds MAX_TRANSCRIPT_BYTES={MAX_TRANSCRIPT_BYTES}"
            )

        try:
            parsed = _SupadataResponse.model_validate(resp.json())
        except _PAYLOAD_ERRORS as err:
            raise TranscriptProviderError(
                f"supadata: malformed response for {video_id}: {err}"
            ) from err

        if not parsed.content:
            raise TranscriptNotFound(f"supadata: empty transcript for {video_id}")
        segments = [
            Segment(start=item.offset / 1000.0, text=item.text)
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

    def __init__(self, status_code: int, headers: httpx.Headers, content: bytes) -> None:
        self.status_code = status_code
        self.headers = headers
        self.content = content

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
    **httpx_kwargs: Any,
) -> _BoundedResponse:
    """R10 + R15: fetch `url` behind `chorus.netguard.safe_url` (every hop,
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
                if content_length is not None:
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
                        raise TranscriptProviderError(
                            f"{what}: response exceeded {max_bytes} bytes at {safe}"
                        )
                return _BoundedResponse(resp.status_code, resp.headers, bytes(body))
        except httpx.TimeoutException as err:
            raise TranscriptProviderError(f"{what}: timeout fetching {url}: {err}") from err
        except httpx.HTTPError as err:
            raise TranscriptProviderError(f"{what}: transport error fetching {url}: {err}") from err


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
        out.append(Segment(start=float(seg.startTime), text=seg.body))
    return out


# WebVTT and SRT cues share the same shape (an optional index/identifier line,
# a "start --> end" timestamp line, one or more text lines, a blank line) and
# differ only in the decimal separator (VTT '.', SRT ','), which this regex
# accepts either way — so one parser covers both formats.
_CUE_TS_RE = re.compile(r"(\d{2}):(\d{2}):(\d{2})[.,](\d{3})\s*-->")


def _parse_cues(text: str) -> list[Segment]:
    segments: list[Segment] = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        match = _CUE_TS_RE.search(lines[i])
        if match:
            h, m, s, ms = (int(g) for g in match.groups())
            start = h * 3600 + m * 60 + s + ms / 1000.0
            i += 1
            body: list[str] = []
            while i < len(lines) and lines[i].strip():
                body.append(lines[i].strip())
                i += 1
            if body:
                segments.append(Segment(start=start, text=" ".join(body)))
        i += 1
    return segments


class RssTranscriptProvider:
    """Podcasting 2.0 `<podcast:transcript>` tags: fetch the feed, find the
    item by <guid> (or, failing that, by <enclosure url>), prefer the JSON
    transcript, then VTT, then SRT."""

    def __init__(self, timeout_s: float = RSS_TIMEOUT_S, resolver: Resolver | None = None) -> None:
        self.timeout_s = timeout_s
        # Injectable DNS resolver (R10): production leaves this None (real
        # DNS); tests pass a fixed hostname->address map.
        self.resolver = resolver

    def _fetch_feed(self, feed_url: str) -> ET.Element:
        resp = _fetch_bounded(
            "GET",
            feed_url,
            timeout_s=self.timeout_s,
            what="rss:feed",
            max_bytes=MAX_FEED_BYTES,
            resolver=self.resolver,
        )
        if resp.status_code >= 500:
            raise TranscriptProviderError(f"rss: server error {resp.status_code} fetching {feed_url}")
        if resp.status_code != 200:
            raise TranscriptNotFound(f"rss: feed unavailable ({resp.status_code}) at {feed_url}")
        try:
            return ET.fromstring(resp.content)
        except ET.ParseError as err:
            raise TranscriptProviderError(f"rss: malformed feed at {feed_url}: {err}") from err

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

        root = self._fetch_feed(episode.feed_url)
        item = self._find_item(root, episode.guid, episode.audio_url)
        if item is None:
            raise TranscriptNotFound(
                f"rss: no item matched guid/audio_url in {episode.feed_url}"
            )

        by_type = {
            tag.get("type"): tag.get("url")
            for tag in item.findall(_TRANSCRIPT_TAG)
            if tag.get("url")
        }
        if not by_type:
            raise TranscriptNotFound(f"rss: no podcast:transcript tag in {episode.feed_url}")

        video_id = episode.resolved_id()
        for mime in _TRANSCRIPT_MIME_PREFERENCE:
            url = by_type.get(mime)
            if not url:
                continue
            resp = self._fetch_transcript_file(url)
            segments = _parse_pc20_json(resp.text) if mime == "application/json" else _parse_cues(resp.text)
            if segments:
                _enforce_transcript_limits(segments, what=f"rss:{video_id}")
                tag = "json" if mime == "application/json" else mime.rsplit("/", 1)[-1]
                return Transcript(video_id=video_id, segments=segments, source=f"rss:{tag}")

        raise TranscriptNotFound(
            f"rss: transcript tag(s) present but none parsed to segments ({episode.feed_url})"
        )

    def enclosure_audio_url(self, episode: EpisodeInput) -> str | None:
        """The <enclosure url> for the matched item, so DeepgramTranscriptProvider
        has something to transcribe when the episode only carries feed_url+guid."""
        if episode.audio_url:
            return episode.audio_url
        if not episode.feed_url or not episode.guid:
            return None
        try:
            root = self._fetch_feed(episode.feed_url)
        except (TranscriptNotFound, TranscriptProviderError) as err:
            log.warning("rss: could not resolve enclosure for %s: %s", episode.feed_url, err)
            return None
        item = self._find_item(root, episode.guid, episode.audio_url)
        if item is None:
            return None
        enclosure = item.find("enclosure")
        return enclosure.get("url") if enclosure is not None else None


def _group_words_into_segments(words: list[_DeepgramWord], window_s: float) -> list[Segment]:
    if not words:
        return []
    segments: list[Segment] = []
    window_start = words[0].start
    buf: list[str] = []
    for w in words:
        if buf and w.start - window_start >= window_s:
            segments.append(Segment(start=window_start, text=" ".join(buf)))
            window_start = w.start
            buf = []
        buf.append(w.word)
    if buf:
        segments.append(Segment(start=window_start, text=" ".join(buf)))
    return segments


class DeepgramTranscriptProvider:
    """STT fallback for episodes with an audio file (direct `audio_url`, or
    one resolved from the RSS `<enclosure>` via `RssTranscriptProvider`).

    Verified 21 Sep 2026 against Deepgram's published reference
    (https://developers.deepgram.com/reference/speech-to-text/listen-pre-recorded):

        POST https://api.deepgram.com/v1/listen?model=nova-3&smart_format=true&utterances=true
        header: Authorization: Token <key>
        body: {"url": "<audio url>"}
        200 -> {"results": {
                  "utterances": [{"start": s, "end": s, "transcript": str, ...}],
                  "channels": [{"alternatives": [{"words": [
                      {"word": str, "start": s, "end": s, ...}]}]}]}}

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
        audio_url = episode.audio_url or self.rss_provider.enclosure_audio_url(episode)
        if not audio_url:
            raise TranscriptNotFound("deepgram: episode has no resolvable audio_url")
        try:
            safe_url(audio_url, resolver=self.resolver)
        except UnsafeURLError as err:
            raise TranscriptProviderError(f"deepgram: refused unsafe audio_url {audio_url!r}: {err}") from err

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
                Segment(start=u.start, text=u.transcript)
                for u in parsed.results.utterances
                if u.transcript
            ]
        else:
            alternatives = parsed.results.channels[0].alternatives if parsed.results.channels else []
            words = alternatives[0].words if alternatives else []
            segments = _group_words_into_segments(words, DEEPGRAM_WORD_GROUP_SECONDS)

        if not segments:
            raise TranscriptNotFound(f"deepgram: empty transcript for {audio_url}")

        video_id = episode.resolved_id()
        _enforce_transcript_limits(segments, what=f"deepgram:{video_id}")
        return Transcript(video_id=video_id, segments=segments, source="deepgram")


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
