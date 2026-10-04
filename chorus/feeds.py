"""Feed listing: turn a subscription Source into its recent episodes.

`list_recent_episodes(source, since, limit)` is the one seam the scheduler
(`chorus.scheduler.run_subscription`) and the preview endpoint
(`POST /subscriptions/preview`) share, so a preview shows exactly what the
next run would pick up.

Four source kinds:

- `rss`: fetch the feed through `chorus.transcripts._fetch_bounded` (SSRF
  guard on every hop, byte cap, redirect cap), parse `<item>` elements, read
  `pubDate` (RFC 2822, ISO 8601 as a fallback), `guid` and the `enclosure`
  URL. Episodes are identified by `{feed_url, guid}`, or `{feed_url,
  audio_url}` when the item has no guid; the enclosure URL rides along when
  both exist so the digest email can link to the audio and the Deepgram
  fallback has something to transcribe. The item scan is capped at
  `MAX_FEED_ITEMS`.
- `youtube`: the channel's public Atom feed,
  `https://www.youtube.com/feeds/videos.xml?channel_id=<UC...>`. YouTube
  publishes this feed for syndication (its Data API push-notification guide
  uses it as the topic URL, and every channel page advertises it for feed
  readers), so reading it is the sanctioned way to learn a channel's newest
  upload ids. We read only metadata from it (video id, title, published
  time). We never scrape channel or watch-page HTML, and nothing here fetches
  captions: transcript acquisition stays behind the provider chain in
  `chorus.transcripts`, which is where the Terms-of-Service question lives
  (docs: reports/Podcast transcript sources.md).
- `show`: the static demo catalog (`chorus.catalog`). Catalog fixtures carry
  no publish date, so they are stamped with `since`; dedupe against
  `seen_episode_ids` is what keeps them from repeating.
- `saved`: the owner's imported saved-episode queue (chorus.library). It is
  per-owner state, not a URL, so `list_recent_episodes` cannot read it;
  `gather_episodes` takes a `saved` lister bound to the owner
  (chorus.saved_items.saved_queue_lister) and routes these sources to it.
  The queue ignores `since` (a save is unheard until it is digested) and
  takes the run's seen ids instead, so already-digested saves never crowd
  out older unheard ones.

Errors are per source. `list_recent_episodes` raises `FeedFetchError`;
`gather_episodes` catches it per source and returns it alongside whatever the
other sources produced, so one dead feed never fails a run.
"""
from __future__ import annotations

import email.utils
import html
import logging
import re
import xml.etree.ElementTree as ET
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from chorus import catalog
from chorus.models import (
    MAX_DESCRIPTION_CHARS,
    MAX_GUID_CHARS,
    MAX_SHOW_CHARS,
    MAX_TITLE_CHARS,
    MAX_URL_CHARS,
    EpisodeInput,
)
from chorus.netguard import Resolver
from chorus.subscriptions import (
    PersonaSource,
    RssSource,
    SavedQueueSource,
    ShowSource,
    Source,
    YoutubeSource,
)
from chorus.transcripts import (
    FEED_PREFIX_BYTES,
    MAX_FEED_BYTES,
    MAX_FEED_FULL_BYTES,
    MAX_FEED_ITEMS,
    RSS_TIMEOUT_S,
    TranscriptProviderError,
    _BoundedResponse,
    _fetch_bounded,
    parse_feed_document,
)

log = logging.getLogger("chorus.feeds")

YOUTUBE_FEED_URL = "https://www.youtube.com/feeds/videos.xml"
ITUNES_NS = "http://www.itunes.com/dtds/podcast-1.0.dtd"
ATOM_NS = "http://www.w3.org/2005/Atom"
YOUTUBE_NS = "http://www.youtube.com/xml/schemas/2015"
DC_NS = "http://purl.org/dc/elements/1.1/"
MEDIA_NS = "http://search.yahoo.com/mrss/"
UNTITLED_EPISODE = "(untitled episode)"
# Episodes listed per source by the scheduler and the preview before the
# round-robin cap is applied; comfortably above MAX_EPISODES_PER_RUN (20).
PREVIEW_LIST_LIMIT = 40
# Feeds are fetched concurrently: 50 sources at RSS_TIMEOUT_S each would
# otherwise outlast a serverless function.
FEED_FETCH_WORKERS = 8
# Entity declarations are never legitimate in a podcast feed; rejecting them
# closes the entity-expansion class of attacks for good measure on top of the
# byte cap.
_ENTITY_DECL = b"<!ENTITY"


