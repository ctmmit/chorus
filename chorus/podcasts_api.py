"""Podcast discovery and onboarding routes: find a show, turn a pasted URL or
an OPML export into subscription Sources, and build a soul from an interview.

    GET  /podcasts/search?q=&limit=     iTunes Search API -> shows with feed URLs
    POST /podcasts/resolve              RSS / Apple Podcasts / YouTube URL -> Source
    POST /podcasts/import-opml          OPML export -> Sources (+ what was skipped)
    POST /souls/interview               interview answers -> soul markdown

Every route needs a normal bearer token (none is in chorus.app's public
tables). None of them creates a subscription: the client reviews the Sources
first, optionally calls `POST /subscriptions/preview`, then subscribes.

External APIs (shapes checked against Apple's and Google's documentation on
02 Oct 2026; no live call is made from tests):

- iTunes Search API, https://performance-partners.apple.com/search-api: `GET
  https://itunes.apple.com/search?media=podcast&entity=podcast&term=<q>&limit=<n>`
  and `GET https://itunes.apple.com/lookup?id=<n>&entity=podcast`; no key; JSON
  `{"resultCount", "results": [{"collectionId", "collectionName", "artistName",
  "feedUrl", "artworkUrl600", ...}]}`. `lookup?id=<n>&entity=podcastEpisode
  &limit=<m>` returns the show followed by its newest episodes
  (`wrapperType: "podcastEpisode"`, with `trackId`, `trackName`,
  `episodeGuid`, `episodeUrl`, `releaseDate`); chorus.library_resolve uses it
  to turn an Apple episode link into a feed item. Apple documents "approximately 20
  calls per minute", so search results are cached in-process and upstream
  calls are throttled below that ceiling.
- YouTube Data API v3 `channels.list` (https://developers.google.com/youtube/
  v3/docs/channels/list): `forHandle` / `forUsername`, 1 quota unit, official
  API, used only when YOUTUBE_API_KEY is set. YouTube HTML is never scraped.

Outbound HTTP goes through `chorus.feeds.fetch_response`, which is
`chorus.transcripts._fetch_bounded` underneath: SSRF guard, redirect cap, byte
cap.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import xml.etree.ElementTree as ET
from collections import OrderedDict, deque
from collections.abc import Callable
from urllib.parse import parse_qs, urlencode, urlsplit

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field, field_validator

from chorus.bootstrap import get_soul_builder
from chorus.feeds import (
    FeedFetchError,
    fetch_response,
    fetch_xml,
    parse_rss,
    parse_youtube_atom,
    youtube_feed_url,
)
from chorus.models import MAX_TITLE_CHARS, MAX_URL_CHARS
from chorus.netguard import Resolver, UnsafeURLError, safe_url
from chorus.subscriptions import (
    YOUTUBE_CHANNEL_ID_PATTERN,
    RssSource,
    Source,
    YoutubeSource,
)

log = logging.getLogger("chorus.podcasts_api")

ITUNES_SEARCH_URL = "https://itunes.apple.com/search"
ITUNES_LOOKUP_URL = "https://itunes.apple.com/lookup"
YOUTUBE_CHANNELS_URL = "https://www.googleapis.com/youtube/v3/channels"
YOUTUBE_API_KEY_ENV = "YOUTUBE_API_KEY"
YOUTUBE_API_KEY_HEADER = "x-goog-api-key"

SEARCH_DEFAULT_LIMIT = 10
SEARCH_MAX_LIMIT = 25
SEARCH_MAX_QUERY_CHARS = 200
# Results change slowly, and Apple asks for ~20 calls/minute: cache a
# normalized query for an hour, and never exceed 15 upstream calls a minute
# per process (headroom under Apple's figure).
SEARCH_CACHE_TTL_S = 3_600.0
SEARCH_CACHE_MAX_ENTRIES = 512
UPSTREAM_MAX_CALLS_PER_MINUTE = 15
UPSTREAM_WINDOW_S = 60.0
# Always ask Apple for one fixed page and slice per request, so every `limit`
# shares one cache entry per query.
SEARCH_UPSTREAM_LIMIT = SEARCH_MAX_LIMIT
JSON_MAX_BYTES = 2 * 1024 * 1024
# Episodes requested per show lookup; Apple's documented maximum is 200.
ITUNES_EPISODE_LOOKUP_LIMIT = 200

OPML_MAX_BYTES = 1 << 20
OPML_MAX_SOURCES = 500
OPML_LINE_MAX_CHARS = 200
OPML_FEED_TYPES = frozenset({"rss", "atom"})

SOUL_INTERVIEW_KEYS = ("identity", "interests", "triggers", "ignore", "style", "guidance")
MAX_INTERVIEW_ANSWER_CHARS = 4_000

YOUTUBE_HOSTS = frozenset({"youtube.com", "www.youtube.com", "m.youtube.com"})
APPLE_HOSTS = frozenset({"podcasts.apple.com", "itunes.apple.com"})
FEED_URL_SCHEMES = {"feed": "https", "itpc": "https", "pcast": "https", "podcast": "https"}
_APPLE_ID_RE = re.compile(r"/id(\d+)")
_CHANNEL_ID_RE = re.compile(YOUTUBE_CHANNEL_ID_PATTERN)
_HANDLE_RE = re.compile(r"^[A-Za-z0-9._\-]{1,100}$")

CHANNEL_URL_HINT = (
    "Paste the channel's https://www.youtube.com/channel/UC... URL instead "
    "(the channel id starts with UC)."
)


class PodcastError(ValueError):
    """A lookup failed. `status_code` is the HTTP status the route returns
    (422 bad input, 429 throttled, 502 upstream trouble); MCP tools surface
    the same message as a tool error."""

    def __init__(self, status_code: int, message: str) -> None:
        self.status_code = status_code
        super().__init__(message)


class PodcastSearchResult(BaseModel):
    """One show from Apple's directory."""

    title: str = Field(description="Show title.")
    author: str | None = Field(default=None, description="Publisher or host, as Apple lists it.")
    feed_url: str = Field(description="The show's RSS feed URL.")
    artwork_url: str | None = Field(default=None, description="Cover art URL (display only).")
    apple_id: int | None = Field(default=None, description="Apple Podcasts collection id.")


