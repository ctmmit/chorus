"""Resolve imported library items to subscription sources and digestible
episodes, with no credentials for any provider.

Resolution for one pending item, cheapest first:

1. The item names its target already: `feed_url` + `guid`/`audio_url` (an
   RSS episode), a YouTube video id (the video is the episode), or a YouTube
   channel id (a followed channel). No lookup, except YouTube's public oEmbed
   for a missing title.
2. It carries an Apple show id (an Apple Podcasts link, shared directly or
   saved through a read-later app): one throttled iTunes lookup per show
   returns the feed URL and the newest ~200 episodes, matched on the Apple
   track id, which yields the feed `<guid>` and audio URL.
3. A Spotify link carries only Spotify ids. Spotify's public oEmbed endpoint
   gives the episode (or show) title without an API key; the title then goes
   through step 4.
4. A title with no feed: when the show is known, find it by exact title in
   Apple's directory and match the episode in its RSS feed; otherwise search
   Apple's episode index by exact episode title.

Steps 2 and 4 fall back to a title match in the show's own feed when Apple's
listing lacks the episode or its guid. A Spotify exclusive has no public feed
anywhere, so it ends `unresolved` with that reason rather than a wrong guess.

Apple asks for about 20 directory calls a minute and PodcastDirectory
throttles below that, so a large import resolves part of the library and
leaves the rest `pending`; importing or sharing again continues. Lookups are
cached per call, so twenty saves from one show cost one lookup.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from urllib.parse import urlencode

from chorus.feeds import FeedFetchError, ParsedFeed, fetch_response, fetch_xml, parse_rss
from chorus.library import LibraryItem, SavedItem, ShowLink, normalize_title, titles_match
from chorus.models import MAX_SHOW_CHARS, MAX_TITLE_CHARS, EpisodeInput
from chorus.netguard import Resolver
from chorus.podcasts_api import AppleShowLookup, PodcastDirectory, PodcastError
from chorus.subscriptions import RssSource, YoutubeSource

log = logging.getLogger("chorus.library_resolve")

THROTTLED_STATUS = 429
THROTTLED_REASON = (
    "Apple's directory rate limit was reached during this import; import again in a "
    "minute to finish resolving"
)
NO_SHOW_REASON = "no podcast with a public RSS feed matches this show"
NO_EPISODE_REASON = (
    "no public podcast feed has an episode with this title (a Spotify or YouTube "
    "exclusive has none)"
)
AMBIGUOUS_REASON = "several shows have an episode with this title; share the Apple link instead"
NO_TITLE_REASON = "the link's title could not be read"
SHOW_SEARCH_LIMIT = 10
EPISODE_SEARCH_LIMIT = 25

SPOTIFY_OEMBED_URL = "https://open.spotify.com/oembed"
YOUTUBE_OEMBED_URL = "https://www.youtube.com/oembed"
OEMBED_MAX_BYTES = 256 * 1024


class _Throttled(Exception):
    pass


class _Unresolvable(Exception):
    def __init__(self, reason: str, show: ShowLink | None = None) -> None:
        self.reason = reason
        self.show = show
        super().__init__(reason)


@dataclass
class _Session:
    """Per-resolve caches: one lookup per show, one fetch per feed."""

    shows: dict[str, AppleShowLookup | PodcastError] = field(default_factory=dict)
    searches: dict[str, RssSource | None] = field(default_factory=dict)
    feeds: dict[str, ParsedFeed | FeedFetchError] = field(default_factory=dict)
    throttled: bool = False


@dataclass
class _Outcome:
    item: LibraryItem
    show: ShowLink | None = None
    episode: EpisodeInput | None = None


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
        outcome = _Outcome(item=entry.item)
        try:
            if entry.item.item_kind == "show":
                self._resolve_show(outcome, session)
            else:
                self._resolve_episode(outcome, session)
        except _Throttled:
            return entry.model_copy(
                update={
                    "item": outcome.item,
                    "reason": THROTTLED_REASON,
                    "show_source": outcome.show,
                }
            )
        except _Unresolvable as err:
            return _unresolved(entry, outcome, err.reason, err.show or outcome.show)
        except PodcastError as err:
            return _unresolved(entry, outcome, str(err), outcome.show)
        except FeedFetchError as err:
            return _unresolved(
                entry, outcome, f"could not read the show's feed: {err.reason}", outcome.show
            )
        return entry.model_copy(
            update={
                "item": outcome.item,
                "status": "resolved",
                "episode": outcome.episode,
                "show_source": outcome.show,
                "reason": None,
            }
        )

    def _resolve_show(self, outcome: _Outcome, session: _Session) -> None:
        item = outcome.item
        if item.youtube_channel_id:
            outcome.show = YoutubeSource(
                kind="youtube", channel_id=item.youtube_channel_id, title=item.title or None
            )
            return
        if item.spotify_id and not item.title:
            outcome.item = item = _with(item, title=self._oembed_title(SPOTIFY_OEMBED_URL, item))
        outcome.show = self._show_source(item, session)
        if outcome.show is None:
            raise _Unresolvable(NO_SHOW_REASON)
        if not item.title and outcome.show.title:
            outcome.item = _with(item, title=outcome.show.title)

    def _resolve_episode(self, outcome: _Outcome, session: _Session) -> None:
        item = outcome.item
        if item.youtube_video_id:
            if not item.title:
                title, channel = self._youtube_oembed(item)
                outcome.item = item = _with(
                    item, title=title, show_title=item.show_title or channel
                )
            outcome.episode = EpisodeInput(
                video_id=item.youtube_video_id,
                title=item.title[:MAX_TITLE_CHARS] or None,
                show=_clip_show(item.show_title),
            )
            return

        if item.feed_url and (item.guid or item.audio_url):
            outcome.show = RssSource(kind="rss", feed_url=item.feed_url, title=item.show_title)
            outcome.episode = EpisodeInput(
                feed_url=item.feed_url,
                guid=item.guid,
                audio_url=item.audio_url,
                show=_show_name(item.show_title, outcome.show),
                title=item.title or None,
            )
            return

        if item.spotify_id and not item.title:
            outcome.item = item = _with(item, title=self._oembed_title(SPOTIFY_OEMBED_URL, item))

        if item.apple_show_id or item.feed_url or item.show_title:
            show = self._show_source(item, session)
            if show is None:
                raise _Unresolvable(NO_SHOW_REASON)
            outcome.show = show
            episode = self._apple_episode(outcome, show, session)
            if episode is None and item.title:
                episode = self._feed_episode(item, show, session)
            if episode is None:
                raise _Unresolvable(f"episode not found in the {show.title or show.feed_url} feed")
            outcome.episode = episode
            return

        if not item.title:
            raise _Unresolvable(NO_TITLE_REASON)
        self._episode_by_title(outcome, session)

    # -- shows -------------------------------------------------------------------

    def _show_source(self, item: LibraryItem, session: _Session) -> RssSource | None:
        if item.feed_url:
            return RssSource(
                kind="rss",
                feed_url=item.feed_url,
                title=(item.title if item.item_kind == "show" else item.show_title) or None,
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

    # -- episodes ----------------------------------------------------------------

    def _apple_episode(
        self, outcome: _Outcome, show: RssSource, session: _Session
    ) -> EpisodeInput | None:
        item = outcome.item
        if not (item.apple_show_id and item.apple_episode_id):
            return None
        lookup = self._apple_show(item.apple_show_id, session)
        for episode in lookup.episodes:
            if episode.track_id == item.apple_episode_id and (episode.guid or episode.audio_url):
                if not item.title:
                    outcome.item = item = _with(
                        item, title=episode.title, show_title=item.show_title or show.title
                    )
                return EpisodeInput(
                    feed_url=show.feed_url,
                    guid=episode.guid,
                    audio_url=episode.audio_url,
                    show=_show_name(item.show_title, show),
                    title=item.title[:MAX_TITLE_CHARS] or None,
                )
        return None

    def _feed_episode(
        self, item: LibraryItem, show: RssSource, session: _Session
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
            if titles_match(listed.title, item.title):
                return listed.episode.model_copy(update={"show": _show_name(item.show_title, show)})
        return None

    def _episode_by_title(self, outcome: _Outcome, session: _Session) -> None:
        """No show known: Apple's episode index, exact title. One show with
        that title wins; two different shows is ambiguous, not a guess."""
        item = outcome.item
        hits = self._directory_call(
            lambda: self._directory.search_episodes(item.title, EPISODE_SEARCH_LIMIT), session
        )
        if isinstance(hits, PodcastError):
            raise hits
        matches = [h for h in hits if titles_match(h.title, item.title)]
        shows = {h.show_id or h.feed_url or h.show_title for h in matches}
        if not matches:
            raise _Unresolvable(NO_EPISODE_REASON)
        if len(shows) > 1:
            raise _Unresolvable(AMBIGUOUS_REASON)
        hit = matches[0]
        feed = hit.feed_url
        if feed is None and hit.show_id:
            feed = self._apple_show(hit.show_id, session).source.feed_url
        if feed is None:
            raise _Unresolvable(NO_SHOW_REASON)
        show = RssSource(kind="rss", feed_url=feed, title=hit.show_title)
        outcome.show = show
        outcome.item = item = _with(
            item,
            show_title=item.show_title or hit.show_title,
            apple_show_id=item.apple_show_id or hit.show_id,
        )
        if hit.guid or hit.audio_url:
            outcome.episode = EpisodeInput(
                feed_url=feed,
                guid=hit.guid,
                audio_url=hit.audio_url,
                show=_show_name(item.show_title, show),
                title=item.title[:MAX_TITLE_CHARS],
            )
            return
        episode = self._feed_episode(item, show, session)
        if episode is None:
            raise _Unresolvable(f"episode not found in the {show.title or feed} feed", show)
        outcome.episode = episode

    # -- oEmbed (public, keyless) ------------------------------------------------

    def _oembed(self, endpoint: str, item: LibraryItem) -> dict[str, object]:
        assert item.url is not None
        query = {"url": item.url}
        if endpoint == YOUTUBE_OEMBED_URL:
            query["format"] = "json"
        try:
            response = fetch_response(
                f"{endpoint}?{urlencode(query)}",
                what="oEmbed",
                max_bytes=OEMBED_MAX_BYTES,
                resolver=self._resolver,
            )
        except FeedFetchError as err:
            raise _Unresolvable(f"{NO_TITLE_REASON}: {err.reason}") from err
        if response.status_code != 200:
            raise _Unresolvable(f"{NO_TITLE_REASON} (HTTP {response.status_code})")
        try:
            payload = json.loads(response.content)
        except ValueError as err:
            raise _Unresolvable(NO_TITLE_REASON) from err
        if not isinstance(payload, dict):
            raise _Unresolvable(NO_TITLE_REASON)
        return payload

    def _oembed_title(self, endpoint: str, item: LibraryItem) -> str:
        title = self._oembed(endpoint, item).get("title")
        if not isinstance(title, str) or not title.strip():
            raise _Unresolvable(NO_TITLE_REASON)
        return title.strip()[:MAX_TITLE_CHARS]

    def _youtube_oembed(self, item: LibraryItem) -> tuple[str, str | None]:
        """A video's title and channel name. A private or embed-disabled
        video still digests by id, so a failed lookup is not fatal."""
        try:
            payload = self._oembed(YOUTUBE_OEMBED_URL, item)
        except _Unresolvable:
            return f"YouTube video {item.youtube_video_id}", None
        title, author = payload.get("title"), payload.get("author_name")
        return (
            title.strip()[:MAX_TITLE_CHARS]
            if isinstance(title, str) and title.strip()
            else f"YouTube video {item.youtube_video_id}",
            author.strip()[:MAX_SHOW_CHARS] if isinstance(author, str) and author.strip() else None,
        )


def _with(item: LibraryItem, **changes: str | None) -> LibraryItem:
    return item.model_copy(update=changes)


def _clip_show(name: str | None) -> str | None:
    return name[:MAX_SHOW_CHARS] if name else None


def _show_name(show_title: str | None, show: RssSource) -> str | None:
    return _clip_show(show.title or show_title)


def _unresolved(
    entry: SavedItem, outcome: _Outcome, reason: str, show: ShowLink | None
) -> SavedItem:
    return entry.model_copy(
        update={"item": outcome.item, "status": "unresolved", "reason": reason, "show_source": show}
    )