class FeedFetchError(Exception):
    """One source could not be listed. `reason` is safe to show a user."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class FeedEpisode(BaseModel):
    """One listed episode and the identity the digest pipeline will fetch."""

    source_title: str = Field(description="Display title of the source this episode came from.")
    title: str = Field(description="Episode title.")
    published_at: datetime = Field(description="Publication time, timezone-aware UTC.")
    episode: EpisodeInput = Field(description="The episode in the shape POST /digest accepts.")
    via_persona: str | None = Field(
        default=None, description="The persona whose published digest surfaced it, if any."
    )


class SourceError(BaseModel):
    """A source that could not be read, and why."""

    source: Source = Field(description="The source that failed.")
    reason: str = Field(description="Human-readable failure reason.")


class ParsedFeed(BaseModel):
    """A fetched podcast feed: channel metadata plus its dated episodes."""

    title: str | None = Field(default=None, description="Channel title, if present.")
    artwork_url: str | None = Field(default=None, description="Channel artwork URL, if present.")
    episodes: list[FeedEpisode] = Field(
        default_factory=list, description="Dated episodes, newest first."
    )


FeedLister = Callable[..., list[FeedEpisode]]
# (source, limit, exclude_ids) -> the owner's saved queue; see the module docstring.
SavedQueueLister = Callable[[SavedQueueSource, int, frozenset[str]], list[FeedEpisode]]
SAVED_QUEUE_UNAVAILABLE = "the saved-episode queue is not available here"


class GatherResult(BaseModel):
    """Per-source episode lists (aligned with the input sources) plus errors."""

    per_source: list[list[FeedEpisode]] = Field(
        description="Episodes per source, in the order sources were given; empty on error."
    )
    errors: list[SourceError] = Field(description="Sources that failed to list.")


def list_persona_episodes(
    source: PersonaSource,
    since: datetime,
    limit: int,
    *,
    personas: Any | None = None,
    publications: Any | None = None,
) -> list[FeedEpisode]:
    """The original episodes a public persona published since `since`
    (chorus/publications.py). Stores default to the deployment's own."""
    from chorus import config_env
    from chorus.publications import persona_feed_episodes

    owns = personas is None, publications is None
    registry = personas or config_env.select_persona_registry()
    store = publications or config_env.select_publication_store()
    try:
        persona = registry.get(source.persona_id)
        if persona is None or not persona.public:
            raise FeedFetchError(f"persona {source.persona_id} is unknown or not public")
        return persona_feed_episodes(persona, store.list(source.persona_id), since, limit)
    finally:
        if owns[0]:
            getattr(registry, "close", lambda: None)()
        if owns[1]:
            store.close()


def source_label(source: Source) -> str:
    """A short human label for logs, errors and the "sources checked" list."""
    if isinstance(source, RssSource):
        return source.title or source.feed_url
    if isinstance(source, YoutubeSource):
        return source.title or f"YouTube channel {source.channel_id}"
    if isinstance(source, SavedQueueSource):
        return source.title
    if isinstance(source, PersonaSource):
        return source.title or f"persona {source.persona_id}"
    return source.show


# --- XML helpers --------------------------------------------------------------


def _local_ns(tag: str) -> str:
    return tag[1:].split("}", 1)[0] if tag.startswith("{") else ""


def _text(element: ET.Element | None) -> str | None:
    if element is None or element.text is None:
        return None
    value = element.text.strip()
    return value or None


