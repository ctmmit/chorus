"""Turn what a principal can hand over without signing in to anything into
library items (chorus.library.LibraryItem). Everything here is pure parsing:
no network, no credentials.

- Shared links (`parse_link`, `links_from_text`): an Apple Podcasts, Spotify
  or YouTube link to one episode or show, as it comes out of a phone's share
  sheet, a pasted message, or a forwarded email. The item carries only the
  ids in the link; chorus.library_resolve fills in titles and feeds.
- A Google Takeout YouTube export (`parse_youtube_takeout`):
  `YouTube and YouTube Music/subscriptions/subscriptions.csv`, one followed
  channel per row.
- An OPML export (`opml_show_items`): the followed shows of Overcast, Pocket
  Casts, Castro, or an Apple Podcasts Shortcut, as explicit show items.
"""

from __future__ import annotations

import csv
import io
import re
from datetime import datetime
from typing import Literal
from urllib.parse import parse_qs, urlsplit

from pydantic import BaseModel, Field

from chorus.library import LibraryItem, parse_apple_url
from chorus.podcasts_api import OpmlImport
from chorus.subscriptions import YOUTUBE_CHANNEL_ID_PATTERN, LibraryProvider, RssSource

MAX_SHARE_LINKS = 100
MAX_SHARE_TEXT_CHARS = 20_000
MAX_TAKEOUT_BYTES = 1 << 20
MAX_TAKEOUT_ROWS = 2_000
MAX_SKIPPED_LINK_CHARS = 300

