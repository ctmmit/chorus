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
"""
from __future__ import annotations

import json
import logging
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Protocol, runtime_checkable

import httpx

from chorus.models import EpisodeInput, Segment, Transcript

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


class TranscriptNotFound(Exception):
    """No transcript could be resolved for an episode (missing/removed/no captions)."""


class TranscriptProviderError(Exception):
    """A provider's transport or auth failed (timeout, 401/403, 5xx). Distinct
    from TranscriptNotFound: the source may well have a transcript, we just
    couldn't reach it. The chain treats this as "try the next provider" too,
    but it is never silently discarded — callers see the reason."""


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

        data = resp.json()
        content = data.get("content")
        if not content:
            raise TranscriptNotFound(f"supadata: empty transcript for {video_id}")
        segments = [
            Segment(start=item["offset"] / 1000.0, text=item["text"])
            for item in content
            if item.get("text")
        ]
        if not segments:
            raise TranscriptNotFound(f"supadata: empty transcript for {video_id}")
        return Transcript(video_id=video_id, segments=segments, source="supadata")


def _fetch(url: str, timeout_s: float, what: str) -> httpx.Response:
    """Shared GET + transport-error mapping for the RSS provider (feed and
    transcript-file fetches both need the same treatment)."""
    try:
        resp = httpx.get(url, timeout=timeout_s)
    except httpx.TimeoutException as err:
        raise TranscriptProviderError(f"rss: timeout fetching {what} {url}: {err}") from err
    except httpx.HTTPError as err:
        raise TranscriptProviderError(f"rss: transport error fetching {what} {url}: {err}") from err
    if resp.status_code >= 500:
        raise TranscriptProviderError(f"rss: server error {resp.status_code} fetching {what} {url}")
    if resp.status_code != 200:
        raise TranscriptNotFound(f"rss: {what} unavailable ({resp.status_code}) at {url}")
    return resp


def _parse_pc20_json(text: str) -> list[Segment]:
    """Podcasting 2.0 JSON transcript: {"segments": [{"startTime": s, "body": t}]}
    (https://github.com/Podcastindex-org/podcast-namespace/blob/main/docs/examples/transcripts/transcripts.md,
    verified 21 Sep 2026). Timestamps are already seconds."""
    data = json.loads(text)
    out: list[Segment] = []
    for seg in data.get("segments", []):
        body = seg.get("body")
        start = seg.get("startTime")
        if body is None or start is None:
            continue
        out.append(Segment(start=float(start), text=body))
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

    def __init__(self, timeout_s: float = RSS_TIMEOUT_S) -> None:
        self.timeout_s = timeout_s

    def _fetch_feed(self, feed_url: str) -> ET.Element:
        resp = _fetch(feed_url, self.timeout_s, "feed")
        try:
            return ET.fromstring(resp.content)
        except ET.ParseError as err:
            raise TranscriptProviderError(f"rss: malformed feed at {feed_url}: {err}") from err

    @staticmethod
    def _find_item(root: ET.Element, guid: str | None, audio_url: str | None) -> ET.Element | None:
        items = list(root.iter("item"))
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
            resp = _fetch(url, self.timeout_s, "transcript")
            segments = _parse_pc20_json(resp.text) if mime == "application/json" else _parse_cues(resp.text)
            if segments:
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


def _group_words_into_segments(words: list[dict], window_s: float) -> list[Segment]:
    if not words:
        return []
    segments: list[Segment] = []
    window_start = float(words[0]["start"])
    buf: list[str] = []
    for w in words:
        if buf and float(w["start"]) - window_start >= window_s:
            segments.append(Segment(start=window_start, text=" ".join(buf)))
            window_start = float(w["start"])
            buf = []
        buf.append(str(w["word"]))
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
    """

    def __init__(self, api_key: str, rss_provider: RssTranscriptProvider | None = None) -> None:
        self.api_key = api_key
        self.rss_provider = rss_provider or RssTranscriptProvider()

    def get(self, episode: EpisodeInput) -> Transcript:
        audio_url = episode.audio_url or self.rss_provider.enclosure_audio_url(episode)
        if not audio_url:
            raise TranscriptNotFound("deepgram: episode has no resolvable audio_url")

        try:
            resp = httpx.post(
                DEEPGRAM_LISTEN_URL,
                params=DEEPGRAM_PARAMS,
                headers={
                    "Authorization": f"Token {self.api_key}",
                    "Content-Type": "application/json",
                },
                json={"url": audio_url},
                timeout=DEEPGRAM_TIMEOUT_S,
            )
        except httpx.TimeoutException as err:
            raise TranscriptProviderError(f"deepgram: timeout transcribing {audio_url}: {err}") from err
        except httpx.HTTPError as err:
            raise TranscriptProviderError(
                f"deepgram: transport error transcribing {audio_url}: {err}"
            ) from err

        if resp.status_code in (401, 403):
            raise TranscriptProviderError(f"deepgram: auth failed ({resp.status_code})")
        if resp.status_code == 404:
            raise TranscriptNotFound(f"deepgram: audio not found at {audio_url}")
        if resp.status_code >= 500:
            raise TranscriptProviderError(f"deepgram: server error {resp.status_code}")
        if resp.status_code != 200:
            raise TranscriptProviderError(f"deepgram: unexpected status {resp.status_code}")

        data = resp.json()
        results = data.get("results", {})
        utterances = results.get("utterances")
        if utterances:
            segments = [
                Segment(start=float(u["start"]), text=u["transcript"])
                for u in utterances
                if u.get("transcript")
            ]
        else:
            channels = results.get("channels") or [{}]
            alternatives = channels[0].get("alternatives") or [{}]
            words = alternatives[0].get("words") or []
            segments = _group_words_into_segments(words, DEEPGRAM_WORD_GROUP_SECONDS)

        if not segments:
            raise TranscriptNotFound(f"deepgram: empty transcript for {audio_url}")

        video_id = episode.resolved_id()
        return Transcript(video_id=video_id, segments=segments, source="deepgram")


class ChainTranscriptProvider:
    """Tries each provider in order. TranscriptNotFound and TranscriptProviderError
    both fall through to the next provider; if every provider fails, raises
    TranscriptNotFound whose message lists each provider's reason (so a caller
    debugging a skip can see what was tried, not just that everything failed)."""

    def __init__(self, providers: list[TranscriptProvider]) -> None:
        self.providers = providers

    def get(self, episode: EpisodeInput) -> Transcript:
        reasons: list[str] = []
        for provider in self.providers:
            name = type(provider).__name__
            try:
                return provider.get(episode)
            except TranscriptNotFound as err:
                reasons.append(f"{name}: not found ({err})")
            except TranscriptProviderError as err:
                reasons.append(f"{name}: error ({err})")
        if not reasons:
            reasons.append("no providers configured")
        raise TranscriptNotFound(
            f"no provider resolved a transcript: {'; '.join(reasons)}"
        )