class AppleEpisode(BaseModel):
    """One episode from an Apple show lookup."""

    track_id: str = Field(description="Apple track id (the ?i= in an episode link).")
    title: str = Field(description="Episode title.")
    guid: str | None = Field(default=None, description="The feed item's <guid>, when Apple has it.")
    audio_url: str | None = Field(default=None, description="The episode's audio URL.")


class AppleShowLookup(BaseModel):
    """A show's feed plus the newest episodes Apple lists for it."""

    source: RssSource = Field(description="The show as an RSS source.")
    episodes: list[AppleEpisode] = Field(description="Newest episodes, as Apple lists them.")


class ResolveRequest(BaseModel):
    """POST /podcasts/resolve request body."""

    url: str = Field(
        min_length=1,
        max_length=MAX_URL_CHARS,
        description="An RSS feed URL, an Apple Podcasts show URL, or a YouTube channel URL.",
    )


class OpmlRequest(BaseModel):
    """POST /podcasts/import-opml request body."""

    opml: str = Field(min_length=1, description="The OPML document as an XML string (max 1 MiB).")


class OpmlSkipped(BaseModel):
    """An OPML entry that did not become a Source."""

    line: str = Field(description="The offending outline, abbreviated.")
    reason: str = Field(description="Why it was skipped.")


class OpmlImport(BaseModel):
    """The result of parsing an OPML export."""

    sources: list[Source] = Field(description="One RSS source per feed outline, in file order.")
    skipped: list[OpmlSkipped] = Field(description="Entries that were not importable, with reasons.")


class InterviewRequest(BaseModel):
    """POST /souls/interview request body."""

    answers: dict[str, str] = Field(
        description=(
            "Interview answers keyed by identity, interests, triggers, ignore, style, "
            "guidance (the chorus-soul-bootstrap skill documents each)."
        )
    )

    @field_validator("answers")
    @classmethod
    def _known_keys(cls, value: dict[str, str]) -> dict[str, str]:
        kept = {
            k: v[:MAX_INTERVIEW_ANSWER_CHARS]
            for k, v in value.items()
            if k in SOUL_INTERVIEW_KEYS and v.strip()
        }
        if not kept:
            raise ValueError(
                f"answers must include at least one non-empty key of {', '.join(SOUL_INTERVIEW_KEYS)}"
            )
        return kept


class SoulResponse(BaseModel):
    """A soul document built from interview answers."""

    soul: str = Field(description="The soul.md markdown.")


# --- cache and throttle ---------------------------------------------------------