def _child(parent: ET.Element, name: str, default_ns: str = "") -> ET.Element | None:
    """First direct child named `name`, un-namespaced or in the document's
    own default namespace (some feeds declare `xmlns=` on the root, which
    prefixes every plain tag). Never matches a differently-namespaced element
    that merely shares the local name, e.g. `itunes:title`."""
    found = parent.find(name)
    if found is None and default_ns:
        found = parent.find(f"{{{default_ns}}}{name}")
    return found


def _iter_named(root: ET.Element, name: str, default_ns: str) -> list[ET.Element]:
    tags = {name, f"{{{default_ns}}}{name}"} if default_ns else {name}
    return [element for element in root.iter() if element.tag in tags]


def parse_xml(content: bytes, *, what: str, truncated: bool = False) -> ET.Element:
    """Parse a whole document, or (truncated=True) the prefix of an RSS feed
    keeping only the items that arrived complete."""
    if _ENTITY_DECL in content:
        raise FeedFetchError(f"{what}: XML entity declarations are not allowed")
    try:
        return parse_feed_document(content, truncated=truncated)
    except ET.ParseError as err:
        raise FeedFetchError(f"{what} is not well-formed XML: {err}") from err


def fetch_response(
    url: str,
    *,
    what: str,
    max_bytes: int = MAX_FEED_BYTES,
    resolver: Resolver | None = None,
    headers: dict[str, str] | None = None,
    truncate_ok: bool = False,
) -> _BoundedResponse:
    """GET `url` behind the SSRF guard, redirect cap and byte cap. The one
    outbound-HTTP seam of this module and of chorus.podcasts_api, so tests
    fake `chorus.feeds._fetch_bounded` and nothing touches the network.
    Returns non-2xx responses to the caller; raises FeedFetchError on SSRF
    refusal, timeout, transport failure or an oversized body."""
    extra: dict[str, object] = {"headers": headers} if headers else {}
    try:
        return _fetch_bounded(
            "GET",
            url,
            timeout_s=RSS_TIMEOUT_S,
            what=what,
            max_bytes=max_bytes,
            resolver=resolver,
            truncate_ok=truncate_ok,
            **extra,
        )
    except TranscriptProviderError as err:
        raise FeedFetchError(str(err)) from err


def fetch_xml(url: str, *, what: str, resolver: Resolver | None = None) -> ET.Element:
    """Fetch and parse an XML document behind the SSRF guard and byte cap."""
    root, _ = fetch_feed_root(url, what=what, resolver=resolver, full=True)
    return root


def fetch_feed_root(
    url: str, *, what: str, resolver: Resolver | None = None, full: bool = False
) -> tuple[ET.Element, bool]:
    """(root, truncated). Default: the first FEED_PREFIX_BYTES of the feed,
    parsed to channel metadata plus complete items. `full=True` reads the
    whole document up to MAX_FEED_FULL_BYTES."""
    response = fetch_response(
        url,
        what=what,
        resolver=resolver,
        max_bytes=MAX_FEED_FULL_BYTES if full else FEED_PREFIX_BYTES,
        truncate_ok=not full,
    )
    if response.status_code != 200:
        raise FeedFetchError(f"{what}: HTTP {response.status_code} at {url}")
    truncated = getattr(response, "truncated", False)
    return parse_xml(response.content, what=what, truncated=truncated), truncated


def _feed_order_ascending(root: ET.Element) -> bool:
    """True when the feed lists episodes oldest-first (its first item is
    older than its last), so a prefix holds the oldest episodes, not the newest."""
    default_ns = _local_ns(root.tag)
    channel = _child(root, "channel", default_ns)
    if channel is None:
        return False
    items = _iter_named(channel, "item", default_ns)
    if len(items) < 2:
        return False
    first = parse_datetime(_text(_child(items[0], "pubDate", default_ns)))
    last = parse_datetime(_text(_child(items[-1], "pubDate", default_ns)))
    return first is not None and last is not None and first < last


