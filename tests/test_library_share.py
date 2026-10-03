"""Library inputs that need no sign-in: shared Apple / Spotify / YouTube links
(chorus.library_inputs.parse_link, POST /library/share, MCP share_links) and
export files (YouTube Takeout, OPML via POST /library/import-file), plus
their keyless resolution (oEmbed, Apple's episode index).

No network: `chorus.feeds._fetch_bounded` is faked and netguard's DNS is
stubbed.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode

import pytest
from fastapi.testclient import TestClient

from chorus import netguard, podcasts_api
from chorus.app import create_app
from chorus.audio import MockAudioRenderer
from chorus.email import MockEmailSender
from chorus.jobs import SqliteJobStore
from chorus.keys import SqliteKeyStore
from chorus.library import LibraryItem, SavedItem, merge_item, saved_queue_episodes
from chorus.library_api import LibraryService, SharedItemStatus, share_summary
from chorus.library_inputs import (
    HANDLE_REASON,
    SHORT_LINK_REASON,
    UNSUPPORTED_REASON,
    LinkError,
    SkippedLink,
    links_from_text,
    parse_link,
    parse_youtube_takeout,
)
from chorus.library_resolve import (
    AMBIGUOUS_REASON,
    NO_EPISODE_REASON,
    SPOTIFY_OEMBED_URL,
    YOUTUBE_OEMBED_URL,
    LibraryResolver,
)
from chorus.llm import MockLLMClient
from chorus.mcp_server import ChorusTools, create_mcp_server
from chorus.models import EpisodeInput
from chorus.pipeline import Deps
from chorus.podcasts_api import PodcastDirectory, normalize_query
from chorus.saved_items import SqliteSavedItemStore
from chorus.script import MockScriptComposer
from chorus.subscriptions import (
    RssSource,
    SavedQueueSource,
    SqliteSubscriptionStore,
    Subscription,
    YoutubeSource,
)
from chorus.transcripts import FixtureTranscriptProvider
from tests.feedfakes import FakeWeb, install_fake_web, rss_feed, rss_item

MASTER_HEADERS = {"Authorization": "Bearer master-token"}
PUBLIC_IP = "93.184.216.34"
NOW = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)

SPOTIFY_EP = "4rOoJ6Egrf8K2IrywzwOMk"
SPOTIFY_SHOW = "2MAi0BvDc6GTFvKFPXnkCL"
VIDEO = "dQw4w9WgXcQ"
CHANNEL = "UC" + "a" * 22
OTHER_CHANNEL = "UC" + "b" * 22
APPLE_SHOW = "1056200096"
APPLE_EP = "1000790896800"
FEED = "https://feeds.example.com/odd-lots.xml"
OTHER_FEED = "https://feeds.example.com/other.xml"
EP_TITLE = "How LA Is Quietly Becoming America's New Industrial Tech Hub"


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(netguard, "_default_resolver", lambda host: [PUBLIC_IP])


def spotify_oembed(kind: str, spotify_id: str) -> str:
    link = f"https://open.spotify.com/{kind}/{spotify_id}"
    return f"{SPOTIFY_OEMBED_URL}?{urlencode({'url': link})}"


def youtube_oembed(video_id: str) -> str:
    query = urlencode({"url": f"https://www.youtube.com/watch?v={video_id}", "format": "json"})
    return f"{YOUTUBE_OEMBED_URL}?{query}"


def episode_search(title: str) -> str:
    query = urlencode(
        {
            "media": "podcast",
            "entity": "podcastEpisode",
            "term": normalize_query(title),
            "limit": 25,
        }
    )
    return f"{podcasts_api.ITUNES_SEARCH_URL}?{query}"


def show_search(term: str) -> str:
    query = urlencode({"media": "podcast", "entity": "podcast", "term": term, "limit": 25})
    return f"{podcasts_api.ITUNES_SEARCH_URL}?{query}"


def show_lookup(show_id: str) -> str:
    query = urlencode({"id": show_id, "entity": "podcastEpisode", "limit": 200})
    return f"{podcasts_api.ITUNES_LOOKUP_URL}?{query}"


def hit(
    title: str, show: str, show_id: int, feed: str | None, guid: str | None
) -> dict[str, object]:
    entry: dict[str, object] = {
        "wrapperType": "podcastEpisode",
        "trackName": title,
        "collectionName": show,
        "collectionId": show_id,
        "episodeUrl": f"https://audio.example.com/{show_id}.mp3",
    }
    if feed:
        entry["feedUrl"] = feed
    if guid:
        entry["episodeGuid"] = guid
    return entry


# --- parsing links ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("link", "kind", "field", "value"),
    [
        (
            f"https://open.spotify.com/episode/{SPOTIFY_EP}?si=abc",
            "episode",
            "spotify_id",
            SPOTIFY_EP,
        ),
        (
            f"https://open.spotify.com/intl-de/episode/{SPOTIFY_EP}",
            "episode",
            "spotify_id",
            SPOTIFY_EP,
        ),
        (f"https://open.spotify.com/show/{SPOTIFY_SHOW}", "show", "spotify_id", SPOTIFY_SHOW),
        (f"spotify:episode:{SPOTIFY_EP}", "episode", "spotify_id", SPOTIFY_EP),
        (f"https://www.youtube.com/watch?v={VIDEO}&t=42", "episode", "youtube_video_id", VIDEO),
        (f"https://youtu.be/{VIDEO}?si=x", "episode", "youtube_video_id", VIDEO),
        (f"https://m.youtube.com/shorts/{VIDEO}", "episode", "youtube_video_id", VIDEO),
        (f"https://www.youtube.com/live/{VIDEO}", "episode", "youtube_video_id", VIDEO),
        (f"https://www.youtube.com/channel/{CHANNEL}", "show", "youtube_channel_id", CHANNEL),
        (
            f"https://podcasts.apple.com/us/podcast/odd-lots/id{APPLE_SHOW}?i={APPLE_EP}",
            "episode",
            "apple_episode_id",
            APPLE_EP,
        ),
        (
            f"https://podcasts.apple.com/us/podcast/odd-lots/id{APPLE_SHOW}",
            "show",
            "apple_show_id",
            APPLE_SHOW,
        ),
    ],
)
def test_parse_link_recognizes_episode_and_show_links(
    link: str, kind: str, field: str, value: str
) -> None:
    item = parse_link(link, saved_at=NOW)
    assert item.item_kind == kind and getattr(item, field) == value
    assert item.provider == "shared" and item.title == "" and item.saved_at == NOW


def test_spotify_links_are_canonicalized_so_resharing_dedupes() -> None:
    a = parse_link(f"https://open.spotify.com/episode/{SPOTIFY_EP}?si=one")
    b = parse_link(f"spotify:episode:{SPOTIFY_EP}")
    assert a.url == b.url == f"https://open.spotify.com/episode/{SPOTIFY_EP}"
    assert a.item_key() == b.item_key()


@pytest.mark.parametrize(
    ("link", "reason"),
    [
        ("https://spotify.link/abc123", SHORT_LINK_REASON),
        ("https://www.youtube.com/@somechannel", HANDLE_REASON),
        ("https://www.youtube.com/watch?v=short", UNSUPPORTED_REASON),
        ("https://open.spotify.com/track/4rOoJ6Egrf8K2IrywzwOMk", UNSUPPORTED_REASON),
        ("https://example.com/podcast", UNSUPPORTED_REASON),
        ("not a link", UNSUPPORTED_REASON),
    ],
)
def test_parse_link_explains_what_it_cannot_follow(link: str, reason: str) -> None:
    with pytest.raises(LinkError) as err:
        parse_link(link)
    assert str(err.value) == reason


def test_links_from_text_extracts_trims_and_dedupes() -> None:
    text = (
        f"Listen to this (https://youtu.be/{VIDEO}). "
        f"Also https://open.spotify.com/episode/{SPOTIFY_EP}, "
        f"and again https://youtu.be/{VIDEO}! Or spotify:episode:{SPOTIFY_EP}"
    )
    assert links_from_text(text) == [
        f"https://youtu.be/{VIDEO}",
        f"https://open.spotify.com/episode/{SPOTIFY_EP}",
        f"spotify:episode:{SPOTIFY_EP}",
    ]


def test_an_item_needs_a_title_or_an_identity() -> None:
    with pytest.raises(ValueError, match="needs a title"):
        LibraryItem(provider="pushed", item_kind="episode", title="  ")
    assert LibraryItem(provider="shared", item_kind="episode", youtube_video_id=VIDEO).title == ""


def test_parse_youtube_takeout_reads_channels_by_shape() -> None:
    csv_text = (
        "﻿Kanal-ID,Kanal-URL,Kanaltitel\n"  # a localized header row
        f"{CHANNEL},http://www.youtube.com/channel/{CHANNEL},Lex Fridman\n"
        f'{OTHER_CHANNEL},http://www.youtube.com/channel/{OTHER_CHANNEL},"Dwarkesh, Patel"\n'
        f"{CHANNEL},http://www.youtube.com/channel/{CHANNEL},Lex Fridman\n"
        "garbage,row,here\n"
        "\n"
    )
    parsed = parse_youtube_takeout(csv_text)
    assert [(i.youtube_channel_id, i.title) for i in parsed.items] == [
        (CHANNEL, "Lex Fridman"),
        (OTHER_CHANNEL, "Dwarkesh, Patel"),
    ]
    assert all(i.item_kind == "show" and i.provider == "youtube" for i in parsed.items)
    assert [s.reason for s in parsed.skipped] == ["no channel id"]


# --- resolution ---------------------------------------------------------------------------


def _resolve(item: LibraryItem) -> SavedItem:
    [resolved] = LibraryResolver(PodcastDirectory(clock=lambda: 0.0)).resolve(
        [merge_item(None, item, "master", NOW)]
    )
    return resolved


def test_a_youtube_video_is_its_own_episode_titled_by_oembed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    web = install_fake_web(monkeypatch)
    web.set(
        youtube_oembed(VIDEO),
        json.dumps({"title": "A long conversation", "author_name": "Some Channel"}),
    )
    resolved = _resolve(parse_link(f"https://youtu.be/{VIDEO}"))
    assert resolved.status == "resolved"
    assert resolved.episode == EpisodeInput(
        video_id=VIDEO, title="A long conversation", show="Some Channel"
    )
    assert (resolved.item.title, resolved.item.show_title) == (
        "A long conversation",
        "Some Channel",
    )


def test_a_youtube_video_without_oembed_still_resolves(monkeypatch: pytest.MonkeyPatch) -> None:
    web = install_fake_web(monkeypatch)
    web.set(youtube_oembed(VIDEO), "unauthorized", status=401)
    resolved = _resolve(parse_link(f"https://youtu.be/{VIDEO}"))
    assert resolved.status == "resolved" and resolved.episode is not None
    assert resolved.episode.video_id == VIDEO and resolved.item.title == f"YouTube video {VIDEO}"


def test_a_youtube_channel_is_a_source_without_any_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    web = install_fake_web(monkeypatch)
    resolved = _resolve(parse_link(f"https://www.youtube.com/channel/{CHANNEL}"))
    assert resolved.status == "resolved" and web.calls == []
    assert resolved.show_source == YoutubeSource(kind="youtube", channel_id=CHANNEL, title=None)


def test_a_spotify_episode_resolves_through_oembed_and_apples_index(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    web = install_fake_web(monkeypatch)
    web.set(
        spotify_oembed("episode", SPOTIFY_EP),
        json.dumps({"title": EP_TITLE, "provider_name": "Spotify"}),
    )
    web.set(
        episode_search(EP_TITLE),
        json.dumps({"results": [hit(EP_TITLE, "Odd Lots", int(APPLE_SHOW), FEED, "ol-guid")]}),
    )
    resolved = _resolve(parse_link(f"https://open.spotify.com/episode/{SPOTIFY_EP}"))
    assert resolved.status == "resolved" and resolved.episode is not None
    assert (resolved.episode.feed_url, resolved.episode.guid) == (FEED, "ol-guid")
    assert (resolved.item.title, resolved.item.show_title) == (EP_TITLE, "Odd Lots")
    assert resolved.item.apple_show_id == APPLE_SHOW  # groups with Apple saves of the same show
    assert resolved.show_source == RssSource(kind="rss", feed_url=FEED, title="Odd Lots")


def test_an_episode_search_hit_without_a_guid_is_matched_in_the_feed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    web = install_fake_web(monkeypatch)
    web.set(spotify_oembed("episode", SPOTIFY_EP), json.dumps({"title": EP_TITLE}))
    entry = hit(EP_TITLE, "Odd Lots", int(APPLE_SHOW), FEED, None)
    del entry["episodeUrl"]
    web.set(episode_search(EP_TITLE), json.dumps({"results": [entry]}))
    web.set(FEED, rss_feed("Odd Lots", [rss_item(EP_TITLE, pub=NOW, guid="from-feed")]))
    resolved = _resolve(parse_link(f"https://open.spotify.com/episode/{SPOTIFY_EP}"))
    assert resolved.status == "resolved" and resolved.episode is not None
    assert resolved.episode.guid == "from-feed"


def test_a_spotify_exclusive_is_unresolved_not_guessed(monkeypatch: pytest.MonkeyPatch) -> None:
    web = install_fake_web(monkeypatch)
    web.set(
        spotify_oembed("episode", SPOTIFY_EP), json.dumps({"title": "Exclusive Interview Part 3"})
    )
    web.set(
        episode_search("Exclusive Interview Part 3"),
        json.dumps({"results": [hit("A different episode entirely", "Other", 5, OTHER_FEED, "g")]}),
    )
    resolved = _resolve(parse_link(f"https://open.spotify.com/episode/{SPOTIFY_EP}"))
    assert resolved.status == "unresolved" and resolved.reason == NO_EPISODE_REASON
    assert (
        resolved.item.title == "Exclusive Interview Part 3"
    )  # kept for the corpus and the listing


def test_one_title_in_two_shows_is_ambiguous(monkeypatch: pytest.MonkeyPatch) -> None:
    web = install_fake_web(monkeypatch)
    title = "The Year in Review and What Comes Next"
    web.set(spotify_oembed("episode", SPOTIFY_EP), json.dumps({"title": title}))
    web.set(
        episode_search(title),
        json.dumps(
            {
                "results": [
                    hit(title, "Show A", 1, FEED, "a"),
                    hit(title, "Show B", 2, OTHER_FEED, "b"),
                ]
            }
        ),
    )
    resolved = _resolve(parse_link(f"https://open.spotify.com/episode/{SPOTIFY_EP}"))
    assert resolved.status == "unresolved" and resolved.reason == AMBIGUOUS_REASON


def test_a_spotify_show_link_resolves_by_exact_show_title(monkeypatch: pytest.MonkeyPatch) -> None:
    web = install_fake_web(monkeypatch)
    web.set(spotify_oembed("show", SPOTIFY_SHOW), json.dumps({"title": "Odd Lots"}))
    web.set(
        show_search("odd lots"),
        json.dumps({"results": [{"collectionName": "Odd Lots", "feedUrl": FEED}]}),
    )
    resolved = _resolve(parse_link(f"https://open.spotify.com/show/{SPOTIFY_SHOW}"))
    assert resolved.status == "resolved" and resolved.show_source is not None
    assert resolved.show_source.kind == "rss" and resolved.item.title == "Odd Lots"


def test_a_titleless_apple_link_takes_its_title_from_apple(monkeypatch: pytest.MonkeyPatch) -> None:
    web = install_fake_web(monkeypatch)
    web.set(
        show_lookup(APPLE_SHOW),
        json.dumps(
            {
                "results": [
                    {"wrapperType": "track", "collectionName": "Odd Lots", "feedUrl": FEED},
                    {
                        "wrapperType": "podcastEpisode",
                        "trackId": int(APPLE_EP),
                        "trackName": EP_TITLE,
                        "episodeGuid": "ol-1",
                    },
                ]
            }
        ),
    )
    link = f"https://podcasts.apple.com/us/podcast/odd-lots/id{APPLE_SHOW}?i={APPLE_EP}"
    resolved = _resolve(parse_link(link))
    assert resolved.status == "resolved" and resolved.episode is not None
    assert (resolved.item.title, resolved.item.show_title) == (EP_TITLE, "Odd Lots")
    assert resolved.episode.guid == "ol-1"


# --- HTTP / MCP ---------------------------------------------------------------------------


def _deps(tmp_path: Path) -> Deps:
    return Deps(
        provider=FixtureTranscriptProvider(),
        llm=MockLLMClient(),
        composer=MockScriptComposer(),
        renderer=MockAudioRenderer(out_dir=tmp_path / "artifacts"),
    )


def _client(tmp_path: Path) -> tuple[TestClient, SqliteSubscriptionStore]:
    db = tmp_path / "chorus.db"
    subs = SqliteSubscriptionStore(db)
    app = create_app(
        SqliteJobStore(db),
        _deps(tmp_path),
        api_token="master-token",
        key_store=SqliteKeyStore(db),
        email_sender=MockEmailSender(),
        subscription_store=subs,
        saved_item_store=SqliteSavedItemStore(db),
    )
    return TestClient(app), subs


def _install_share_web(monkeypatch: pytest.MonkeyPatch) -> FakeWeb:
    web = install_fake_web(monkeypatch)
    web.set(youtube_oembed(VIDEO), json.dumps({"title": "Video episode", "author_name": "Chan"}))
    web.set(spotify_oembed("episode", SPOTIFY_EP), json.dumps({"title": EP_TITLE}))
    web.set(
        episode_search(EP_TITLE),
        json.dumps({"results": [hit(EP_TITLE, "Odd Lots", int(APPLE_SHOW), FEED, "ol-guid")]}),
    )
    return web


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/library/share", {"links": [f"https://youtu.be/{VIDEO}"]}),
        ("/library/import-file", {"format": "opml", "content": "<opml/>"}),
    ],
)
def test_share_and_file_routes_require_a_bearer_token(
    tmp_path: Path, path: str, body: object
) -> None:
    client, _ = _client(tmp_path)
    assert client.post(path, json=body).status_code == 401


def test_share_text_saves_each_link_and_reports_the_rest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_share_web(monkeypatch)
    client, _ = _client(tmp_path)
    text = (
        f"two for the weekend: https://youtu.be/{VIDEO} and "
        f"https://open.spotify.com/episode/{SPOTIFY_EP}?si=1 (also https://example.com/blog)"
    )
    r = client.post("/library/share", json={"text": text}, headers=MASTER_HEADERS)
    assert r.status_code == 200, r.text
    body = r.json()
    assert [(i["title"], i["status"]) for i in body["items"]] == [
        ("Video episode", "resolved"),
        (EP_TITLE, "resolved"),
    ]
    assert body["skipped"] == [{"link": "https://example.com/blog", "reason": UNSUPPORTED_REASON}]
    assert body["preview"]["queued_episodes"] == 2


def test_share_summary_is_one_readable_line() -> None:
    def status(title: str, state: str, kind: str = "episode", reason: str | None = None) -> object:
        return SharedItemStatus(
            link="x", title=title, show_title="Odd Lots", item_kind=kind, status=state, reason=reason
        )

    assert share_summary([status("Ep", "resolved")], [], 3) == (
        "Saved “Ep” (Odd Lots). 3 episodes in your queue."
    )
    assert share_summary([status("Ep", "unresolved", reason="no feed")], [], 0) == (
        "Could not match “Ep” (Odd Lots): no feed"
    )
    assert share_summary([status("Show", "resolved", kind="show")], [], 0) == (
        "Added “Show” (Odd Lots) to your suggested shows."
    )
    skipped = [SkippedLink(link="y", reason="nope")]
    assert share_summary([], skipped, 0) == "Nothing saved: nope"
    two = [status("A", "resolved"), status("B", "unresolved")]
    assert share_summary(two, skipped, 1) == "Saved 1 of 3 links. 1 episode in your queue."


def test_resharing_keeps_titles_and_costs_no_lookups(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    web = _install_share_web(monkeypatch)
    client, _ = _client(tmp_path)
    links = {"links": [f"https://open.spotify.com/episode/{SPOTIFY_EP}"]}
    client.post("/library/share", json=links, headers=MASTER_HEADERS)
    calls = len(web.calls)
    again = client.post("/library/share", json=links, headers=MASTER_HEADERS).json()
    assert again["preview"]["new"] == 0 and again["items"][0]["title"] == EP_TITLE
    assert len(web.calls) == calls


def test_share_requires_links_or_text(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    assert client.post("/library/share", json={}, headers=MASTER_HEADERS).status_code == 422


def test_takeout_import_suggests_channels_and_marks_subscribed_ones(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    web = install_fake_web(monkeypatch)
    client, subs = _client(tmp_path)
    subs.create(
        Subscription(
            subscription_id="s1",
            owner="master",
            email="p@example.com",
            soul="soul",
            context="",
            sources=[YoutubeSource(kind="youtube", channel_id=CHANNEL)],
            next_run_at=NOW + timedelta(days=1),
        )
    )
    content = (
        "Channel Id,Channel Url,Channel Title\n"
        f"{CHANNEL},http://www.youtube.com/channel/{CHANNEL},Lex Fridman\n"
        f"{OTHER_CHANNEL},http://www.youtube.com/channel/{OTHER_CHANNEL},Dwarkesh Patel\n"
    )
    r = client.post(
        "/library/import-file",
        json={"format": "youtube_takeout", "content": content},
        headers=MASTER_HEADERS,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["imported"] == 2 and web.calls == []
    by_title = {s["title"]: s for s in body["preview"]["suggested_sources"]}
    assert by_title["Lex Fridman"]["already_subscribed"] is True
    assert by_title["Dwarkesh Patel"]["source"] == {
        "kind": "youtube",
        "channel_id": OTHER_CHANNEL,
        "title": "Dwarkesh Patel",
    }
    assert by_title["Dwarkesh Patel"]["explicit"] is True


def test_opml_import_turns_follows_into_suggestions(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    opml = (
        '<opml version="2.0"><body>'
        f'<outline text="Odd Lots" type="rss" xmlUrl="{FEED}"/>'
        '<outline text="Broken" type="rss" xmlUrl="not-a-url"/></body></opml>'
    )
    body = client.post(
        "/library/import-file", json={"format": "opml", "content": opml}, headers=MASTER_HEADERS
    ).json()
    assert body["imported"] == 1 and len(body["skipped"]) == 1
    [suggestion] = body["preview"]["suggested_sources"]
    assert suggestion["title"] == "Odd Lots" and suggestion["source"]["feed_url"] == FEED
    bad = client.post(
        "/library/import-file",
        json={"format": "opml", "content": "<html/>"},
        headers=MASTER_HEADERS,
    )
    assert bad.status_code == 422


def test_mcp_share_links_and_import_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _install_share_web(monkeypatch)
    db = tmp_path / f"t-{uuid.uuid4().hex}.db"
    subs, saved = SqliteSubscriptionStore(db), SqliteSavedItemStore(db)
    directory = PodcastDirectory(clock=lambda: 0.0)
    tools = ChorusTools(
        SqliteJobStore(db),
        _deps(tmp_path),
        subscription_store=subs,
        podcasts=directory,
        library=LibraryService(saved, directory, subs),
    )
    result = tools.share_links(text=f"watch https://youtu.be/{VIDEO}")
    assert [i.status for i in result.items] == ["resolved"]
    takeout = f"Channel Id,Channel Url,Channel Title\n{CHANNEL},x,Lex\n"
    assert tools.import_file("youtube_takeout", takeout).imported == 1

    queue = saved_queue_episodes(
        saved.list("master"), SavedQueueSource(kind="saved"), datetime.now(UTC), 10
    )
    assert [e.episode.video_id for e in queue] == [VIDEO]


def test_mcp_registers_share_and_file_tools(tmp_path: Path) -> None:
    import asyncio

    server, _ = create_mcp_server(SqliteJobStore(tmp_path / "c.db"), _deps(tmp_path))
    names = {tool.name for tool in asyncio.run(server.list_tools())}
    assert {"share_links", "import_file"} <= names