class _TTLCache:
    """Small thread-safe TTL cache with oldest-first eviction."""

    def __init__(self, ttl_s: float, max_entries: int, clock: Callable[[], float]) -> None:
        self._ttl_s = ttl_s
        self._max_entries = max_entries
        self._clock = clock
        self._items: OrderedDict[str, tuple[float, list[PodcastSearchResult]]] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: str) -> list[PodcastSearchResult] | None:
        with self._lock:
            entry = self._items.get(key)
            if entry is None:
                return None
            expires_at, value = entry
            if self._clock() >= expires_at:
                del self._items[key]
                return None
            return value

    def put(self, key: str, value: list[PodcastSearchResult]) -> None:
        with self._lock:
            self._items[key] = (self._clock() + self._ttl_s, value)
            self._items.move_to_end(key)
            while len(self._items) > self._max_entries:
                self._items.popitem(last=False)


class _CallThrottle:
    """Sliding-window cap on upstream calls (per process: on serverless each
    instance keeps its own budget, so this is a courtesy ceiling, not a
    global guarantee)."""

    def __init__(self, max_calls: int, window_s: float, clock: Callable[[], float]) -> None:
        self._max_calls = max_calls
        self._window_s = window_s
        self._clock = clock
        self._calls: deque[float] = deque()
        self._lock = threading.Lock()

    def allow(self) -> bool:
        with self._lock:
            now = self._clock()
            while self._calls and now - self._calls[0] >= self._window_s:
                self._calls.popleft()
            if len(self._calls) >= self._max_calls:
                return False
            self._calls.append(now)
            return True


def normalize_query(query: str) -> str:
    return " ".join(query.lower().split())


# --- OPML -------------------------------------------------------------------------


def _outline_line(outline: ET.Element) -> str:
    attrs = " ".join(f'{k}="{v}"' for k, v in outline.attrib.items())
    text = f"<outline {attrs}>" if attrs else "<outline>"
    return text if len(text) <= OPML_LINE_MAX_CHARS else text[: OPML_LINE_MAX_CHARS - 1] + "…"


def _valid_feed_url(url: str) -> bool:
    parts = urlsplit(url)
    return parts.scheme in ("http", "https") and bool(parts.hostname) and len(url) <= MAX_URL_CHARS


def parse_opml(opml: str) -> OpmlImport:
    """Parse an OPML subscription export. Every `<outline>` that carries an
    `xmlUrl` (and is typed rss/atom, or untyped) becomes an RssSource;
    folders are walked, anything else is reported in `skipped`."""
    if len(opml.encode("utf-8")) > OPML_MAX_BYTES:
        raise PodcastError(413, f"OPML exceeds the {OPML_MAX_BYTES} byte limit")
    if "<!ENTITY" in opml:
        raise PodcastError(422, "OPML must not declare XML entities")
    try:
        root = ET.fromstring(opml)
    except ET.ParseError as err:
        raise PodcastError(422, f"OPML is not well-formed XML: {err}") from err
    if root.tag.rsplit("}", 1)[-1].lower() != "opml":
        raise PodcastError(422, "not an OPML document (root element is not <opml>)")

    sources: list[Source] = []
    skipped: list[OpmlSkipped] = []
    seen_urls: set[str] = set()
    for outline in root.iter("outline"):
        feed_url = (outline.get("xmlUrl") or outline.get("xmlurl") or "").strip()
        if not feed_url:
            if len(outline) == 0:  # a leaf with no feed; a folder has children
                skipped.append(OpmlSkipped(line=_outline_line(outline), reason="no xmlUrl attribute"))
            continue
        kind = (outline.get("type") or "").strip().lower()
        if kind and kind not in OPML_FEED_TYPES:
            skipped.append(
                OpmlSkipped(line=_outline_line(outline), reason=f"unsupported outline type {kind!r}")
            )
            continue
        scheme, _, rest = feed_url.partition("://")
        if scheme.lower() in FEED_URL_SCHEMES:
            feed_url = f"{FEED_URL_SCHEMES[scheme.lower()]}://{rest}"
        if not _valid_feed_url(feed_url):
            skipped.append(
                OpmlSkipped(line=_outline_line(outline), reason="xmlUrl is not a valid http(s) URL")
            )
            continue
        if feed_url in seen_urls:
            skipped.append(OpmlSkipped(line=_outline_line(outline), reason="duplicate feed"))
            continue
        if len(sources) >= OPML_MAX_SOURCES:
            skipped.append(
                OpmlSkipped(
                    line=_outline_line(outline),
                    reason=f"over the {OPML_MAX_SOURCES}-feed import limit",
                )
            )
            continue
        seen_urls.add(feed_url)
        title = (outline.get("text") or outline.get("title") or "").strip() or None
        sources.append(
            RssSource(kind="rss", feed_url=feed_url, title=title[:MAX_TITLE_CHARS] if title else None)
        )
    return OpmlImport(sources=sources, skipped=skipped)


