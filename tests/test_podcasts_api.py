"""Podcast discovery routes (search, resolve, OPML import), POST
/souls/interview, and the MCP subscription tools. No network: the fetch seam
(`chorus.feeds._fetch_bounded`) is faked and netguard's DNS is stubbed."""
from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest
from fastapi.testclient import TestClient

from chorus import netguard, podcasts_api
from chorus.app import create_app
from chorus.audio import MockAudioRenderer
from chorus.email import MockEmailSender
from chorus.jobs import SqliteJobStore
from chorus.keys import SqliteKeyStore
from chorus.llm import MockLLMClient
from chorus.mcp_server import ChorusTools, create_mcp_server
from chorus.pipeline import Deps
from chorus.podcasts_api import PodcastDirectory, PodcastError, parse_opml
from chorus.script import MockScriptComposer
from chorus.subscriptions import RssSource, SqliteSubscriptionStore, YoutubeSource
from chorus.transcripts import FixtureTranscriptProvider
from tests.feedfakes import (
    install_fake_web,
    rss_feed,
    rss_item,
    youtube_entry,
    youtube_feed,
)

MASTER_HEADERS = {"Authorization": "Bearer master-token"}
FEED = "https://feeds.example.com/acquired.xml"
CHANNEL = "UC" + "q" * 22
YT_FEED = f"https://www.youtube.com/feeds/videos.xml?channel_id={CHANNEL}"
PUBLIC_IP = "93.184.216.34"


def _search_url(term: str) -> str:
    query = urlencode({"media": "podcast", "entity": "podcast", "term": term, "limit": 25})
    return f"{podcasts_api.ITUNES_SEARCH_URL}?{query}"


def _lookup_url(apple_id: str) -> str:
    return f"{podcasts_api.ITUNES_LOOKUP_URL}?{urlencode({'id': apple_id, 'entity': 'podcast'})}"


def _channels_url(parameter: str, value: str) -> str:
    return f"{podcasts_api.YOUTUBE_CHANNELS_URL}?{urlencode({'part': 'snippet', parameter: value})}"


ITUNES_ACQUIRED = {
    "resultCount": 2,
    "results": [
        {
            "wrapperType": "track",
            "kind": "podcast",
            "collectionId": 1050462261,
            "collectionName": "Acquired",
            "artistName": "Ben Gilbert and David Rosenthal",
            "feedUrl": FEED,
            "artworkUrl600": "https://img.example.com/acquired600.jpg",
        },
        {  # an entry with no feedUrl (some catalog items have none) is dropped
            "wrapperType": "track",
            "kind": "podcast",
            "collectionId": 2,
            "collectionName": "No Feed Show",
            "artistName": "Nobody",
        },
    ],
}


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("CHORUS_VIEWER_URL", raising=False)
    # Public-looking DNS for every host, so safe_url passes without a lookup.
    monkeypatch.setattr(netguard, "_default_resolver", lambda host: [PUBLIC_IP])


def _deps(tmp_path: Path) -> Deps:
    return Deps(
        provider=FixtureTranscriptProvider(),
        llm=MockLLMClient(),
        composer=MockScriptComposer(),
        renderer=MockAudioRenderer(out_dir=tmp_path / "artifacts"),
    )


def _client(tmp_path: Path, directory: PodcastDirectory | None = None):  # type: ignore[no-untyped-def]
    db = tmp_path / "chorus.db"
    app = create_app(
        SqliteJobStore(db),
        _deps(tmp_path),
        api_token="master-token",
        key_store=SqliteKeyStore(db),
        email_sender=MockEmailSender(),
        subscription_store=SqliteSubscriptionStore(db),
    )
    return TestClient(app)


# --- auth ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("GET", "/podcasts/search?q=acquired", None),
        ("POST", "/podcasts/resolve", {"url": FEED}),
        ("POST", "/podcasts/import-opml", {"opml": "<opml/>"}),
        ("POST", "/souls/interview", {"answers": {"identity": "x"}}),
        ("POST", "/subscriptions/preview", {"sources": [{"kind": "rss", "feed_url": FEED}]}),
    ],
)
def test_new_endpoints_require_a_bearer_token(
    tmp_path: Path, method: str, path: str, body: dict | None
) -> None:
    client = _client(tmp_path)
    assert client.request(method, path, json=body).status_code == 401
    bad = client.request(method, path, json=body, headers={"Authorization": "Bearer nope"})
    assert bad.status_code == 401