def parse_datetime(text: str | None) -> datetime | None:
    """RFC 2822 (RSS `pubDate`) first, ISO 8601 (Atom, `dc:date`) second.
    Returns timezone-aware UTC, treating a zone-less stamp as UTC."""
    if not text:
        return None
    value = text.strip()
    parsed: datetime | None
    try:
        parsed = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        parsed = None
    if parsed is None:
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


_HTML_TAG_RE = re.compile(r"<[^>]+>")


def plain_description(raw: str | None) -> str | None:
    """Show notes as plain text: tags stripped, entities decoded, whitespace
    collapsed, capped at MAX_DESCRIPTION_CHARS. None when nothing is left."""
    if not raw:
        return None
    text = " ".join(html.unescape(_HTML_TAG_RE.sub(" ", raw)).split())
    return text[:MAX_DESCRIPTION_CHARS] or None


# --- RSS ------------------------------------------------------------------------


def _channel_artwork(channel: ET.Element, default_ns: str) -> str | None:
    itunes_image = channel.find(f"{{{ITUNES_NS}}}image")
    if itunes_image is not None and itunes_image.get("href"):
        return itunes_image.get("href")
    image = _child(channel, "image", default_ns)
    if image is not None:
        return _text(_child(image, "url", default_ns))
    return None


def _rss_item_to_episode(
    item: ET.Element, feed_url: str, source_title: str, default_ns: str
) -> FeedEpisode | None:
    published = parse_datetime(_text(_child(item, "pubDate", default_ns))) or parse_datetime(
        _text(item.find(f"{{{DC_NS}}}date"))
    )
    if published is None:
        return None

    guid = _text(_child(item, "guid", default_ns))
    enclosure = _child(item, "enclosure", default_ns)
    audio_url = enclosure.get("url") if enclosure is not None else None
    if audio_url is not None:
        audio_url = audio_url.strip() or None
    if audio_url and len(audio_url) > MAX_URL_CHARS:
        audio_url = None
    if guid and len(guid) > MAX_GUID_CHARS:
        guid = None  # fall back to the enclosure URL as the identity
    title = (_text(_child(item, "title", default_ns)) or UNTITLED_EPISODE)[:MAX_TITLE_CHARS]
    show = source_title[:MAX_SHOW_CHARS]
    description = plain_description(
        _text(_child(item, "description", default_ns)) or _text(item.find(f"{{{ITUNES_NS}}}summary"))
    )
    if not (guid or audio_url):
        return None  # nothing identifies this item

    try:
        episode = EpisodeInput(
            feed_url=feed_url,
            guid=guid or None,
            audio_url=audio_url,
            show=show,
            title=title,
            published_at=published,
            description=description,
        )
    except ValidationError:
        return None
    return FeedEpisode(
        source_title=source_title, title=title, published_at=published, episode=episode
    )


def parse_rss(root: ET.Element, feed_url: str, *, title_override: str | None = None) -> ParsedFeed:
    """Parse an RSS 2.0 document. Raises FeedFetchError when `root` is not RSS."""
    default_ns = _local_ns(root.tag)
    local_root = root.tag.rsplit("}", 1)[-1]
    if local_root != "rss":
        raise FeedFetchError(f"not an RSS feed (root element is <{local_root}>)")
    channel = _child(root, "channel", default_ns)
    if channel is None:
        raise FeedFetchError("not an RSS feed (no <channel> element)")

    channel_title = _text(_child(channel, "title", default_ns))
    source_title = title_override or channel_title or feed_url
    items = _iter_named(channel, "item", default_ns)[:MAX_FEED_ITEMS]
    episodes = [
        e
        for item in items
        if (e := _rss_item_to_episode(item, feed_url, source_title, default_ns)) is not None
    ]
    episodes.sort(key=lambda e: e.published_at, reverse=True)
    return ParsedFeed(
        title=channel_title, artwork_url=_channel_artwork(channel, default_ns), episodes=episodes
    )


# --- YouTube Atom -----------------------------------------------------------------


def youtube_feed_url(channel_id: str) -> str:
    return f"{YOUTUBE_FEED_URL}?channel_id={channel_id}"


