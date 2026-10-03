"""Resolve imported library items to feeds and digestible episodes.

Resolution order for one pending item, cheapest first:

1. The item already names a feed item (`feed_url` + `guid` or `audio_url`):
   no network at all.
2. It carries an Apple show id (from an Apple Podcasts link, the common case
   for Readwise Reader saves): one throttled iTunes lookup per show returns
   the feed URL and the newest ~200 episodes; the episode is matched on its
   Apple track id, which yields the feed `<guid>` and audio URL.
3. Otherwise (a Spotify save, a bare title): find the show by exact
   normalized title in Apple's directory, then match the episode by title in
   the show's own RSS feed.

Steps 2 and 3 fall back to a title match in the feed when Apple's listing
does not include the episode (older than its newest 200) or omits the guid.

Apple asks for about 20 directory calls a minute and PodcastDirectory
throttles below that, so a large first import resolves part of the library
and leaves the rest `pending` with a reason; importing again (or the next
import) continues where this one stopped. Lookups are cached per call, so
twenty saves from one show cost one lookup.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from chorus.feeds import FeedFetchError, ParsedFeed, fetch_xml, parse_rss
from chorus.library import SavedItem, normalize_title, titles_match
from chorus.models import MAX_SHOW_CHARS, MAX_TITLE_CHARS, EpisodeInput
from chorus.netguard import Resolver
from chorus.podcasts_api import AppleShowLookup, PodcastDirectory, PodcastError
from chorus.subscriptions import RssSource

log = logging.getLogger("chorus.library_resolve")

THROTTLED_STATUS = 429
THROTTLED_REASON = (
    "Apple's directory rate limit was reached during this import; import again in a "
    "minute to finish resolving"
)
NO_SHOW_REASON = "no podcast with a public RSS feed matches this show"
SHOW_SEARCH_LIMIT = 10


class _Throttled(Exception):
    pass


@dataclass
class _Session:
    """Per-resolve caches: one lookup per show, one fetch per feed."""

    shows: dict[str, AppleShowLookup | PodcastError] = field(default_factory=dict)
    searches: dict[str, RssSource | None] = field(default_factory=dict)
    feeds: dict[str, ParsedFeed | FeedFetchError] = field(default_factory=dict)
    throttled: bool = False


class LibraryResolver:
    def __init__(self, directory: PodcastDirectory, *, resolver: Resolver | None = None) -> None:
        self._directory = directory
        self._resolver = resolver

    def resolve(self, entries: Sequence[SavedItem]) -> list[SavedItem]:
        """Resolve every `pending` entry; others are returned unchanged."""
        session = _Session()
        return [self._resolve_one(entry, session) for entry in entries]

    # -- one item --------------------------------------------------------------

    def _resolve_one(self, entry: SavedItem, session: _Session) -> SavedItem:
        if entry.status != "pending":
            return entry
        item = entry.item
        show_source: RssSource | None = None
        try:
            if item.item_kind == "show":
                show_source = self._show_source(entry, session)
                if show_source is None:
                    return _unresolved(entry, NO_SHOW_REASON)
                return entry.model_copy(
                    update={"status": "resolved", "show_source": show_source, "reason": None}
                )

            episode: EpisodeInput | None = None
            if item.feed_url and (item.guid or item.audio_url):
                show_source = RssSource(kind="rss", feed_url=item.feed_url, title=item.show_title)
                episode = EpisodeInput(
                    feed_url=item.feed_url,
                    guid=item.guid,
                    audio_url=item.audio_url,
                    show=_show_name(item.show_title, show_source),
                    title=item.title,
                )
            else:
                show_source = self._show_source(entry, session)
                if show_source is None:
                    return _unresolved(entry, NO_SHOW_REASON)
                episode = self._apple_episode(entry, show_source, session) or self._feed_episode(
                    entry, show_source, session
                )
            if episode is None:
                return _unresolved(
                    entry,
                    f"episode not found in the {show_source.title or show_source.feed_url} feed",
                    show_source,
                )
            return entry.model_copy(
                update={
                    "status": "resolved",
                    "episode": episode,
                    "show_source": show_source,
                    "reason": None,
                }
            )
        except _Throttled:
            return entry.model_copy(update={"reason": THROTTLED_REASON, "show_source": show_source})
        except PodcastError as err:
            return _unresolved(entry, str(err), show_source)
        except FeedFetchError as err:
            return _unresolved(entry, f"could not read the show's feed: {err.reason}", show_source)

    # -- show ------------------------------------------------------------------

    def _show_source(self, entry: SavedItem, session: _Session) -> RssSource | None:
        item = entry.item
        if item.feed_url:
            return RssSource(
                kind="rss",
                feed_url=item.feed_url,
                title=item.title if item.item_kind == "show" else item.show_title,
            )
        if item.apple_show_id:
            return self._apple_show(item.apple_show_id, session).source
        name = item.title if item.item_kind == "show" else item.show_title
        return self._search_show(name, item.author, session) if name else None

    def _apple_show(self, apple_id: str, session: _Session) -> AppleShowLookup:
        cached = session.shows.get(apple_id)
        if cached is None:
            cached = self._directory_call(
                lambda: self._directory.lookup_show_episodes(apple_id), session
            )
            session.shows[apple_id] = cached
        if isinstance(cached, PodcastError):
            raise cached
        return cached

    def _search_show(self, name: str, author: str | None, session: _Session) -> RssSource | None:
        """Exact normalized-title match only: a near miss would subscribe the
        principal to the wrong show, so no match is better than a guess."""
        wanted = normalize_title(name)
        if wanted in session.searches:
            return session.searches[wanted]
        results = self._directory_call(
            lambda: self._directory.search(name, SHOW_SEARCH_LIMIT), session
        )
        if isinstance(results, PodcastError):
            raise results
        matches = [r for r in results if normalize_title(r.title) == wanted]
        if author and len(matches) > 1:
            by_author = [
                r
                for r in matches
                if r.author and normalize_title(r.author) == normalize_title(author)
            ]
            matches = by_author or matches
        found = (
            RssSource(
                kind="rss",
                feed_url=matches[0].feed_url,
                title=matches[0].title,
                artwork_url=matches[0].artwork_url,
            )
            if matches
            else None
        )
        session.searches[wanted] = found
        return found

    def _directory_call[T](self, call: Callable[[], T], session: _Session) -> T | PodcastError:
        if session.throttled:
            raise _Throttled
        try:
            return call()
        except PodcastError as err:
            if err.status_code == THROTTLED_STATUS:
                session.throttled = True
                raise _Throttled from err
            return err

    # -- episode ---------------------------------------------------------------

    def _apple_episode(
        self, entry: SavedItem, show: RssSource, session: _Session
    ) -> EpisodeInput | None:
        item = entry.item
        if not (item.apple_show_id and item.apple_episode_id):
            return None
        lookup = self._apple_show(item.apple_show_id, session)
        for episode in lookup.episodes:
            if episode.track_id == item.apple_episode_id and (episode.guid or episode.audio_url):
                return EpisodeInput(
                    feed_url=show.feed_url,
                    guid=episode.guid,
                    audio_url=episode.audio_url,
                    show=_show_name(item.show_title, show),
                    title=item.title[:MAX_TITLE_CHARS],
                )
        return None

    def _feed_episode(
        self, entry: SavedItem, show: RssSource, session: _Session
    ) -> EpisodeInput | None:
        parsed = session.feeds.get(show.feed_url)
        if parsed is None:
            try:
                root = fetch_xml(show.feed_url, what="rss feed", resolver=self._resolver)
                parsed = parse_rss(root, show.feed_url, title_override=show.title)
            except FeedFetchError as err:
                parsed = err
            session.feeds[show.feed_url] = parsed
        if isinstance(parsed, FeedFetchError):
            raise parsed
        for listed in parsed.episodes:
            if titles_match(listed.title, entry.item.title):
                return listed.episode.model_copy(
                    update={"show": _show_name(entry.item.show_title, show)}
                )
        return None


def _show_name(show_title: str | None, show: RssSource) -> str | None:
    name = show.title or show_title
    return name[:MAX_SHOW_CHARS] if name else None


def _unresolved(entry: SavedItem, reason: str, show_source: RssSource | None = None) -> SavedItem:
    return entry.model_copy(
        update={"status": "unresolved", "reason": reason, "show_source": show_source}
    )
