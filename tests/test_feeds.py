"""chorus/feeds.py: RSS and YouTube Atom parsing, per-source errors, the
round-robin cap, and the SSRF guard on the fetch path. No network: the fetch
seam is faked (tests/feedfakes.py), and one test drives the real
`_fetch_bounded` with an injected resolver and a fake `httpx.stream`."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from chorus import feeds
from chorus import transcripts as tc
from chorus.feeds import (
    FeedEpisode,
    FeedFetchError,
    gather_episodes,
    list_recent_episodes,
    parse_datetime,
    round_robin,
)
from chorus.models import EpisodeInput
from chorus.subscriptions import RssSource, ShowSource, YoutubeSource
from tests.feedfakes import (
    install_fake_web,
    rfc2822,
    rss_feed,
    rss_item,
    youtube_entry,
    youtube_feed,
)

NOW = datetime(2026, 10, 2, 13, 0, tzinfo=UTC)
LONG_AGO = NOW - timedelta(days=365)
FEED = "https://feeds.example.com/show.xml"
CHANNEL = "UC" + "a" * 22
YT_FEED = f"https://www.youtube.com/feeds/videos.xml?channel_id={CHANNEL}"


def _rss(feed: str = FEED, title: str | None = None) -> RssSource:
    return RssSource(kind="rss", feed_url=feed, title=title)


# --- date parsing -------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Tue, 30 Sep 2025 05:00:00 +0000", datetime(2025, 9, 30, 5, 0, tzinfo=UTC)),
        ("Tue, 30 Sep 2025 05:00:00 GMT", datetime(2025, 9, 30, 5, 0, tzinfo=UTC)),
        ("Tue, 30 Sep 2025 01:00:00 -0400", datetime(2025, 9, 30, 5, 0, tzinfo=UTC)),
        ("Tue, 30 Sep 2025 05:00:00 EST", datetime(2025, 9, 30, 10, 0, tzinfo=UTC)),
        ("30 Sep 2025 05:00:00 +0000", datetime(2025, 9, 30, 5, 0, tzinfo=UTC)),
        ("Tue, 30 Sep 2025 05:00:00 -0000", datetime(2025, 9, 30, 5, 0, tzinfo=UTC)),
        ("2025-09-30T05:00:00Z", datetime(2025, 9, 30, 5, 0, tzinfo=UTC)),
        ("2025-09-30T05:00:00+00:00", datetime(2025, 9, 30, 5, 0, tzinfo=UTC)),
        ("2025-09-30", datetime(2025, 9, 30, 0, 0, tzinfo=UTC)),
    ],
)
def test_parse_datetime_accepts_rfc2822_and_iso(text: str, expected: datetime) -> None:
    parsed = parse_datetime(text)
    assert parsed == expected
    assert parsed is not None and parsed.tzinfo is not None


@pytest.mark.parametrize("text", [None, "", "yesterday-ish", "99 Foo 2025"])
def test_parse_datetime_rejects_garbage(text: str | None) -> None:
    assert parse_datetime(text) is None


# --- RSS parsing ----------------------------------------------------------------


def test_rss_lists_items_newest_first_with_guid_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    web = install_fake_web(monkeypatch)
    web.set(
        FEED,
        rss_feed(
            "Acquired",
            [
                rss_item("Old", pub=NOW - timedelta(days=30), guid="g-old", audio="https://cdn/old.mp3"),
                rss_item("New", pub=NOW - timedelta(days=1), guid="g-new", audio="https://cdn/new.mp3"),
                rss_item("Mid", pub=NOW - timedelta(days=3), guid="g-mid", audio="https://cdn/mid.mp3"),
            ],
        ),
    )

    out = list_recent_episodes(_rss(), NOW - timedelta(days=7), 10)

    assert [e.title for e in out] == ["New", "Mid"]  # the 30-day-old one is before `since`
    first = out[0]
    assert first.source_title == "Acquired"
    assert first.published_at == NOW - timedelta(days=1)
    assert first.published_at.tzinfo is not None
    assert first.episode.feed_url == FEED
    assert first.episode.guid == "g-new"
    assert first.episode.audio_url == "https://cdn/new.mp3"
    assert first.episode.show == "Acquired"
    assert first.episode.title == "New"


def test_rss_source_title_override_wins_over_feed_title(monkeypatch: pytest.MonkeyPatch) -> None:
    web = install_fake_web(monkeypatch)
    web.set(FEED, rss_feed("Feed Title", [rss_item("E", pub=NOW, guid="g")]))
    out = list_recent_episodes(_rss(title="My Name For It"), LONG_AGO, 5)
    assert out[0].source_title == "My Name For It"
    assert out[0].episode.show == "My Name For It"


def test_rss_missing_guid_falls_back_to_audio_url_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    web = install_fake_web(monkeypatch)
    web.set(FEED, rss_feed("Show", [rss_item("No guid", pub=NOW, audio="https://cdn/a.mp3")]))
    (episode,) = list_recent_episodes(_rss(), LONG_AGO, 5)
    assert episode.episode.guid is None
    assert episode.episode.audio_url == "https://cdn/a.mp3"
    assert episode.episode.resolved_id().startswith("rss-")


def test_rss_guid_without_enclosure_is_still_listed(monkeypatch: pytest.MonkeyPatch) -> None:
    web = install_fake_web(monkeypatch)
    web.set(FEED, rss_feed("Show", [rss_item("Text only", pub=NOW, guid="g-1")]))
    (episode,) = list_recent_episodes(_rss(), LONG_AGO, 5)
    assert episode.episode.guid == "g-1"
    assert episode.episode.audio_url is None


def test_rss_item_with_neither_guid_nor_enclosure_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    web = install_fake_web(monkeypatch)
    web.set(
        FEED,
        rss_feed("Show", [rss_item("Nothing identifies me", pub=NOW), rss_item("Ok", pub=NOW, guid="g")]),
    )
    out = list_recent_episodes(_rss(), LONG_AGO, 5)
    assert [e.title for e in out] == ["Ok"]


def test_rss_item_without_a_parseable_date_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    web = install_fake_web(monkeypatch)
    web.set(
        FEED,
        rss_feed(
            "Show",
            [
                rss_item("No date", pub=None, guid="g-nodate"),
                rss_item("Bad date", pub="sometime soon", guid="g-bad"),
                rss_item("Dated", pub=NOW, guid="g-ok"),
            ],
        ),
    )
    assert [e.title for e in list_recent_episodes(_rss(), LONG_AGO, 5)] == ["Dated"]


def test_rss_overlong_guid_falls_back_to_enclosure(monkeypatch: pytest.MonkeyPatch) -> None:
    web = install_fake_web(monkeypatch)
    web.set(
        FEED,
        rss_feed("Show", [rss_item("Long", pub=NOW, guid="g" * 600, audio="https://cdn/l.mp3")]),
    )
    (episode,) = list_recent_episodes(_rss(), LONG_AGO, 5)
    assert episode.episode.guid is None
    assert episode.episode.audio_url == "https://cdn/l.mp3"


def test_rss_limit_and_item_scan_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    web = install_fake_web(monkeypatch)
    items = [rss_item(f"E{i}", pub=NOW - timedelta(hours=i), guid=f"g{i}") for i in range(6)]
    web.set(FEED, rss_feed("Show", items))
    assert [e.title for e in list_recent_episodes(_rss(), LONG_AGO, 2)] == ["E0", "E1"]

    # Items past MAX_FEED_ITEMS are never scanned, even if they are newer.
    monkeypatch.setattr(feeds, "MAX_FEED_ITEMS", 3)
    web.set(
        FEED,
        rss_feed("Show", [rss_item(f"Old{i}", pub=NOW - timedelta(days=9 + i), guid=f"o{i}") for i in range(3)]
                 + [rss_item("Newest but past the cap", pub=NOW, guid="late")]),
    )
    titles = [e.title for e in list_recent_episodes(_rss(), LONG_AGO, 10)]
    assert "Newest but past the cap" not in titles


def test_rss_namespaced_default_namespace_feed(monkeypatch: pytest.MonkeyPatch) -> None:
    # Some feeds declare xmlns= on <rss>, which prefixes every plain tag; the
    # iTunes tags must not be mistaken for the plain ones that share a name.
    xml = (
        '<?xml version="1.0"?>'
        '<rss version="2.0" xmlns="http://example.com/ns" '
        'xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">'
        "<channel><title>Namespaced Show</title><itunes:title>Wrong Title</itunes:title>"
        "<itunes:image href=\"https://img/a.jpg\"/>"
        f"<item><title>Plain Title</title><itunes:title>Wrong</itunes:title><guid>ns-1</guid>"
        f"<pubDate>{rfc2822(NOW)}</pubDate>"
        '<enclosure url="https://cdn/ns.mp3" type="audio/mpeg"/></item>'
        "</channel></rss>"
    )
    web = install_fake_web(monkeypatch)
    web.set(FEED, xml)
    (episode,) = list_recent_episodes(_rss(), LONG_AGO, 5)
    assert episode.source_title == "Namespaced Show"
    assert episode.title == "Plain Title"
    assert episode.episode.guid == "ns-1"


def test_rss_podcast_namespace_tags_do_not_disturb_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    xml = (
        '<?xml version="1.0"?>'
        '<rss version="2.0" xmlns:podcast="https://podcastindex.org/namespace/1.0">'
        "<channel><title>PC20</title>"
        f"<item><title>With transcript</title><guid>t-1</guid><pubDate>{rfc2822(NOW)}</pubDate>"
        '<podcast:transcript url="https://cdn/t.json" type="application/json"/></item>'
        "</channel></rss>"
    )
    web = install_fake_web(monkeypatch)
    web.set(FEED, xml)
    assert [e.title for e in list_recent_episodes(_rss(), LONG_AGO, 5)] == ["With transcript"]


def test_rss_not_a_feed_is_a_feed_fetch_error(monkeypatch: pytest.MonkeyPatch) -> None:
    web = install_fake_web(monkeypatch)
    web.set(FEED, "<html><body>nope</body></html>")
    with pytest.raises(FeedFetchError, match="not an RSS feed"):
        list_recent_episodes(_rss(), LONG_AGO, 5)

    web.set(FEED, "<rss><oops>")
    with pytest.raises(FeedFetchError, match="not well-formed"):
        list_recent_episodes(_rss(), LONG_AGO, 5)


def test_rss_entity_declarations_are_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    web = install_fake_web(monkeypatch)
    web.set(
        FEED,
        '<?xml version="1.0"?><!DOCTYPE rss [<!ENTITY a "aaaa">]><rss><channel><title>&a;</title></channel></rss>',
    )
    with pytest.raises(FeedFetchError, match="entity"):
        list_recent_episodes(_rss(), LONG_AGO, 5)


def test_rss_http_error_status(monkeypatch: pytest.MonkeyPatch) -> None:
    web = install_fake_web(monkeypatch)
    web.set(FEED, "gone", status=410)
    with pytest.raises(FeedFetchError, match="HTTP 410"):
        list_recent_episodes(_rss(), LONG_AGO, 5)


def test_since_must_be_timezone_aware() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        list_recent_episodes(_rss(), datetime(2026, 1, 1), 5)  # noqa: DTZ001 - naive on purpose


# --- YouTube Atom ------------------------------------------------------------------


def test_youtube_atom_entries_become_video_id_episodes(monkeypatch: pytest.MonkeyPatch) -> None:
    web = install_fake_web(monkeypatch)
    web.set(
        YT_FEED,
        youtube_feed(
            "Lex Clips",
            [
                youtube_entry("AAAAAAAAAAA", "Older", "2026-09-20T10:00:00+00:00"),
                youtube_entry("BBBBBBBBBBB", "Newer", "2026-09-30T10:00:00+00:00"),
            ],
        ),
    )
    out = list_recent_episodes(
        YoutubeSource(kind="youtube", channel_id=CHANNEL), datetime(2026, 9, 1, tzinfo=UTC), 10
    )
    assert [e.title for e in out] == ["Newer", "Older"]
    assert out[0].episode == EpisodeInput(
        video_id="BBBBBBBBBBB",
        show="Lex Clips",
        title="Newer",
        published_at=datetime(2026, 9, 30, 10, 0, tzinfo=UTC),
    )
    assert out[0].source_title == "Lex Clips"
    assert out[0].published_at == datetime(2026, 9, 30, 10, 0, tzinfo=UTC)
    assert web.calls == [YT_FEED]


def test_youtube_entry_id_is_used_when_videoid_element_is_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    xml = (
        '<feed xmlns="http://www.w3.org/2005/Atom"><title>C</title>'
        "<entry><id>yt:video:CCCCCCCCCCC</id><title>T</title>"
        "<published>2026-09-30T10:00:00+00:00</published></entry></feed>"
    )
    web = install_fake_web(monkeypatch)
    web.set(YT_FEED, xml)
    (episode,) = list_recent_episodes(YoutubeSource(kind="youtube", channel_id=CHANNEL), LONG_AGO, 5)
    assert episode.episode.video_id == "CCCCCCCCCCC"


def test_youtube_unknown_channel_404(monkeypatch: pytest.MonkeyPatch) -> None:
    web = install_fake_web(monkeypatch)
    web.set(YT_FEED, "not found", status=404)
    with pytest.raises(FeedFetchError, match="not found"):
        list_recent_episodes(YoutubeSource(kind="youtube", channel_id=CHANNEL), LONG_AGO, 5)


def test_youtube_non_atom_response_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    web = install_fake_web(monkeypatch)
    web.set(YT_FEED, "<rss><channel/></rss>")
    with pytest.raises(FeedFetchError, match="not a YouTube channel feed"):
        list_recent_episodes(YoutubeSource(kind="youtube", channel_id=CHANNEL), LONG_AGO, 5)


# --- catalog show -------------------------------------------------------------------


def test_catalog_show_lists_fixture_episodes_stamped_with_since() -> None:
    since = NOW - timedelta(days=7)
    out = list_recent_episodes(ShowSource(kind="show", show="20VC with Harry Stebbings"), since, 10)
    assert out
    assert all(e.published_at == since for e in out)
    assert all(e.source_title == "20VC with Harry Stebbings" for e in out)


def test_unknown_catalog_show_is_an_error() -> None:
    with pytest.raises(FeedFetchError, match="unknown catalog show"):
        list_recent_episodes(ShowSource(kind="show", show="No Such Show"), NOW, 10)


# --- per-source errors never fail the gather -----------------------------------------


def test_gather_collects_errors_per_source(monkeypatch: pytest.MonkeyPatch) -> None:
    web = install_fake_web(monkeypatch)
    good = "https://good.example.com/rss"
    bad = "https://bad.example.com/rss"
    web.set(good, rss_feed("Good", [rss_item("Fine", pub=NOW, guid="g")]))
    web.set(bad, "boom", status=500)

    result = gather_episodes([_rss(bad), _rss(good)], LONG_AGO, 5)

    assert [len(per) for per in result.per_source] == [0, 1]
    assert len(result.errors) == 1
    assert result.errors[0].source == _rss(bad)
    assert "HTTP 500" in result.errors[0].reason


def test_gather_survives_an_unexpected_exception() -> None:
    def exploding(source, since, limit, *, resolver=None):  # type: ignore[no-untyped-def]
        raise RuntimeError("kaboom")

    result = gather_episodes([_rss()], LONG_AGO, 5, lister=exploding)
    assert result.per_source == [[]]
    assert result.errors[0].reason == "RuntimeError: kaboom"


def test_gather_with_no_sources() -> None:
    assert gather_episodes([], LONG_AGO, 5).per_source == []


# --- SSRF guard on the real fetch path ------------------------------------------------


def test_private_address_feed_is_refused_through_the_real_fetch_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse_any_stream(*args: object, **kwargs: object) -> None:
        raise AssertionError("a refused URL must never reach the network")

    monkeypatch.setattr(tc.httpx, "stream", refuse_any_stream)
    with pytest.raises(FeedFetchError, match="unsafe url|disallowed|not permitted"):
        list_recent_episodes(
            _rss("https://internal.example.com/feed"),
            LONG_AGO,
            5,
            resolver=lambda host: ["10.0.0.5"],
        )
    with pytest.raises(FeedFetchError, match="not permitted"):
        list_recent_episodes(_rss("http://169.254.169.254/latest/meta-data"), LONG_AGO, 5)


# --- round robin ------------------------------------------------------------------------


def _fe(source: str, n: int, hours_ago: int) -> FeedEpisode:
    return FeedEpisode(
        source_title=source,
        title=f"{source}-{n}",
        published_at=NOW - timedelta(hours=hours_ago),
        episode=EpisodeInput(feed_url=f"https://{source}.example.com/rss", guid=f"{source}-{n}"),
    )


def test_round_robin_gives_every_source_a_turn() -> None:
    prolific = [_fe("A", i, hours_ago=i) for i in range(10)]  # A is always newest
    quiet_b = [_fe("B", 0, hours_ago=50)]
    quiet_c = [_fe("C", 0, hours_ago=90)]

    picked = round_robin([prolific, quiet_b, quiet_c], cap=4)

    titles = [e.title for e in picked]
    assert "B-0" in titles and "C-0" in titles  # not crowded out by A
    assert len(picked) == 4
    assert [e.published_at for e in picked] == sorted((e.published_at for e in picked), reverse=True)


def test_round_robin_cap_smaller_than_source_count_prefers_newest_heads() -> None:
    a, b, c = _fe("A", 0, 5), _fe("B", 0, 1), _fe("C", 0, 9)
    assert [e.title for e in round_robin([[a], [b], [c]], cap=2)] == ["B-0", "A-0"]


def test_round_robin_counts_a_duplicate_episode_once() -> None:
    shared = _fe("A", 0, 1)
    same_via_other_source = shared.model_copy(update={"source_title": "B"})
    picked = round_robin([[shared], [same_via_other_source, _fe("B", 1, 5)]], cap=5)
    assert [e.title for e in picked] == ["A-0", "B-1"]


def test_round_robin_handles_empty_input() -> None:
    assert round_robin([], cap=3) == []
    assert round_robin([[], []], cap=3) == []


# --- Oversized feeds: newest-first prefix, full read only when needed ------------

_BIG_URL = "https://feeds.example.com/big.xml"
_NOW = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)
_PAD = "x" * 2000  # ~2 KB per item, so ~1000 items fill the 2 MiB prefix


def _daily_items(n: int, *, newest_first: bool) -> list[str]:
    items = [
        rss_item(
            f"Episode {i} {_PAD}",
            pub=_NOW - timedelta(days=i),
            guid=f"g{i}",
            audio=f"https://cdn.example.com/{i}.mp3",
        )
        for i in range(n)
    ]
    return items if newest_first else list(reversed(items))


def _big_source(web: object, *, newest_first: bool, n: int = 1_500) -> RssSource:
    body = rss_feed("Big Show", _daily_items(n, newest_first=newest_first))
    assert len(body.encode("utf-8")) > tc.FEED_PREFIX_BYTES
    web.set(_BIG_URL, body)  # type: ignore[attr-defined]
    return RssSource(kind="rss", feed_url=_BIG_URL)


def test_big_newest_first_feed_is_answered_from_the_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    web = install_fake_web(monkeypatch)
    source = _big_source(web, newest_first=True)
    eps = feeds.list_recent_episodes(source, _NOW - timedelta(days=7), limit=8)
    assert [e.episode.guid for e in eps] == [f"g{i}" for i in range(8)]
    assert web.calls == [_BIG_URL]  # one bounded read, no full download
    assert web.call_kwargs[0].get("truncate_ok") is True


def test_big_oldest_first_feed_falls_back_to_a_full_read(monkeypatch: pytest.MonkeyPatch) -> None:
    web = install_fake_web(monkeypatch)
    source = _big_source(web, newest_first=False)
    eps = feeds.list_recent_episodes(source, _NOW - timedelta(days=7), limit=8)
    assert [e.episode.guid for e in eps] == [f"g{i}" for i in range(8)]
    assert web.calls == [_BIG_URL, _BIG_URL]  # prefix held only old episodes


def test_window_running_past_the_prefix_triggers_a_full_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    web = install_fake_web(monkeypatch)
    source = _big_source(web, newest_first=True)
    eps = feeds.list_recent_episodes(source, _NOW - timedelta(days=5_000), limit=1_400)
    assert len(eps) == 1_400  # more than the prefix holds
    assert web.calls == [_BIG_URL, _BIG_URL]


# --- Source context for the script stage -------------------------------------------


def test_rss_item_carries_plain_text_show_notes_and_date() -> None:
    import xml.etree.ElementTree as ET

    from chorus.feeds import parse_rss

    notes = "&lt;p&gt;Marc Andreessen joins &lt;b&gt;Harry&lt;/b&gt; to talk venture &amp;amp; AI.&lt;/p&gt;"
    item = (
        f"<item><title>Ep</title><guid>g</guid><pubDate>{rfc2822(NOW)}</pubDate>"
        f"<description>{notes}</description></item>"
    )
    parsed = parse_rss(ET.fromstring(rss_feed("20VC", [item])), FEED)

    episode = parsed.episodes[0].episode
    assert episode.description == "Marc Andreessen joins Harry to talk venture & AI."
    assert episode.published_at == parsed.episodes[0].published_at
    assert episode.show == "20VC"


def test_youtube_entry_carries_media_description() -> None:
    import xml.etree.ElementTree as ET

    from chorus.feeds import parse_youtube_atom

    entry = (
        '<entry xmlns:media="http://search.yahoo.com/mrss/"><yt:videoId>AAAAAAAAAAA</yt:videoId>'
        "<title>T</title><published>2026-09-20T10:00:00+00:00</published>"
        "<media:group><media:description>Guest: Jane Doe, CEO of Acme.</media:description>"
        "</media:group></entry>"
    )
    parsed = parse_youtube_atom(ET.fromstring(youtube_feed("Chan", [entry])))

    assert parsed.episodes[0].episode.description == "Guest: Jane Doe, CEO of Acme."


def test_plain_description_caps_and_empties() -> None:
    from chorus.feeds import plain_description
    from chorus.models import MAX_DESCRIPTION_CHARS

    assert plain_description(None) is None
    assert plain_description("<p> </p>") is None
    assert len(plain_description("word " * 2000) or "") == MAX_DESCRIPTION_CHARS