SPOTIFY_HOSTS = frozenset({"open.spotify.com", "spotify.link"})
YOUTUBE_HOSTS = frozenset(
    {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be"}
)
# open.spotify.com/episode/<id>, optionally behind a locale segment (/intl-de/).
_SPOTIFY_PATH_RE = re.compile(
    r"^/(?:intl-[a-z]{2}(?:-[a-z]{2})?/)?(episode|show)/([A-Za-z0-9]{22})"
)
_SPOTIFY_URI_RE = re.compile(r"^spotify:(episode|show):([A-Za-z0-9]{22})$")
_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_CHANNEL_ID_RE = re.compile(YOUTUBE_CHANNEL_ID_PATTERN)
_YOUTUBE_VIDEO_PREFIXES = ("shorts", "live", "embed", "v")
# Links in free text: http(s) URLs and spotify: URIs, stopping at whitespace
# and the brackets/quotes that usually wrap a pasted link.
_LINK_IN_TEXT_RE = re.compile(
    r"""(https?://[^\s<>"'()\[\]]+|spotify:(?:episode|show):[A-Za-z0-9]{22})"""
)
_TRAILING_PUNCTUATION = ".,;:!?"

SHORT_LINK_REASON = (
    "spotify.link short links need opening first; share the open.spotify.com link instead"
)
HANDLE_REASON = (
    "YouTube @handle links cannot be followed without a YouTube API key; share the "
    "channel's /channel/UC... link or a video link instead"
)
UNSUPPORTED_REASON = "not an Apple Podcasts, Spotify or YouTube episode or show link"


class SkippedLink(BaseModel):
    """A shared link or file row that did not become a library item."""

    link: str = Field(description="The link or row, abbreviated.")
    reason: str = Field(description="Why it was skipped.")


class ParsedLinks(BaseModel):
    items: list[LibraryItem] = Field(description="One item per recognized link, in order.")
    skipped: list[SkippedLink] = Field(description="Links that were not recognized, with reasons.")


def _clip(text: str) -> str:
    if len(text) <= MAX_SKIPPED_LINK_CHARS:
        return text
    return text[: MAX_SKIPPED_LINK_CHARS - 1] + "…"


class LinkError(ValueError):
    """A link that names nothing Chorus can follow; the message says why."""


def parse_link(
    link: str, *, saved_at: datetime | None = None, provider: LibraryProvider = "shared"
) -> LibraryItem:
    """One shared link -> a title-less LibraryItem carrying the link's ids.
    Raises LinkError (with a user-facing reason) for anything unsupported."""
    raw = link.strip()
    common: dict[str, object] = {"provider": provider, "saved_at": saved_at}
    spotify_uri = _SPOTIFY_URI_RE.match(raw)
    if spotify_uri:
        kind, spotify_id = spotify_uri.groups()
        return LibraryItem.model_validate(
            {
                **common,
                "item_kind": "episode" if kind == "episode" else "show",
                "spotify_id": spotify_id,
                "url": f"https://open.spotify.com/{kind}/{spotify_id}",
            }
        )
    try:
        parts = urlsplit(raw)
    except ValueError as err:
        raise LinkError(UNSUPPORTED_REASON) from err
    host = (parts.hostname or "").lower()
    if parts.scheme not in ("http", "https") or not host:
        raise LinkError(UNSUPPORTED_REASON)

    apple = parse_apple_url(raw)
    if apple is not None:
        return LibraryItem.model_validate(
            {**common, "item_kind": "episode" if apple.episode_id else "show", "url": raw}
        )

    if host in SPOTIFY_HOSTS:
        if host == "spotify.link":
            raise LinkError(SHORT_LINK_REASON)
        match = _SPOTIFY_PATH_RE.match(parts.path)
        if match is None:
            raise LinkError(UNSUPPORTED_REASON)
        kind, spotify_id = match.groups()
        return LibraryItem.model_validate(
            {
                **common,
                "item_kind": "episode" if kind == "episode" else "show",
                "spotify_id": spotify_id,
                "url": f"https://open.spotify.com/{kind}/{spotify_id}",
            }
        )

    if host in YOUTUBE_HOSTS:
        return _youtube_item(host, parts.path, parts.query, common)

    raise LinkError(UNSUPPORTED_REASON)


def _youtube_item(host: str, path: str, query: str, common: dict[str, object]) -> LibraryItem:
    segments = [s for s in path.split("/") if s]
    video_id: str | None = None
    if host == "youtu.be" and segments:
        video_id = segments[0]
    elif segments[:1] == ["watch"]:
        video_id = next(iter(parse_qs(query).get("v", [])), None)
    elif len(segments) >= 2 and segments[0] in _YOUTUBE_VIDEO_PREFIXES:
        video_id = segments[1]
    elif len(segments) >= 2 and segments[0] == "channel" and _CHANNEL_ID_RE.match(segments[1]):
        return LibraryItem.model_validate(
            {
                **common,
                "item_kind": "show",
                "youtube_channel_id": segments[1],
                "url": f"https://www.youtube.com/channel/{segments[1]}",
            }
        )
    elif segments and (segments[0].startswith("@") or segments[0] in ("c", "user")):
        raise LinkError(HANDLE_REASON)
    if video_id is None or not _VIDEO_ID_RE.match(video_id):
        raise LinkError(UNSUPPORTED_REASON)
    return LibraryItem.model_validate(
        {
            **common,
            "item_kind": "episode",
            "youtube_video_id": video_id,
            "url": f"https://www.youtube.com/watch?v={video_id}",
        }
    )


def links_from_text(text: str) -> list[str]:
    """Every http(s) URL and spotify: URI in `text`, in order, de-duplicated,
    with sentence punctuation trimmed off the end."""
    found: list[str] = []
    for match in _LINK_IN_TEXT_RE.finditer(text[:MAX_SHARE_TEXT_CHARS]):
        link = match.group(1).rstrip(_TRAILING_PUNCTUATION)
        if link not in found:
            found.append(link)
    return found


def parse_links(
    links: list[str], *, saved_at: datetime | None = None, provider: LibraryProvider = "shared"
) -> ParsedLinks:
    items: list[LibraryItem] = []
    skipped: list[SkippedLink] = []
    for link in links[:MAX_SHARE_LINKS]:
        try:
            items.append(parse_link(link, saved_at=saved_at, provider=provider))
        except LinkError as err:
            skipped.append(SkippedLink(link=_clip(link), reason=str(err)))
    for link in links[MAX_SHARE_LINKS:]:
        skipped.append(
            SkippedLink(link=_clip(link), reason=f"over the {MAX_SHARE_LINKS}-link limit")
        )
    return ParsedLinks(items=items, skipped=skipped)


# --- files ------------------------------------------------------------------------

FileFormat = Literal["youtube_takeout", "opml"]


def parse_youtube_takeout(content: str) -> ParsedLinks:
    """Google Takeout `subscriptions.csv` -> one YouTube show item per
    channel. Columns are found by content, not header text, because Takeout
    localizes the header row ("Channel Id,Channel Url,Channel Title" in
    English): the channel id is the cell shaped like UC + 22 characters, the
    title is the first other cell that is not a URL."""
    if len(content.encode("utf-8")) > MAX_TAKEOUT_BYTES:
        raise ValueError(f"the Takeout file exceeds the {MAX_TAKEOUT_BYTES} byte limit")
    items: list[LibraryItem] = []
    skipped: list[SkippedLink] = []
    seen: set[str] = set()
    rows = csv.reader(io.StringIO(content.lstrip("﻿")))
    for index, row in enumerate(rows):
        cells = [c.strip() for c in row if c.strip()]
        if not cells:
            continue
        channel = next((c for c in cells if _CHANNEL_ID_RE.match(c)), None)
        if channel is None:
            if index > 0:
                skipped.append(SkippedLink(link=_clip(",".join(cells)), reason="no channel id"))
            continue  # the header row
        if channel in seen:
            continue
        if len(items) >= MAX_TAKEOUT_ROWS:
            skipped.append(
                SkippedLink(link=channel, reason=f"over the {MAX_TAKEOUT_ROWS}-channel limit")
            )
            continue
        seen.add(channel)
        title = next(
            (
                c
                for c in cells
                if c != channel and not c.lower().startswith(("http://", "https://"))
            ),
            "",
        )
        items.append(
            LibraryItem(
                provider="youtube",
                item_kind="show",
                title=title,
                youtube_channel_id=channel,
                url=f"https://www.youtube.com/channel/{channel}",
            )
        )
    return ParsedLinks(items=items, skipped=skipped)


def opml_show_items(parsed: OpmlImport) -> list[LibraryItem]:
    """OPML feed outlines -> explicit show items (always suggested)."""
    return [
        LibraryItem(
            provider="opml",
            item_kind="show",
            title=source.title or "",
            feed_url=source.feed_url,
        )
        for source in parsed.sources
        if isinstance(source, RssSource)
    ]