def test_any_valid_issued_key_can_use_them(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    web = install_fake_web(monkeypatch)
    web.set(_search_url("acquired"), json.dumps(ITUNES_ACQUIRED))
    client = _client(tmp_path)
    token = SqliteKeyStore(tmp_path / "chorus.db").issue("someone@example.com")
    r = client.get("/podcasts/search?q=acquired", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200


# --- search ---------------------------------------------------------------------------------


def test_search_maps_itunes_results_and_drops_feedless_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    web = install_fake_web(monkeypatch)
    web.set(_search_url("acquired"), json.dumps(ITUNES_ACQUIRED))
    client = _client(tmp_path)

    r = client.get("/podcasts/search", params={"q": "Acquired"}, headers=MASTER_HEADERS)

    assert r.status_code == 200, r.text
    assert r.json() == [
        {
            "title": "Acquired",
            "author": "Ben Gilbert and David Rosenthal",
            "feed_url": FEED,
            "artwork_url": "https://img.example.com/acquired600.jpg",
            "apple_id": 1050462261,
        }
    ]


def test_search_is_cached_by_normalized_query(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    web = install_fake_web(monkeypatch)
    web.set(_search_url("acquired"), json.dumps(ITUNES_ACQUIRED))
    client = _client(tmp_path)

    for q in ("Acquired", "  acquired  ", "ACQUIRED"):
        assert client.get("/podcasts/search", params={"q": q}, headers=MASTER_HEADERS).status_code == 200
    # A different `limit` is served from the same cached upstream page.
    client.get("/podcasts/search", params={"q": "acquired", "limit": 1}, headers=MASTER_HEADERS)

    assert web.calls == [_search_url("acquired")]


def test_search_cache_expires_after_the_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    web = install_fake_web(monkeypatch)
    web.set(_search_url("acquired"), json.dumps(ITUNES_ACQUIRED))
    clock = {"now": 1_000.0}
    directory = PodcastDirectory(clock=lambda: clock["now"], cache_ttl_s=60.0)

    directory.search("acquired")
    clock["now"] += 59
    directory.search("acquired")
    assert len(web.calls) == 1
    clock["now"] += 2
    directory.search("acquired")
    assert len(web.calls) == 2


def test_upstream_calls_are_throttled_below_apples_ceiling(monkeypatch: pytest.MonkeyPatch) -> None:
    web = install_fake_web(monkeypatch)
    for i in range(5):
        web.set(_search_url(f"show {i}"), json.dumps({"results": []}))
    clock = {"now": 0.0}
    directory = PodcastDirectory(clock=lambda: clock["now"], max_calls_per_minute=3)

    for i in range(3):
        directory.search(f"show {i}")
    with pytest.raises(PodcastError) as excinfo:
        directory.search("show 3")
    assert excinfo.value.status_code == 429
    directory.search("show 0")  # cached: costs no upstream call
    clock["now"] += 61
    directory.search("show 3")  # window slid


def test_search_limit_and_validation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    web = install_fake_web(monkeypatch)
    many = {"results": [
        {"collectionId": i, "collectionName": f"Show {i}", "feedUrl": f"https://f.example.com/{i}"}
        for i in range(30)
    ]}
    web.set(_search_url("show"), json.dumps(many))
    client = _client(tmp_path)

    assert len(client.get("/podcasts/search?q=show", headers=MASTER_HEADERS).json()) == 10  # default
    assert len(client.get("/podcasts/search?q=show&limit=3", headers=MASTER_HEADERS).json()) == 3
    assert len(client.get("/podcasts/search?q=show&limit=25", headers=MASTER_HEADERS).json()) == 25
    assert client.get("/podcasts/search?q=show&limit=26", headers=MASTER_HEADERS).status_code == 422
    assert client.get("/podcasts/search?q=show&limit=0", headers=MASTER_HEADERS).status_code == 422
    assert client.get("/podcasts/search", headers=MASTER_HEADERS).status_code == 422
    assert client.get("/podcasts/search?q=", headers=MASTER_HEADERS).status_code == 422
    assert client.get("/podcasts/search?q=" + "x" * 201, headers=MASTER_HEADERS).status_code == 422


def test_search_upstream_failure_is_a_502(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    web = install_fake_web(monkeypatch)
    web.set(_search_url("down"), "oops", status=503)
    client = _client(tmp_path)
    r = client.get("/podcasts/search?q=down", headers=MASTER_HEADERS)
    assert r.status_code == 502 and "Apple" in r.json()["detail"]

    web.set(_search_url("garbage"), "<html>not json</html>")
    assert client.get("/podcasts/search?q=garbage", headers=MASTER_HEADERS).status_code == 502


# --- resolve ----------------------------------------------------------------------------------


def _resolve(client: TestClient, url: str):  # type: ignore[no-untyped-def]
    return client.post("/podcasts/resolve", json={"url": url}, headers=MASTER_HEADERS)


def test_resolve_direct_rss_url_confirms_the_feed_and_fills_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    web = install_fake_web(monkeypatch)
    web.set(FEED, rss_feed("Acquired", [rss_item("E", pub=datetime.now(UTC), guid="g")], image="https://img/a.jpg"))
    r = _resolve(_client(tmp_path), FEED)
    assert r.status_code == 200, r.text
    assert r.json() == {"kind": "rss", "feed_url": FEED, "title": "Acquired", "artwork_url": "https://img/a.jpg"}


def test_resolve_feed_scheme_links_are_rewritten_to_https(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    web = install_fake_web(monkeypatch)
    web.set(FEED, rss_feed("Acquired", []))
    r = _resolve(_client(tmp_path), FEED.replace("https://", "feed://"))
    assert r.status_code == 200 and r.json()["feed_url"] == FEED


def test_resolve_apple_podcasts_url_uses_the_lookup_api(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    web = install_fake_web(monkeypatch)
    web.set(_lookup_url("1050462261"), json.dumps(ITUNES_ACQUIRED))
    r = _resolve(_client(tmp_path), "https://podcasts.apple.com/us/podcast/acquired/id1050462261?i=1000")
    assert r.status_code == 200, r.text
    assert r.json() == {
        "kind": "rss",
        "feed_url": FEED,
        "title": "Acquired",
        "artwork_url": "https://img.example.com/acquired600.jpg",
    }
    assert web.calls == [_lookup_url("1050462261")]  # only the lookup; the feed itself is not fetched


def test_resolve_apple_url_without_a_result_is_422(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    web = install_fake_web(monkeypatch)
    web.set(_lookup_url("999"), json.dumps({"resultCount": 0, "results": []}))
    client = _client(tmp_path)
    assert _resolve(client, "https://podcasts.apple.com/us/podcast/x/id999").status_code == 422
    # An Apple URL that is not a show page never triggers a lookup at all.
    r = _resolve(client, "https://podcasts.apple.com/us/browse")
    assert r.status_code == 422 and "id<number>" in r.json()["detail"]


def test_resolve_youtube_channel_url_is_direct_and_titles_from_the_atom_feed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    web = install_fake_web(monkeypatch)
    web.set(YT_FEED, youtube_feed("Lex Clips", [youtube_entry("AAAAAAAAAAA", "t", "2026-01-01T00:00:00+00:00")]))
    client = _client(tmp_path)
    for url in (
        f"https://www.youtube.com/channel/{CHANNEL}",
        f"https://youtube.com/channel/{CHANNEL}/videos",
        YT_FEED,
    ):
        r = _resolve(client, url)
        assert r.status_code == 200, (url, r.text)
        assert r.json() == {"kind": "youtube", "channel_id": CHANNEL, "title": "Lex Clips"}
    assert not any("googleapis" in c for c in web.calls)  # no API call for the direct form


def test_resolve_youtube_channel_that_does_not_exist_is_422(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    web = install_fake_web(monkeypatch)
    web.set(YT_FEED, "nope", status=404)
    r = _resolve(_client(tmp_path), f"https://www.youtube.com/channel/{CHANNEL}")
    assert r.status_code == 422 and "not found" in r.json()["detail"]


def test_resolve_youtube_channel_survives_a_failed_title_lookup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_fake_web(monkeypatch)  # no route for the Atom feed: a transport failure
    r = _resolve(_client(tmp_path), f"https://www.youtube.com/channel/{CHANNEL}")
    assert r.status_code == 200 and r.json() == {"kind": "youtube", "channel_id": CHANNEL, "title": None}


@pytest.mark.parametrize(
    "url", ["https://www.youtube.com/@lexfridman", "https://www.youtube.com/c/LexFridman", "https://www.youtube.com/user/lexfridman"]
)
def test_resolve_youtube_handle_forms_without_an_api_key_explain_what_to_paste(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, url: str
) -> None:
    web = install_fake_web(monkeypatch)
    r = _resolve(_client(tmp_path), url)
    assert r.status_code == 422
    assert "/channel/UC" in r.json()["detail"]
    assert web.calls == []  # never scrapes the page, never calls anything


@pytest.mark.parametrize(
    ("url", "parameter", "value"),
    [
        ("https://www.youtube.com/@lexfridman", "forHandle", "@lexfridman"),
        ("https://www.youtube.com/@lexfridman/videos", "forHandle", "@lexfridman"),
        ("https://www.youtube.com/c/LexFridman", "forHandle", "LexFridman"),
        ("https://www.youtube.com/user/lexfridman", "forUsername", "lexfridman"),
    ],
)
def test_resolve_youtube_handle_forms_with_an_api_key_use_channels_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, url: str, parameter: str, value: str
) -> None:
    monkeypatch.setenv("YOUTUBE_API_KEY", "test-api-key")
    web = install_fake_web(monkeypatch)
    web.set(
        _channels_url(parameter, value),
        json.dumps({"items": [{"id": CHANNEL, "snippet": {"title": "Lex Fridman"}}]}),
    )
    r = _resolve(_client(tmp_path), url)
    assert r.status_code == 200, r.text
    assert r.json() == {"kind": "youtube", "channel_id": CHANNEL, "title": "Lex Fridman"}
    # The key travels in a header, never in the URL (so it cannot reach a log line).
    assert web.call_kwargs[0]["headers"] == {"x-goog-api-key": "test-api-key"}
    assert "test-api-key" not in web.calls[0]


def test_resolve_youtube_handle_not_found_and_api_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("YOUTUBE_API_KEY", "k")
    web = install_fake_web(monkeypatch)
    client = _client(tmp_path)
    web.set(_channels_url("forHandle", "@ghost"), json.dumps({"pageInfo": {"totalResults": 0}}))
    assert _resolve(client, "https://www.youtube.com/@ghost").status_code == 422
    web.set(_channels_url("forHandle", "@quota"), json.dumps({"error": {}}), status=403)
    assert _resolve(client, "https://www.youtube.com/@quota").status_code == 502


@pytest.mark.parametrize(
    "url",
    [
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "https://www.youtube.com/playlist?list=PL123",
        "https://www.youtube.com/channel/not-a-real-id",
        "https://youtu.be/dQw4w9WgXcQ",  # not a recognised host, fetched as a feed and rejected
    ],
)
def test_resolve_non_channel_youtube_urls_are_422(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, url: str
) -> None:
    install_fake_web(monkeypatch)
    assert _resolve(_client(tmp_path), url).status_code == 422


@pytest.mark.parametrize("url", ["not a url", "ftp://example.com/feed", "mailto:a@b.co", "https:///nohost", "   "])
def test_resolve_unrecognized_input_is_422(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, url: str) -> None:
    install_fake_web(monkeypatch)
    r = _resolve(_client(tmp_path), url)
    assert r.status_code == 422, r.text


def test_resolve_html_page_is_422_with_a_clear_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    web = install_fake_web(monkeypatch)
    web.set("https://example.com/podcast", "<html><body><h1>Podcast</h1></body></html>")
    r = _resolve(_client(tmp_path), "https://example.com/podcast")
    assert r.status_code == 422
    assert "RSS" in r.json()["detail"]


def test_resolve_refuses_private_addresses_before_fetching(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    web = install_fake_web(monkeypatch)
    monkeypatch.setattr(netguard, "_default_resolver", lambda host: ["10.1.2.3"])
    client = _client(tmp_path)
    for url in ("https://intranet.example.com/feed", "https://127.0.0.1/feed", "https://localhost/feed"):
        r = _resolve(client, url)
        assert r.status_code == 422, url
        assert "cannot be fetched" in r.json()["detail"]
    assert web.calls == []


def test_resolve_uses_an_injected_resolver(monkeypatch: pytest.MonkeyPatch) -> None:
    web = install_fake_web(monkeypatch)
    web.set(FEED, rss_feed("Acquired", []))
    seen: list[str] = []

    def resolver(host: str) -> list[str]:
        seen.append(host)
        return ["10.0.0.9"]

    with pytest.raises(PodcastError) as excinfo:
        PodcastDirectory(resolver=resolver).resolve(FEED)
    assert excinfo.value.status_code == 422 and seen == ["feeds.example.com"]


# --- OPML -------------------------------------------------------------------------------------------


OPML = """<?xml version="1.0" encoding="UTF-8"?>
<opml version="2.0">
  <head><title>Subscriptions</title></head>
  <body>
    <outline text="Business" title="Business">
      <outline type="rss" text="Acquired" xmlUrl="https://feeds.example.com/acquired.xml" htmlUrl="https://acquired.fm"/>
      <outline text="Nested folder">
        <outline type="rss" text="20VC" xmlUrl="https://feeds.example.com/20vc.xml"/>
        <outline type="rss" title="Title Attr Only" xmlUrl="feed://feeds.example.com/other.xml"/>
      </outline>
    </outline>
    <outline type="rss" text="Top level" xmlUrl="https://feeds.example.com/top.xml"/>
    <outline type="rss" text="Duplicate" xmlUrl="https://feeds.example.com/acquired.xml"/>
    <outline type="link" text="A bookmark" xmlUrl="https://example.com/page"/>
    <outline text="Leaf with no feed"/>
    <outline type="rss" text="Bad scheme" xmlUrl="ftp://example.com/feed"/>
    <outline type="rss" text="Not a url" xmlUrl="just some words"/>
  </body>
</opml>"""


def test_import_opml_walks_nested_outlines_and_reports_junk(tmp_path: Path) -> None:
    client = _client(tmp_path)
    r = client.post("/podcasts/import-opml", json={"opml": OPML}, headers=MASTER_HEADERS)
    assert r.status_code == 200, r.text
    body = r.json()

    assert [(s["kind"], s["title"], s["feed_url"]) for s in body["sources"]] == [
        ("rss", "Acquired", "https://feeds.example.com/acquired.xml"),
        ("rss", "20VC", "https://feeds.example.com/20vc.xml"),
        ("rss", "Title Attr Only", "https://feeds.example.com/other.xml"),
        ("rss", "Top level", "https://feeds.example.com/top.xml"),
    ]
    reasons = {s["reason"] for s in body["skipped"]}
    assert reasons == {
        "duplicate feed",
        "unsupported outline type 'link'",
        "no xmlUrl attribute",
        "xmlUrl is not a valid http(s) URL",
    }
    assert len(body["skipped"]) == 5  # duplicate, link, leaf, ftp, not-a-url
    assert all(entry["line"].startswith("<outline") for entry in body["skipped"])
    # Importing never creates a subscription.
    assert client.get("/subscriptions", headers=MASTER_HEADERS).json() == []


def test_import_opml_rejects_bad_documents(tmp_path: Path) -> None:
    client = _client(tmp_path)
    for bad in ("<opml><body>", "not xml at all\njust junk lines", "<rss><channel/></rss>"):
        r = client.post("/podcasts/import-opml", json={"opml": bad}, headers=MASTER_HEADERS)
        assert r.status_code == 422, bad
    entity = '<?xml version="1.0"?><!DOCTYPE o [<!ENTITY a "b">]><opml><body/></opml>'
    assert client.post("/podcasts/import-opml", json={"opml": entity}, headers=MASTER_HEADERS).status_code == 422


def test_import_opml_enforces_the_one_mebibyte_cap() -> None:
    with pytest.raises(PodcastError) as excinfo:
        parse_opml("<opml>" + " " * podcasts_api.OPML_MAX_BYTES + "</opml>")
    assert excinfo.value.status_code == 413


def test_import_opml_caps_the_number_of_sources() -> None:
    outlines = "".join(
        f'<outline type="rss" text="s{i}" xmlUrl="https://f.example.com/{i}"/>'
        for i in range(podcasts_api.OPML_MAX_SOURCES + 3)
    )
    result = parse_opml(f"<opml><body>{outlines}</body></opml>")
    assert len(result.sources) == podcasts_api.OPML_MAX_SOURCES
    assert len(result.skipped) == 3


def test_import_opml_accepts_a_declared_encoding() -> None:
    xml = '<?xml version="1.0" encoding="UTF-8"?><opml><body><outline type="rss" text="Café" xmlUrl="https://f.example.com/c"/></body></opml>'
    assert parse_opml(xml).sources[0].title == "Café"  # type: ignore[union-attr]


# --- souls ----------------------------------------------------------------------------------------------


def test_souls_interview_builds_a_soul(tmp_path: Path) -> None:
    client = _client(tmp_path)
    answers = {
        "identity": "A growth-stage VC partner",
        "interests": "unit economics, marketplaces",
        "triggers": "a founder contradicting their own metrics",
        "ignore": "crypto hype",
        "style": "empirical",
        "guidance": "Refuse rather than pad.",
    }
    r = client.post("/souls/interview", json={"answers": answers}, headers=MASTER_HEADERS)
    assert r.status_code == 200, r.text
    soul = r.json()["soul"]
    assert set(r.json()) == {"soul"}
    assert "A growth-stage VC partner" in soul
    assert "- unit economics" in soul and "- marketplaces" in soul
    assert "## Curation Guidance" in soul


def test_souls_interview_validation(tmp_path: Path) -> None:
    client = _client(tmp_path)
    assert client.post("/souls/interview", json={"answers": {}}, headers=MASTER_HEADERS).status_code == 422
    assert client.post("/souls/interview", json={"answers": {"identity": "  "}}, headers=MASTER_HEADERS).status_code == 422
    assert client.post("/souls/interview", json={"answers": {"hobby": "chess"}}, headers=MASTER_HEADERS).status_code == 422
    # Unknown keys alongside a real one are ignored rather than passed to a model.
    ok = client.post("/souls/interview", json={"answers": {"identity": "an analyst", "hobby": "chess"}}, headers=MASTER_HEADERS)
    assert ok.status_code == 200 and "chess" not in ok.json()["soul"]


# --- MCP tools ------------------------------------------------------------------------------------------------


def _tools(tmp_path: Path, directory: PodcastDirectory | None = None) -> tuple[ChorusTools, SqliteSubscriptionStore]:
    db = tmp_path / "mcp.db"
    subs = SqliteSubscriptionStore(db)
    return ChorusTools(SqliteJobStore(db), _deps(tmp_path), subscription_store=subs, podcasts=directory), subs


def _ctx_for(owner: str) -> object:
    request = SimpleNamespace(state=SimpleNamespace(owner=owner))
    return SimpleNamespace(request_context=SimpleNamespace(request=request))


def test_mcp_search_resolve_and_preview(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    web = install_fake_web(monkeypatch)
    now = datetime.now(UTC)
    web.set(_search_url("acquired"), json.dumps(ITUNES_ACQUIRED))
    web.set(FEED, rss_feed("Acquired", [rss_item("Fresh", pub=now - timedelta(days=1), guid="g1", audio="https://cdn/1.mp3")]))
    tools, _ = _tools(tmp_path)

    found = tools.search_podcasts("acquired", limit=5)
    assert [(r.title, r.feed_url, r.apple_id) for r in found] == [("Acquired", FEED, 1050462261)]

    source = tools.resolve_podcast(FEED)
    assert isinstance(source, RssSource) and source.title == "Acquired"

    preview = tools.preview_subscription([source])
    assert [e.title for e in preview.episodes] == ["Fresh"]
    assert preview.errors == []


def test_mcp_subscribe_list_update_unsubscribe(tmp_path: Path) -> None:
    tools, subs = _tools(tmp_path)
    source = RssSource(kind="rss", feed_url=FEED, title="Acquired")

    created = tools.subscribe(
        email="principal@example.com",
        soul="# soul",
        context="ctx",
        sources=[source],
        max_episodes_per_run=5,
    )
    assert created.owner == "master"
    assert created.cadence == "weekly" and created.highlight_count == 4
    assert created.max_episodes_per_run == 5
    assert created.sources == [source]
    assert subs.get(created.subscription_id) == created

    assert [s.subscription_id for s in tools.list_subscriptions()] == [created.subscription_id]

    updated = tools.update_subscription(
        created.subscription_id,
        context="fresh",
        active=False,
        notify_when_empty=False,
        sources=[YoutubeSource(kind="youtube", channel_id=CHANNEL)],
    )
    assert updated.context == "fresh" and updated.active is False and updated.notify_when_empty is False
    assert updated.sources == [YoutubeSource(kind="youtube", channel_id=CHANNEL, title=None)]
    assert updated.cadence == "weekly"  # untouched fields stay
    assert subs.get(created.subscription_id) == updated

    assert tools.unsubscribe(created.subscription_id) == {"unsubscribed": created.subscription_id}
    assert subs.get(created.subscription_id) is None
    assert tools.list_subscriptions() == []


def test_mcp_subscribe_accepts_plain_dict_sources_and_validates(tmp_path: Path) -> None:
    tools, _ = _tools(tmp_path)
    sub = tools.subscribe(
        email="principal@example.com",
        soul="# soul",
        context="",
        sources=[{"kind": "rss", "feed_url": FEED}],  # type: ignore[list-item]
    )
    assert sub.sources is not None and sub.sources[0].kind == "rss"
    with pytest.raises(ValueError):
        tools.subscribe(email="principal@example.com", soul="# soul", context="", sources=[])
    with pytest.raises(ValueError):
        tools.subscribe(email="not-an-email", soul="# soul", context="", sources=[{"kind": "rss", "feed_url": FEED}])  # type: ignore[list-item]


def test_mcp_subscription_tools_are_owner_scoped(tmp_path: Path) -> None:
    tools, _ = _tools(tmp_path)
    mine = _ctx_for("a@example.com")
    theirs = _ctx_for("b@example.com")
    source = RssSource(kind="rss", feed_url=FEED)

    created = tools.subscribe(email="a@example.com", soul="# s", context="", sources=[source], ctx=mine)  # type: ignore[arg-type]
    assert created.owner == "a@example.com"

    assert tools.list_subscriptions(ctx=theirs) == []  # type: ignore[arg-type]
    assert [s.subscription_id for s in tools.list_subscriptions(ctx=mine)] == [created.subscription_id]  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="unknown subscription_id"):
        tools.update_subscription(created.subscription_id, active=False, ctx=theirs)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="unknown subscription_id"):
        tools.unsubscribe(created.subscription_id, ctx=theirs)  # type: ignore[arg-type]
    assert len(tools.list_subscriptions()) == 1  # the master (stdio) sees everything
    assert tools.unsubscribe(created.subscription_id, ctx=mine)  # type: ignore[arg-type]


def test_mcp_unknown_subscription_id_is_an_error(tmp_path: Path) -> None:
    tools, _ = _tools(tmp_path)
    with pytest.raises(ValueError, match="unknown subscription_id"):
        tools.update_subscription("nope", active=False)
    with pytest.raises(ValueError, match="unknown subscription_id"):
        tools.unsubscribe("nope")


def test_mcp_tools_are_registered_with_the_server_and_blocking_ones_run_off_the_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    web = install_fake_web(monkeypatch)
    web.set(_search_url("acquired"), json.dumps(ITUNES_ACQUIRED))
    db = tmp_path / "srv.db"
    server, _ = create_mcp_server(SqliteJobStore(db), _deps(tmp_path), subscription_store=SqliteSubscriptionStore(db))

    names = {t.name for t in asyncio.run(server.list_tools())}
    assert {
        "search_podcasts",
        "resolve_podcast",
        "preview_subscription",
        "subscribe",
        "list_subscriptions",
        "update_subscription",
        "unsubscribe",
    } <= names

    schemas = {t.name: t.inputSchema for t in asyncio.run(server.list_tools())}
    assert "ctx" not in schemas["subscribe"]["properties"]
    assert {"query", "limit"} == set(schemas["search_podcasts"]["properties"])

    result = asyncio.run(server.call_tool("search_podcasts", {"query": "acquired"}))
    assert "Acquired" in json.dumps(result, default=str)


def test_mcp_tools_share_the_apps_subscription_store(tmp_path: Path) -> None:
    db = tmp_path / "chorus.db"
    subs = SqliteSubscriptionStore(db)
    app = create_app(
        SqliteJobStore(db),
        _deps(tmp_path),
        api_token="master-token",
        key_store=SqliteKeyStore(db),
        email_sender=MockEmailSender(),
        subscription_store=subs,
    )
    tools: ChorusTools = app.state.chorus_mcp_tools
    created = tools.subscribe(email="p@example.com", soul="# s", context="", sources=[RssSource(kind="rss", feed_url=FEED)])
    client = TestClient(app)
    listed = client.get("/subscriptions", headers=MASTER_HEADERS).json()
    assert [s["subscription_id"] for s in listed] == [created.subscription_id]
