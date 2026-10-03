"""Offline stand-ins for the web, shared by the feed-subscription tests.

`FakeWeb` replaces `chorus.feeds._fetch_bounded` (the one outbound-HTTP seam
of chorus.feeds and chorus.podcasts_api), so no test touches DNS or the
network. Feed builders produce small RSS 2.0 and YouTube Atom documents.
"""
from __future__ import annotations

import email.utils
from datetime import datetime
from html import escape

import pytest

from chorus import feeds
from chorus.transcripts import TranscriptProviderError, _BoundedResponse


class FakeWeb:
    def __init__(self) -> None:
        self.routes: dict[str, tuple[int, bytes]] = {}
        self.calls: list[str] = []
        self.call_kwargs: list[dict[str, object]] = []

    def set(self, url: str, body: str | bytes, status: int = 200) -> None:
        self.routes[url] = (status, body.encode("utf-8") if isinstance(body, str) else body)

    def __call__(
        self,
        method: str,
        url: str,
        *,
        timeout_s: float,
        what: str,
        max_bytes: int,
        resolver: object = None,
        **kwargs: object,
    ) -> _BoundedResponse:
        self.calls.append(url)
        self.call_kwargs.append(kwargs)
        if method != "GET" or url not in self.routes:
            raise TranscriptProviderError(f"{what}: transport error fetching {url}: no route")
        status, body = self.routes[url]
        if len(body) > max_bytes:
            raise TranscriptProviderError(f"{what}: response exceeded {max_bytes} bytes at {url}")
        import httpx

        return _BoundedResponse(status, httpx.Headers({}), body)


def install_fake_web(monkeypatch: pytest.MonkeyPatch) -> FakeWeb:
    web = FakeWeb()
    monkeypatch.setattr(feeds, "_fetch_bounded", web)
    return web


def rfc2822(moment: datetime) -> str:
    return email.utils.format_datetime(moment)


def rss_item(
    title: str,
    *,
    pub: datetime | str | None,
    guid: str | None = None,
    audio: str | None = None,
) -> str:
    parts = [f"<title>{escape(title)}</title>"]
    if pub is not None:
        parts.append(f"<pubDate>{pub if isinstance(pub, str) else rfc2822(pub)}</pubDate>")
    if guid is not None:
        parts.append(f'<guid isPermaLink="false">{escape(guid)}</guid>')
    if audio is not None:
        parts.append(f'<enclosure url="{escape(audio)}" type="audio/mpeg" length="1"/>')
    return "<item>" + "".join(parts) + "</item>"


def rss_feed(title: str, items: list[str], *, image: str | None = None) -> str:
    art = f'<itunes:image href="{image}"/>' if image else ""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">'
        f"<channel><title>{escape(title)}</title>{art}" + "".join(items) + "</channel></rss>"
    )


def youtube_entry(video_id: str, title: str, published: str) -> str:
    return (
        f"<entry><id>yt:video:{video_id}</id><yt:videoId>{video_id}</yt:videoId>"
        f"<yt:channelId>UCxxxxxxxxxxxxxxxxxxxxxx</yt:channelId><title>{escape(title)}</title>"
        f"<published>{published}</published><updated>{published}</updated></entry>"
    )


def youtube_feed(title: str, entries: list[str]) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<feed xmlns:yt="http://www.youtube.com/xml/schemas/2015" '
        'xmlns="http://www.w3.org/2005/Atom">'
        f"<title>{escape(title)}</title>" + "".join(entries) + "</feed>"
    )
