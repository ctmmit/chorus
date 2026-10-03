"""Library import: what the principal already follows or has saved, in one
normalized shape, plus the pure logic that turns it into Chorus inputs.

A library item is a show (a followed podcast or YouTube channel), an episode
(something saved to hear later), or a document (an article or highlight,
used only as soul corpus). Items reach Chorus without the principal signing
in anywhere, all landing in the same model (chorus.library_api):

- Shared links: an Apple Podcasts, Spotify or YouTube link from a share
  sheet or a message (chorus.library_inputs.parse_link), so Chorus itself
  is the save-for-later queue.
- Export files: a YouTube Takeout subscriptions.csv or a podcast-app OPML.
- Agent push: an agent with its own connector to a library the principal
  keeps elsewhere posts the items as-is; Chorus never holds that token.

Three outputs come from the same items:

1. `rank_show_suggestions`: shows ranked by how often and how recently they
   were saved, as subscription candidates.
2. The saved queue: resolved, unheard episodes that a `SavedQueueSource`
   feeds into each run (`saved_queue_episodes`).
3. `corpus_texts`: titles, tags, notes and highlights for
   `chorus.bootstrap.SoulBuilder.derive_from_corpus`.

Everything in this module is pure (no I/O); network resolution lives in
chorus.library_resolve and storage in chorus.saved_items.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal
from urllib.parse import parse_qs, urlsplit

from pydantic import BaseModel, Field, field_validator, model_validator

from chorus.feeds import FeedEpisode
from chorus.models import (
    MAX_GUID_CHARS,
    MAX_SHOW_CHARS,
    MAX_TITLE_CHARS,
    MAX_URL_CHARS,
    EpisodeInput,
    _canonical_feed_url,
)
from chorus.subscriptions import (
    YOUTUBE_CHANNEL_ID_PATTERN,
    LibraryProvider,
    RssSource,
    SavedQueueSource,
    YoutubeSource,
)

MAX_LIBRARY_ITEMS = 500
MAX_EXTERNAL_ID_CHARS = 200
MAX_ITEM_TAGS = 25
MAX_TAG_CHARS = 100
MAX_ITEM_HIGHLIGHTS = 50
MAX_HIGHLIGHT_CHARS = 2_000
MAX_NOTES_CHARS = 4_000
ITEM_KEY_HASH_CHARS = 20

# Suggestion scoring: each save contributes 0.5 ** (age_days / half_life), so
# a save from today counts 1.0 and one from a month ago 0.5. An explicit
# subscription (a show item) counts as one fresh save and is always suggested.
SUGGESTION_HALF_LIFE_DAYS = 30.0
UNDATED_SAVE_WEIGHT = 0.5
SHOW_ITEM_WEIGHT = 1.0
MIN_SAVES_TO_SUGGEST = 2
MAX_SUGGESTIONS = 20
SECONDS_PER_DAY = 86_400.0

# The saved queue ignores saves older than this: a save from last year that
# was never played is no longer a statement of current interest.
SAVED_QUEUE_MAX_AGE_DAYS = 120

CORPUS_MAX_TEXTS = 200
# Two titles match when their normalized forms are equal, or when the shorter
# is at least this long and contained in the longer (feeds often append
# "| Show Name" or an episode number that the saving app dropped).
MIN_TITLE_CONTAINMENT_CHARS = 24

APPLE_PODCAST_HOSTS = frozenset({"podcasts.apple.com", "itunes.apple.com"})
_APPLE_SHOW_ID_RE = re.compile(r"/id(\d+)")
_DIGITS_RE = re.compile(r"^\d{1,20}$")
_NON_WORD_RE = re.compile(r"[^\w]+")
_EPOCH = datetime.min.replace(tzinfo=UTC)

YOUTUBE_VIDEO_ID_PATTERN = r"^[A-Za-z0-9_-]{11}$"
SPOTIFY_ID_PATTERN = r"^[A-Za-z0-9]{22}$"

ItemKind = Literal["show", "episode", "document"]
# A followed show resolves to either kind of subscription source.
ShowLink = RssSource | YoutubeSource
ResolutionStatus = Literal["resolved", "pending", "unresolved", "corpus"]


def _require_tz_aware(value: datetime | None, field_name: str) -> datetime | None:
    if value is not None and value.tzinfo is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value


# --- Apple URLs ---------------------------------------------------------------


class AppleRef(BaseModel):
    """The ids an Apple Podcasts link carries."""

    show_id: str = Field(description="Apple collection id of the show.")
    episode_id: str | None = Field(
        default=None, description="Apple track id of the episode (the ?i= parameter), if any."
    )


def parse_apple_url(url: str) -> AppleRef | None:
    """`podcasts.apple.com/us/podcast/<slug>/id1516093381?i=1000792593373`
    -> AppleRef(show_id="1516093381", episode_id="1000792593373"). Returns
    None for anything that is not an Apple Podcasts show or episode link."""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return None
    if (
        parts.scheme not in ("http", "https")
        or (parts.hostname or "").lower() not in APPLE_PODCAST_HOSTS
    ):
        return None
    match = _APPLE_SHOW_ID_RE.search(parts.path)
    if match is None:
        return None
    episode = next(iter(parse_qs(parts.query).get("i", [])), None)
    return AppleRef(
        show_id=match.group(1),
        episode_id=episode if episode and _DIGITS_RE.match(episode) else None,
    )


def normalize_title(title: str) -> str:
    """Lowercase, punctuation folded to spaces, whitespace collapsed."""
    return " ".join(_NON_WORD_RE.sub(" ", title.lower()).split())


def titles_match(a: str, b: str) -> bool:
    left, right = normalize_title(a), normalize_title(b)
    if not left or not right:
        return False
    if left == right:
        return True
    shorter, longer = sorted((left, right), key=len)
    return len(shorter) >= MIN_TITLE_CONTAINMENT_CHARS and shorter in longer


# --- models -------------------------------------------------------------------


class LibraryItem(BaseModel):
    """One show, episode, or document from the principal's library, as an
    agent or connector reports it. `provider`, `item_kind`, and a title or
    an identifying link/id are required; every identifier that is present
    makes resolution cheaper (feed_url + guid needs no lookup at all; an
    Apple link needs one). A shared link with no title gets one from
    resolution."""

    provider: LibraryProvider = Field(description="Where the item came from.")
    item_kind: ItemKind = Field(
        description=(
            '"show" (a followed show), "episode" (a saved episode) or "document" (corpus only).'
        ),
    )
    title: str = Field(
        default="",
        max_length=MAX_TITLE_CHARS,
        description=(
            "Episode title, show title for a show item, or document title; may be empty "
            "when url or an id identifies the item (resolution fills it in)."
        ),
    )
    show_title: str | None = Field(
        default=None, max_length=MAX_SHOW_CHARS, description="The show an episode belongs to."
    )
    author: str | None = Field(
        default=None, max_length=MAX_SHOW_CHARS, description="Author or publisher."
    )
    external_id: str | None = Field(
        default=None,
        max_length=MAX_EXTERNAL_ID_CHARS,
        description="The provider's own id for the item (e.g. a Reader document id).",
    )
    url: str | None = Field(
        default=None,
        max_length=MAX_URL_CHARS,
        description="Canonical link, e.g. an Apple Podcasts episode URL; Apple ids come from it.",
    )
    apple_show_id: str | None = Field(
        default=None, pattern=r"^\d{1,20}$", description="Apple collection id."
    )
    apple_episode_id: str | None = Field(
        default=None, pattern=r"^\d{1,20}$", description="Apple track id."
    )
    spotify_id: str | None = Field(
        default=None, pattern=SPOTIFY_ID_PATTERN, description="Spotify show or episode id."
    )
    youtube_video_id: str | None = Field(
        default=None, pattern=YOUTUBE_VIDEO_ID_PATTERN, description="YouTube video id (episodes)."
    )
    youtube_channel_id: str | None = Field(
        default=None,
        pattern=YOUTUBE_CHANNEL_ID_PATTERN,
        description="YouTube channel id, UC plus 22 characters (shows).",
    )
    feed_url: str | None = Field(
        default=None,
        max_length=MAX_URL_CHARS,
        pattern=r"^https?://",
        description="Show RSS feed URL.",
    )
    guid: str | None = Field(
        default=None, max_length=MAX_GUID_CHARS, description="Episode <guid> in the feed."
    )
    audio_url: str | None = Field(
        default=None,
        max_length=MAX_URL_CHARS,
        pattern=r"^https?://",
        description="Episode audio URL.",
    )
    saved_at: datetime | None = Field(
        default=None, description="When it was saved (timezone-aware)."
    )
    consumed: bool | None = Field(
        default=None,
        description="True once listened to or archived; consumed episodes leave the saved queue.",
    )
    tags: list[str] = Field(default_factory=list, description="The principal's tags on the item.")
    highlights: list[str] = Field(
        default_factory=list, description="Highlighted passages or quotes."
    )
    notes: str | None = Field(
        default=None, max_length=MAX_NOTES_CHARS, description="The principal's notes."
    )

    @field_validator("saved_at")
    @classmethod
    def _validate_saved_at(cls, value: datetime | None) -> datetime | None:
        return _require_tz_aware(value, "saved_at")

    @field_validator("tags")
    @classmethod
    def _bound_tags(cls, value: list[str]) -> list[str]:
        cleaned = [t.strip()[:MAX_TAG_CHARS] for t in value if t.strip()]
        return list(dict.fromkeys(cleaned))[:MAX_ITEM_TAGS]

    @field_validator("highlights")
    @classmethod
    def _bound_highlights(cls, value: list[str]) -> list[str]:
        return [h.strip()[:MAX_HIGHLIGHT_CHARS] for h in value if h.strip()][:MAX_ITEM_HIGHLIGHTS]

    @model_validator(mode="after")
    def _ids_from_url(self) -> LibraryItem:
        if self.url and self.apple_show_id is None:
            ref = parse_apple_url(self.url)
            if ref is not None:
                self.apple_show_id = ref.show_id
                if self.apple_episode_id is None:
                    self.apple_episode_id = ref.episode_id
        if not self.title.strip() and not self._has_identity():
            raise ValueError("a library item needs a title, a url, or an id that identifies it")
        self.title = self.title.strip()
        return self

    def _has_identity(self) -> bool:
        return bool(
            self.url
            or self.apple_show_id
            or self.spotify_id
            or self.youtube_video_id
            or self.youtube_channel_id
            or self.feed_url
        )

    def item_key(self) -> str:
        """A stable identity, provider-independent where the item carries a
        universal id: the same Apple episode saved through Reader and pushed
        by an agent is one item, not two."""
        if self.item_kind == "show":
            return f"show:{show_key(self)}"
        if self.apple_episode_id:
            return f"apple:{self.apple_episode_id}"
        if self.youtube_video_id:
            return f"youtube:{self.youtube_video_id}"
        if self.spotify_id:
            return f"spotify:{self.spotify_id}"
        if self.feed_url and (self.guid or self.audio_url):
            basis = f"{_canonical_feed_url(self.feed_url)}\n{self.guid or self.audio_url}"
            return f"rss:{_hash(basis)}"
        if self.external_id:
            return f"{self.provider}:{self.external_id}"
        if self.url:
            return f"url:{_hash(self.url.strip())}"
        return f"title:{_hash(f'{self.show_title or ""}\n{normalize_title(self.title)}')}"


def _hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:ITEM_KEY_HASH_CHARS]


def show_key(item: LibraryItem) -> str:
    """Grouping key for the show an item belongs to: Apple id first (most
    stable), then the YouTube channel or Spotify show, then the feed URL,
    then the normalized show title."""
    if item.apple_show_id:
        return f"apple:{item.apple_show_id}"
    if item.youtube_channel_id:
        return f"youtube:{item.youtube_channel_id}"
    if item.item_kind == "show" and item.spotify_id:
        return f"spotify:{item.spotify_id}"
    if item.feed_url:
        return f"feed:{_canonical_feed_url(item.feed_url)}"
    name = item.title if item.item_kind == "show" else (item.show_title or item.author or "")
    return f"title:{normalize_title(name)}"


class LibraryImport(BaseModel):
    """POST /library/import request body (and the MCP import_library input)."""

    items: list[LibraryItem] = Field(
        min_length=1, max_length=MAX_LIBRARY_ITEMS, description="Library items to import."
    )


class SavedItem(BaseModel):
    """A stored library item for one owner, plus what resolution found."""

    owner: str = Field(description='The importing key\'s email, or "master".')
    key: str = Field(description="LibraryItem.item_key(); unique per owner.")
    item: LibraryItem = Field(description="The item as last imported.")
    status: ResolutionStatus = Field(
        description=(
            '"resolved", "pending" (not looked up yet), "unresolved" (with reason) or "corpus".'
        ),
    )
    episode: EpisodeInput | None = Field(
        default=None, description="The digestible episode, once resolved (episodes only)."
    )
    show_source: ShowLink | None = Field(
        default=None,
        discriminator="kind",
        description="The show as a subscription source (RSS or YouTube), once resolved.",
    )
    reason: str | None = Field(default=None, description="Why the item is pending or unresolved.")
    imported_at: datetime = Field(description="First import time (UTC).")
    updated_at: datetime = Field(description="Most recent import time (UTC).")


def _identity(item: LibraryItem) -> tuple[str | None, ...]:
    return (
        item.apple_show_id,
        item.apple_episode_id,
        item.spotify_id,
        item.youtube_video_id,
        item.youtube_channel_id,
        item.feed_url,
        item.guid,
        item.audio_url,
        normalize_title(item.title),
        item.show_title,
    )


# Fields chorus.library_resolve may fill in on a stored item.
RESOLVER_FILLED_FIELDS = ("title", "show_title", "apple_show_id")


def initial_status(item: LibraryItem) -> ResolutionStatus:
    return "corpus" if item.item_kind == "document" else "pending"


def merge_item(
    existing: SavedItem | None, incoming: LibraryItem, owner: str, now: datetime
) -> SavedItem:
    """Fold a re-imported item into what is stored. Resolution is kept when
    nothing that identifies the episode changed, so re-importing a library
    (to pick up new saves, or `consumed` flipping after a listen) costs no
    lookups for items already resolved. An unresolved item is retried. A
    re-share that lacks what resolution filled in (title, show, Apple show
    id) keeps the stored values."""
    if existing is not None:
        filled = {
            name: getattr(existing.item, name)
            for name in RESOLVER_FILLED_FIELDS
            if not getattr(incoming, name) and getattr(existing.item, name)
        }
        if filled:
            incoming = incoming.model_copy(update=filled)
    if existing is not None and _identity(existing.item) == _identity(incoming):
        status = existing.status if existing.status != "unresolved" else initial_status(incoming)
        return existing.model_copy(
            update={
                "item": incoming,
                "status": status,
                "reason": existing.reason if status == existing.status else None,
                "updated_at": now,
            }
        )
    return SavedItem(
        owner=owner,
        key=incoming.item_key(),
        item=incoming,
        status=initial_status(incoming),
        imported_at=existing.imported_at if existing is not None else now,
        updated_at=now,
    )


# --- suggestions --------------------------------------------------------------


class ShowSuggestion(BaseModel):
    """A show the principal saves often (or follows elsewhere), ranked."""

    show_key: str = Field(description="Grouping key (apple:<id>, feed:<url> or title:<name>).")
    title: str = Field(description="Show title.")
    save_count: int = Field(ge=0, description="Saved episodes from this show.")
    last_saved_at: datetime | None = Field(default=None, description="Most recent save, if dated.")
    score: float = Field(description="Recency-weighted save count; higher ranks first.")
    explicit: bool = Field(description="True when the principal follows the show in another app.")
    source: ShowLink | None = Field(
        default=None,
        discriminator="kind",
        description="Ready-to-subscribe source (RSS or YouTube), when the show resolved.",
    )
    already_subscribed: bool = Field(
        default=False,
        description="True when one of the principal's subscriptions already has this feed.",
    )


def save_weight(saved_at: datetime | None, now: datetime, half_life_days: float) -> float:
    """0.5 ** (age_days / half_life_days); a save in the future counts as now."""
    if saved_at is None:
        return UNDATED_SAVE_WEIGHT
    age_days = max(0.0, (now - saved_at).total_seconds() / SECONDS_PER_DAY)
    return float(0.5 ** (age_days / half_life_days))


def source_key(source: ShowLink) -> str:
    """The show_key a subscription source corresponds to."""
    if isinstance(source, YoutubeSource):
        return f"youtube:{source.channel_id}"
    return f"feed:{_canonical_feed_url(source.feed_url)}"


@dataclass
class _ShowGroup:
    title: str | None = None
    title_at: datetime | None = None
    count: int = 0
    last: datetime | None = None
    score: float = 0.0
    explicit: bool = False


def rank_show_suggestions(
    items: Iterable[LibraryItem],
    now: datetime,
    *,
    sources: Mapping[str, ShowLink] | None = None,
    subscribed: Iterable[ShowLink] = (),
    half_life_days: float = SUGGESTION_HALF_LIFE_DAYS,
    min_saves: int = MIN_SAVES_TO_SUGGEST,
    limit: int = MAX_SUGGESTIONS,
) -> list[ShowSuggestion]:
    """Group episodes by show and rank. A show qualifies with `min_saves`
    saved episodes or one explicit follow. `sources` maps show_key to a
    resolved source; `subscribed` (the principal's current sources) marks
    shows already covered."""
    if now.tzinfo is None:
        raise ValueError("rank_show_suggestions: `now` must be timezone-aware")
    covered = {source_key(s) for s in subscribed}
    groups: dict[str, _ShowGroup] = {}
    for item in items:
        if item.item_kind == "document":
            continue
        key = show_key(item)
        if key == "title:":
            continue
        group = groups.setdefault(key, _ShowGroup())
        name = item.title if item.item_kind == "show" else (item.show_title or item.author)
        stamp = item.saved_at or _EPOCH
        if name and (group.title_at is None or stamp >= group.title_at):
            group.title, group.title_at = name, stamp
        if item.item_kind == "show":
            group.explicit = True
            group.score += SHOW_ITEM_WEIGHT
            continue
        group.count += 1
        group.score += save_weight(item.saved_at, now, half_life_days)
        if item.saved_at is not None and (group.last is None or item.saved_at > group.last):
            group.last = item.saved_at

    suggestions: list[ShowSuggestion] = []
    for key, group in groups.items():
        if not group.explicit and group.count < min_saves:
            continue
        source = (sources or {}).get(key)
        keys = {key} | ({source_key(source)} if source is not None else set())
        suggestions.append(
            ShowSuggestion(
                show_key=key,
                title=(source.title if source and source.title else None)
                or group.title
                or key.split(":", 1)[1],
                save_count=group.count,
                last_saved_at=group.last,
                score=round(group.score, 4),
                explicit=group.explicit,
                source=source,
                already_subscribed=bool(keys & covered),
            )
        )
    suggestions.sort(key=lambda s: (-s.score, s.title.lower()))
    return suggestions[:limit]


# --- saved queue --------------------------------------------------------------


def saved_queue_episodes(
    saved: Sequence[SavedItem],
    source: SavedQueueSource,
    now: datetime,
    limit: int,
    exclude_ids: frozenset[str] = frozenset(),
    *,
    max_age_days: int = SAVED_QUEUE_MAX_AGE_DAYS,
) -> list[FeedEpisode]:
    """The saved queue as FeedEpisodes, newest save first: resolved,
    unconsumed episodes saved within `max_age_days`, from `source.providers`
    (all when unset), minus `exclude_ids` (a subscription's seen list, applied
    here so already-digested saves cannot crowd out older unheard ones).
    `published_at` is the save time, which is what orders a queue."""
    oldest = now - timedelta(days=max_age_days)
    wanted = set(source.providers) if source.providers else None
    picked: list[FeedEpisode] = []
    seen: set[str] = set()
    ordered = sorted(saved, key=lambda s: s.item.saved_at or _EPOCH, reverse=True)
    for entry in ordered:
        item = entry.item
        if entry.status != "resolved" or entry.episode is None or item.item_kind != "episode":
            continue
        if item.consumed or (wanted is not None and item.provider not in wanted):
            continue
        saved_at = item.saved_at or entry.imported_at
        if saved_at < oldest:
            continue
        episode_id = entry.episode.resolved_id()
        if episode_id in exclude_ids or episode_id in seen:
            continue
        seen.add(episode_id)
        picked.append(
            FeedEpisode(
                source_title=item.show_title or source.title,
                title=item.title,
                published_at=saved_at.astimezone(UTC),
                episode=entry.episode,
            )
        )
        if len(picked) >= limit:
            break
    return picked


# --- soul corpus --------------------------------------------------------------


def corpus_texts(items: Iterable[LibraryItem], limit: int = CORPUS_MAX_TEXTS) -> list[str]:
    """One text per item, newest first, for SoulBuilder.derive_from_corpus:
    the title (with its show), then tags, notes and highlights when present.
    Consumed and unconsumed items both count: a save is a statement of
    interest whether or not it was played."""
    ordered = sorted(items, key=lambda i: i.saved_at or _EPOCH, reverse=True)
    texts: list[str] = []
    for item in [i for i in ordered if i.title][:limit]:
        lines = [f"{item.title} ({item.show_title})" if item.show_title else item.title]
        if item.tags:
            lines.append("Tags: " + ", ".join(item.tags))
        if item.notes:
            lines.append(f"Notes: {item.notes}")
        lines.extend(f"Highlight: {h}" for h in item.highlights)
        texts.append("\n".join(lines))
    return texts