def parse_youtube_atom(root: ET.Element, *, title_override: str | None = None) -> ParsedFeed:
    """Parse a YouTube channel Atom feed (`<yt:videoId>`, `<published>`)."""
    local_root = root.tag.rsplit("}", 1)[-1]
    if local_root != "feed":
        raise FeedFetchError(f"not a YouTube channel feed (root element is <{local_root}>)")
    channel_title = _text(root.find(f"{{{ATOM_NS}}}title"))
    source_title = title_override or channel_title or "YouTube"

    episodes: list[FeedEpisode] = []
    for entry in root.findall(f"{{{ATOM_NS}}}entry")[:MAX_FEED_ITEMS]:
        video_id = _text(entry.find(f"{{{YOUTUBE_NS}}}videoId"))
        if video_id is None:
            entry_id = _text(entry.find(f"{{{ATOM_NS}}}id")) or ""
            video_id = entry_id.removeprefix("yt:video:") or None
        published = parse_datetime(_text(entry.find(f"{{{ATOM_NS}}}published")))
        if video_id is None or published is None:
            continue
        title = (_text(entry.find(f"{{{ATOM_NS}}}title")) or UNTITLED_EPISODE)[:MAX_TITLE_CHARS]
        description = plain_description(
            _text(entry.find(f"{{{MEDIA_NS}}}group/{{{MEDIA_NS}}}description"))
        )
        try:
            episode = EpisodeInput(
                video_id=video_id,
                show=source_title[:MAX_SHOW_CHARS],
                title=title,
                published_at=published,
                description=description,
            )
        except ValidationError:
            continue
        episodes.append(
            FeedEpisode(
                source_title=source_title, title=title, published_at=published, episode=episode
            )
        )
    episodes.sort(key=lambda e: e.published_at, reverse=True)
    return ParsedFeed(title=channel_title, episodes=episodes)


# --- Public listing -----------------------------------------------------------------

_CHANNEL_NOT_FOUND = re.compile(r"HTTP 404")


def list_recent_episodes(
    source: Source,
    since: datetime,
    limit: int,
    *,
    resolver: Resolver | None = None,
) -> list[FeedEpisode]:
    """Episodes of `source` published at or after `since`, newest first, at
    most `limit`. Raises FeedFetchError when the source cannot be read."""
    if since.tzinfo is None:
        raise ValueError("list_recent_episodes: `since` must be timezone-aware")

    if isinstance(source, RssSource):
        root, truncated = fetch_feed_root(source.feed_url, what="rss feed", resolver=resolver)
        parsed = parse_rss(root, source.feed_url, title_override=source.title)
        episodes = parsed.episodes
        if truncated:
            in_window = [e for e in episodes if e.published_at >= since]
            window_may_continue = (
                bool(episodes)
                and len(in_window) < limit
                and min(e.published_at for e in episodes) >= since
            )
            if not episodes or window_may_continue or _feed_order_ascending(root):
                root, _ = fetch_feed_root(
                    source.feed_url, what="rss feed", resolver=resolver, full=True
                )
                episodes = parse_rss(root, source.feed_url, title_override=source.title).episodes
    elif isinstance(source, YoutubeSource):
        try:
            root = fetch_xml(
                youtube_feed_url(source.channel_id), what="youtube channel feed", resolver=resolver
            )
        except FeedFetchError as err:
            if _CHANNEL_NOT_FOUND.search(err.reason):
                raise FeedFetchError(
                    f"YouTube channel {source.channel_id} was not found (HTTP 404)"
                ) from err
            raise
        episodes = parse_youtube_atom(root, title_override=source.title).episodes
    elif isinstance(source, SavedQueueSource):
        raise FeedFetchError(SAVED_QUEUE_UNAVAILABLE)
    elif isinstance(source, PersonaSource):
        return list_persona_episodes(source, since, limit)
    else:
        assert isinstance(source, ShowSource)
        resolved = catalog.resolve(shows=[source.show])
        if not resolved:
            raise FeedFetchError(
                f"unknown catalog show {source.show!r}; GET /shows lists the available shows"
            )
        return [
            FeedEpisode(
                source_title=source.show,
                title=ep.title or ep.resolved_id(),
                published_at=since.astimezone(UTC),
                episode=ep,
            )
            for ep in resolved
        ][:limit]

    return [e for e in episodes if e.published_at >= since][:limit]