# --- directory --------------------------------------------------------------------


class PodcastDirectory:
    """Search and URL resolution. One instance per app, shared by the HTTP
    routes and the MCP tools so they share one cache and one throttle."""

    def __init__(
        self,
        *,
        resolver: Resolver | None = None,
        clock: Callable[[], float] = time.monotonic,
        cache_ttl_s: float = SEARCH_CACHE_TTL_S,
        max_calls_per_minute: int = UPSTREAM_MAX_CALLS_PER_MINUTE,
    ) -> None:
        self._resolver = resolver
        self._cache = _TTLCache(cache_ttl_s, SEARCH_CACHE_MAX_ENTRIES, clock)
        self._throttle = _CallThrottle(max_calls_per_minute, UPSTREAM_WINDOW_S, clock)

    # -- upstream JSON ---------------------------------------------------------

    def _get_json(self, url: str, *, what: str, headers: dict[str, str] | None = None) -> dict[str, object]:
        try:
            response = fetch_response(
                url,
                what=what,
                max_bytes=JSON_MAX_BYTES,
                resolver=self._resolver,
                headers=headers,
            )
        except FeedFetchError as err:
            raise PodcastError(502, f"{what} is unavailable: {err.reason}") from err
        if response.status_code != 200:
            raise PodcastError(502, f"{what} returned HTTP {response.status_code}")
        try:
            parsed = json.loads(response.content)
        except ValueError as err:
            raise PodcastError(502, f"{what} returned a malformed response") from err
        if not isinstance(parsed, dict):
            raise PodcastError(502, f"{what} returned an unexpected response")
        return parsed

    def _itunes(self, url: str) -> list[dict[str, object]]:
        if not self._throttle.allow():
            raise PodcastError(
                429, "too many directory lookups right now (Apple limits this API); retry shortly"
            )
        payload = self._get_json(url, what="Apple iTunes Search API")
        results = payload.get("results")
        if not isinstance(results, list):
            return []
        return [r for r in results if isinstance(r, dict)]

    @staticmethod
    def _as_result(item: dict[str, object]) -> PodcastSearchResult | None:
        feed_url = item.get("feedUrl")
        title = item.get("collectionName") or item.get("trackName")
        if not isinstance(feed_url, str) or not isinstance(title, str) or not _valid_feed_url(feed_url):
            return None
        author = item.get("artistName")
        artwork = item.get("artworkUrl600") or item.get("artworkUrl100")
        apple_id = item.get("collectionId") or item.get("trackId")
        return PodcastSearchResult(
            title=title[:MAX_TITLE_CHARS],
            author=author if isinstance(author, str) else None,
            feed_url=feed_url,
            artwork_url=artwork if isinstance(artwork, str) and len(artwork) <= MAX_URL_CHARS else None,
            apple_id=apple_id if isinstance(apple_id, int) else None,
        )

    # -- search ----------------------------------------------------------------

    def search(self, query: str, limit: int = SEARCH_DEFAULT_LIMIT) -> list[PodcastSearchResult]:
        normalized = normalize_query(query)
        if not normalized:
            raise PodcastError(422, "query must not be empty")
        limit = max(1, min(limit, SEARCH_MAX_LIMIT))
        cached = self._cache.get(normalized)
        if cached is None:
            params = urlencode(
                {
                    "media": "podcast",
                    "entity": "podcast",
                    "term": normalized,
                    "limit": SEARCH_UPSTREAM_LIMIT,
                }
            )
            items = self._itunes(f"{ITUNES_SEARCH_URL}?{params}")
            cached = [r for item in items if (r := self._as_result(item)) is not None]
            self._cache.put(normalized, cached)
        return cached[:limit]

    # -- resolve ---------------------------------------------------------------

    def resolve(self, url: str) -> Source:
        raw = url.strip()
        if not raw:
            raise PodcastError(422, "url must not be empty")
        scheme, _, rest = raw.partition("://")
        if scheme.lower() in FEED_URL_SCHEMES:  # feed:// itpc:// pcast:// links
            raw = f"{FEED_URL_SCHEMES[scheme.lower()]}://{rest}"
        parts = urlsplit(raw)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise PodcastError(
                422,
                "not a recognized URL: expected an RSS feed URL, an Apple Podcasts show URL "
                "(podcasts.apple.com/.../id<number>), or a YouTube channel URL",
            )
        host = parts.hostname.lower()
        if host in APPLE_HOSTS:
            return self._resolve_apple(parts.path)
        if host in YOUTUBE_HOSTS:
            return self._resolve_youtube(parts.path, parts.query)
        return self._resolve_feed(raw)

    def _resolve_apple(self, path: str) -> RssSource:
        match = _APPLE_ID_RE.search(path)
        if match is None:
            raise PodcastError(
                422,
                "that Apple URL is not a podcast show page; expected "
                "podcasts.apple.com/.../id<number>",
            )
        apple_id = match.group(1)
        items = self._itunes(f"{ITUNES_LOOKUP_URL}?{urlencode({'id': apple_id, 'entity': 'podcast'})}")
        for item in items:
            result = self._as_result(item)
            if result is not None:
                return RssSource(
                    kind="rss",
                    feed_url=result.feed_url,
                    title=result.title,
                    artwork_url=result.artwork_url,
                )
        raise PodcastError(
            422, f"Apple has no podcast with id {apple_id}, or it publishes no public feed URL"
        )

    def lookup_show_episodes(self, apple_id: str) -> AppleShowLookup:
        """The show's feed and its newest episodes in one throttled call."""
        query = urlencode(
            {"id": apple_id, "entity": "podcastEpisode", "limit": ITUNES_EPISODE_LOOKUP_LIMIT}
        )
        items = self._itunes(f"{ITUNES_LOOKUP_URL}?{query}")
        source: RssSource | None = None
        episodes: list[AppleEpisode] = []
        for item in items:
            if item.get("wrapperType") == "podcastEpisode":
                track_id, title = item.get("trackId"), item.get("trackName")
                if not isinstance(track_id, int) or not isinstance(title, str):
                    continue
                guid, audio = item.get("episodeGuid"), item.get("episodeUrl")
                episodes.append(
                    AppleEpisode(
                        track_id=str(track_id),
                        title=title[:MAX_TITLE_CHARS],
                        guid=guid if isinstance(guid, str) and guid else None,
                        audio_url=audio if isinstance(audio, str) and _valid_feed_url(audio) else None,
                    )
                )
            elif source is None and (result := self._as_result(item)) is not None:
                source = RssSource(
                    kind="rss",
                    feed_url=result.feed_url,
                    title=result.title,
                    artwork_url=result.artwork_url,
                )
        if source is None:
            raise PodcastError(
                422, f"Apple has no podcast with id {apple_id}, or it publishes no public feed URL"
            )
        return AppleShowLookup(source=source, episodes=episodes)

    def _resolve_feed(self, url: str) -> RssSource:
        try:
            safe_url(url, resolver=self._resolver)
        except UnsafeURLError as err:
            raise PodcastError(422, f"that URL cannot be fetched: {err}") from err
        try:
            root = fetch_xml(url, what="feed", resolver=self._resolver)
            parsed = parse_rss(root, url)
        except FeedFetchError as err:
            raise PodcastError(422, f"could not read that URL as an RSS feed: {err.reason}") from err
        artwork = parsed.artwork_url
        return RssSource(
            kind="rss",
            feed_url=url,
            title=parsed.title[:MAX_TITLE_CHARS] if parsed.title else None,
            artwork_url=artwork if artwork and len(artwork) <= MAX_URL_CHARS else None,
        )

    def _resolve_youtube(self, path: str, query: str) -> YoutubeSource:
        segments = [s for s in path.split("/") if s]
        channel_id: str | None = None
        title: str | None = None

        if len(segments) >= 2 and segments[0] == "channel":
            channel_id = segments[1]
        elif segments[:2] == ["feeds", "videos.xml"]:
            channel_id = (parse_qs(query).get("channel_id") or [""])[0]
        elif segments and segments[0].startswith("@"):
            channel_id, title = self._channel_via_api("forHandle", segments[0])
        elif len(segments) >= 2 and segments[0] == "c":
            channel_id, title = self._channel_via_api("forHandle", segments[1])
        elif len(segments) >= 2 and segments[0] == "user":
            channel_id, title = self._channel_via_api("forUsername", segments[1])
        else:
            raise PodcastError(
                422,
                "that is not a YouTube channel URL (video and playlist links cannot be "
                f"subscribed to). {CHANNEL_URL_HINT}",
            )

        if channel_id is None or not _CHANNEL_ID_RE.match(channel_id):
            raise PodcastError(422, f"not a valid YouTube channel id. {CHANNEL_URL_HINT}")
        return YoutubeSource(
            kind="youtube", channel_id=channel_id, title=title or self._youtube_title(channel_id)
        )

    def _youtube_title(self, channel_id: str) -> str | None:
        """The channel's name from its public Atom feed. Doubles as an
        existence check: a 404 means no such channel. Any other failure just
        leaves the title blank (the source is still valid)."""
        try:
            root = fetch_xml(youtube_feed_url(channel_id), what="youtube channel feed", resolver=self._resolver)
            title = parse_youtube_atom(root).title
        except FeedFetchError as err:
            if "HTTP 404" in err.reason:
                raise PodcastError(422, f"YouTube channel {channel_id} was not found") from err
            return None
        return title[:MAX_TITLE_CHARS] if title else None

    def _channel_via_api(self, parameter: str, name: str) -> tuple[str, str | None]:
        """Resolve a handle or legacy username with the official YouTube Data
        API (channels.list, 1 quota unit). Needs YOUTUBE_API_KEY."""
        key = os.environ.get(YOUTUBE_API_KEY_ENV)
        if not key:
            raise PodcastError(
                422,
                "this server cannot look up @handle, /c/ or /user/ channel URLs "
                f"(no {YOUTUBE_API_KEY_ENV} configured). {CHANNEL_URL_HINT}",
            )
        value = name.lstrip("@") if parameter == "forUsername" else name
        if not _HANDLE_RE.match(value.lstrip("@")):
            raise PodcastError(422, f"not a valid YouTube channel name. {CHANNEL_URL_HINT}")
        query = urlencode({"part": "snippet", parameter: value})
        payload = self._get_json(
            f"{YOUTUBE_CHANNELS_URL}?{query}",
            what="YouTube Data API",
            headers={YOUTUBE_API_KEY_HEADER: key},
        )
        items = payload.get("items")
        first = items[0] if isinstance(items, list) and items else None
        if not isinstance(first, dict) or not isinstance(first.get("id"), str):
            raise PodcastError(422, f"YouTube has no channel for {name!r}. {CHANNEL_URL_HINT}")
        snippet = first.get("snippet")
        title = snippet.get("title") if isinstance(snippet, dict) else None
        return str(first["id"]), title[:MAX_TITLE_CHARS] if isinstance(title, str) else None


def build_podcasts_router(directory: PodcastDirectory | None = None) -> APIRouter:
    router = APIRouter()
    podcasts = directory or PodcastDirectory()

    def _http(err: PodcastError) -> HTTPException:
        return HTTPException(status_code=err.status_code, detail=str(err))

    @router.get("/podcasts/search")
    def search_podcasts(
        q: str = Query(min_length=1, max_length=SEARCH_MAX_QUERY_CHARS),
        limit: int = Query(default=SEARCH_DEFAULT_LIMIT, ge=1, le=SEARCH_MAX_LIMIT),
    ) -> list[PodcastSearchResult]:
        try:
            return podcasts.search(q, limit)
        except PodcastError as err:
            raise _http(err) from err

    @router.post("/podcasts/resolve")
    def resolve_podcast(payload: ResolveRequest) -> Source:
        try:
            return podcasts.resolve(payload.url)
        except PodcastError as err:
            raise _http(err) from err

    @router.post("/podcasts/import-opml")
    def import_opml(payload: OpmlRequest) -> OpmlImport:
        try:
            return parse_opml(payload.opml)
        except PodcastError as err:
            raise _http(err) from err

    @router.post("/souls/interview")
    def soul_from_interview(payload: InterviewRequest) -> SoulResponse:
        try:
            return SoulResponse(soul=get_soul_builder().build_from_interview(payload.answers))
        except Exception as err:
            log.exception("souls/interview: soul builder failed")
            raise HTTPException(status_code=502, detail="the soul builder failed; try again") from err

    return router