def gather_episodes(
    sources: Sequence[Source],
    since: datetime,
    limit: int,
    *,
    resolver: Resolver | None = None,
    lister: FeedLister | None = None,
    saved: SavedQueueLister | None = None,
    exclude_ids: frozenset[str] = frozenset(),
) -> GatherResult:
    """List every source concurrently. A failing source contributes an entry
    in `errors` and an empty list; it never raises. `saved` reads
    SavedQueueSource entries (with `exclude_ids`); without it they fail as
    unavailable."""
    list_one = lister or list_recent_episodes

    def run(source: Source) -> tuple[list[FeedEpisode], SourceError | None]:
        try:
            if isinstance(source, SavedQueueSource):
                if saved is None:
                    raise FeedFetchError(SAVED_QUEUE_UNAVAILABLE)
                return saved(source, limit, exclude_ids), None
            return list_one(source, since, limit, resolver=resolver), None
        except FeedFetchError as err:
            return [], SourceError(source=source, reason=err.reason)
        except Exception as err:  # noqa: BLE001 - one source must never fail the run
            log.exception("feeds: unexpected failure listing %s", source_label(source))
            return [], SourceError(source=source, reason=f"{type(err).__name__}: {err}")

    if not sources:
        return GatherResult(per_source=[], errors=[])
    workers = max(1, min(FEED_FETCH_WORKERS, len(sources)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(run, sources))
    return GatherResult(
        per_source=[episodes for episodes, _ in results],
        errors=[error for _, error in results if error is not None],
    )


def round_robin(per_source: Sequence[Sequence[FeedEpisode]], cap: int) -> list[FeedEpisode]:
    """Pick up to `cap` episodes fairly: each round takes every source's
    newest remaining episode (most recently published first), so one
    prolific show cannot crowd the others out. Duplicate episode ids (the
    same episode reached through two sources) count once. The result is
    sorted newest first."""
    queues = [sorted(episodes, key=lambda e: e.published_at, reverse=True) for episodes in per_source]
    picked: list[FeedEpisode] = []
    picked_ids: set[str] = set()
    while len(picked) < cap and any(queues):
        heads: list[FeedEpisode] = []
        for queue in queues:
            while queue and queue[0].episode.resolved_id() in picked_ids:
                queue.pop(0)
            if queue:
                heads.append(queue.pop(0))
        heads.sort(key=lambda e: e.published_at, reverse=True)
        for head in heads:
            if len(picked) >= cap:
                break
            head_id = head.episode.resolved_id()
            if head_id in picked_ids:
                continue
            picked_ids.add(head_id)
            picked.append(head)
    picked.sort(key=lambda e: e.published_at, reverse=True)
    return picked


class SubscriptionPreview(BaseModel):
    """What the next run of a set of sources would pick up."""

    episodes: list[FeedEpisode] = Field(
        description="Episodes the next run would digest, newest first."
    )
    errors: list[SourceError] = Field(description="Sources that could not be read, and why.")


def preview_sources(
    sources: Sequence[Source],
    *,
    lookback_days: int,
    max_episodes_per_run: int,
    now: datetime | None = None,
    resolver: Resolver | None = None,
    lister: FeedLister | None = None,
    saved: SavedQueueLister | None = None,
) -> SubscriptionPreview:
    """Exactly the selection a first run would make (same listing window,
    same round-robin cap), minus the seen filter since nothing is saved yet."""
    since = (now or datetime.now(UTC)) - timedelta(days=lookback_days)
    gathered = gather_episodes(
        sources, since, PREVIEW_LIST_LIMIT, resolver=resolver, lister=lister, saved=saved
    )
    picked = round_robin(gathered.per_source, max_episodes_per_run)
    return SubscriptionPreview(episodes=picked, errors=gathered.errors)
