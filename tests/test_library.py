"""Library import (chorus.library, chorus.library_resolve, chorus.saved_items,
chorus.library_api): pure ranking and parsing, the saved-item store, Apple /
feed resolution against a faked web, the HTTP and MCP surfaces, and a
subscription run that draws from the saved queue.

No network: `chorus.feeds._fetch_bounded` is faked (tests/feedfakes.py) and
netguard's DNS is stubbed.
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
from chorus.artifacts import LocalArtifactStore
from chorus.audio import MockAudioRenderer
from chorus.email import MockEmailSender
from chorus.feeds import gather_episodes
from chorus.jobs import SqliteJobStore
from chorus.keys import SqliteKeyStore
from chorus.library import (
    LibraryImport,
    LibraryItem,
    SavedItem,
    corpus_texts,
    merge_item,
    parse_apple_url,
    rank_show_suggestions,
    save_weight,
    saved_queue_episodes,
    titles_match,
)
from chorus.library_api import LibraryService
from chorus.library_resolve import THROTTLED_REASON, LibraryResolver
from chorus.llm import MockLLMClient
from chorus.mcp_server import ChorusTools, create_mcp_server
from chorus.models import EpisodeInput, Transcript
from chorus.pipeline import Deps
from chorus.podcasts_api import PodcastDirectory
from chorus.saved_items import SqliteSavedItemStore, saved_queue_lister
from chorus.scheduler import run_subscription
from chorus.script import MockScriptComposer
from chorus.subscriptions import (
    RssSource,
    SavedQueueSource,
    SqliteSubscriptionStore,
    Subscription,
)
from tests.feedfakes import FakeWeb, install_fake_web, rss_feed, rss_item

FIX = Path(__file__).resolve().parent.parent / "fixtures"
SOUL = (FIX / "souls" / "soul_investor.md").read_text(encoding="utf-8")
SAMPLE_SEGMENTS = json.loads(
    (FIX / "transcripts" / "sample_public.json").read_text(encoding="utf-8")
)["segments"]
MASTER_HEADERS = {"Authorization": "Bearer master-token"}
PUBLIC_IP = "93.184.216.34"
NOW = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)

SHOW_A_ID = "1516093381"
SHOW_B_ID = "1154105909"
FEED_A = "https://feeds.example.com/deep-talk.xml"
FEED_B = "https://feeds.example.com/invest.xml"


def apple_url(show_id: str, episode_id: str | None = None) -> str:
    base = f"https://podcasts.apple.com/us/podcast/some-show/id{show_id}"
    return f"{base}?i={episode_id}" if episode_id else base


def episode_lookup_url(show_id: str) -> str:
    query = urlencode(
        {
            "id": show_id,
            "entity": "podcastEpisode",
            "limit": podcasts_api.ITUNES_EPISODE_LOOKUP_LIMIT,
        }
    )
    return f"{podcasts_api.ITUNES_LOOKUP_URL}?{query}"


def search_url(term: str) -> str:
    query = urlencode({"media": "podcast", "entity": "podcast", "term": term, "limit": 25})
    return f"{podcasts_api.ITUNES_SEARCH_URL}?{query}"


def lookup_body(
    show_id: str, title: str, feed: str, episodes: list[tuple[str, str, str | None]]
) -> str:
    """An iTunes `entity=podcastEpisode` lookup: the show, then (track_id,
    title, guid) episodes."""
    results: list[dict[str, object]] = [
        {
            "wrapperType": "track",
            "kind": "podcast",
            "collectionId": int(show_id),
            "collectionName": title,
            "artistName": "Host",
            "feedUrl": feed,
        }
    ]
    for track_id, ep_title, guid in episodes:
        entry: dict[str, object] = {
            "wrapperType": "podcastEpisode",
            "kind": "podcast-episode",
            "trackId": int(track_id),
            "trackName": ep_title,
            "episodeUrl": f"https://audio.example.com/{track_id}.mp3",
        }
        if guid is not None:
            entry["episodeGuid"] = guid
        results.append(entry)
    return json.dumps({"resultCount": len(results), "results": results})


def saved_episode(
    show_id: str,
    episode_id: str,
    title: str,
    *,
    show: str,
    days_ago: float,
    provider: str = "readwise",
    consumed: bool | None = None,
    tags: list[str] | None = None,
) -> LibraryItem:
    return LibraryItem.model_validate(
        {
            "provider": provider,
            "item_kind": "episode",
            "title": title,
            "show_title": show,
            "url": apple_url(show_id, episode_id),
            "saved_at": (NOW - timedelta(days=days_ago)).isoformat(),
            "consumed": consumed,
            "tags": tags or [],
        }
    )


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("CHORUS_UNSUBSCRIBE_SECRET", "test-unsubscribe-secret")
    for name in ("CHORUS_MAX_JOBS_PER_DAY", "CHORUS_MAX_INFLIGHT_JOBS", "CHORUS_VIEWER_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(netguard, "_default_resolver", lambda host: [PUBLIC_IP])


# --- pure: parsing ------------------------------------------------------------------


def test_parse_apple_url_reads_show_and_episode_ids() -> None:
    ref = parse_apple_url(apple_url(SHOW_A_ID, "1000792593373"))
    assert ref is not None and (ref.show_id, ref.episode_id) == (SHOW_A_ID, "1000792593373")
    show_only = parse_apple_url(apple_url(SHOW_A_ID))
    assert show_only is not None and show_only.episode_id is None


@pytest.mark.parametrize(
    "url",
    [
        "https://open.spotify.com/episode/abc",
        "https://podcasts.apple.com/us/podcast/no-id-here",
        "ftp://podcasts.apple.com/us/podcast/x/id123",
        "not a url",
    ],
)
def test_parse_apple_url_rejects_other_links(url: str) -> None:
    assert parse_apple_url(url) is None


def test_parse_apple_url_ignores_a_non_numeric_episode_param() -> None:
    ref = parse_apple_url(f"{apple_url(SHOW_A_ID)}?i=abc")
    assert ref is not None and ref.episode_id is None


def test_library_item_reads_apple_ids_from_its_url() -> None:
    item = saved_episode(SHOW_A_ID, "42", "Ep", show="Deep Talk", days_ago=1)
    assert (item.apple_show_id, item.apple_episode_id) == (SHOW_A_ID, "42")


def test_library_item_rejects_a_naive_saved_at() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        naive = datetime(2026, 1, 1)  # noqa: DTZ001 - naive on purpose
        LibraryItem(provider="pushed", item_kind="episode", title="x", saved_at=naive)


def test_library_item_trims_and_dedupes_tags() -> None:
    item = LibraryItem(
        provider="pushed", item_kind="episode", title="x", tags=[" ai ", "ai", "", "macro"]
    )
    assert item.tags == ["ai", "macro"]


def test_item_key_is_provider_independent_for_the_same_apple_episode() -> None:
    a = saved_episode(SHOW_A_ID, "42", "Ep", show="Deep Talk", days_ago=1, provider="readwise")
    b = saved_episode(
        SHOW_A_ID, "42", "Ep (renamed)", show="Deep Talk", days_ago=2, provider="pushed"
    )
    assert a.item_key() == b.item_key() == "apple:42"


def test_titles_match_folds_punctuation_and_allows_long_containment() -> None:
    assert titles_match("Noam Brown – Agent swarms!", "noam brown agent swarms")
    assert titles_match(
        "Michael Moritz - Lessons From 40 Years of Investing",
        "Michael Moritz - Lessons From 40 Years of Investing - [Invest Like the Best, EP.491]",
    )
    assert not titles_match(
        "Episode 1", "Episode 12: the sequel"
    )  # short containment is not enough
    assert not titles_match("", "anything")


# --- pure: ranking --------------------------------------------------------------


def test_save_weight_halves_every_half_life() -> None:
    assert save_weight(NOW, NOW, 30.0) == pytest.approx(1.0)
    assert save_weight(NOW - timedelta(days=30), NOW, 30.0) == pytest.approx(0.5)
    assert save_weight(NOW - timedelta(days=60), NOW, 30.0) == pytest.approx(0.25)
    assert save_weight(NOW + timedelta(days=5), NOW, 30.0) == pytest.approx(1.0)
    assert save_weight(None, NOW, 30.0) == pytest.approx(0.5)


def test_rank_show_suggestions_matches_hand_computed_scores() -> None:
    items = [
        # Show A: saves today and 30 days ago -> 1.0 + 0.5 = 1.5
        saved_episode(SHOW_A_ID, "1", "A1", show="Deep Talk", days_ago=0),
        saved_episode(SHOW_A_ID, "2", "A2", show="Deep Talk", days_ago=30),
        # Show B: three saves 60 days ago -> 3 * 0.25 = 0.75
        saved_episode(SHOW_B_ID, "3", "B1", show="Invest", days_ago=60),
        saved_episode(SHOW_B_ID, "4", "B2", show="Invest", days_ago=60),
        saved_episode(SHOW_B_ID, "5", "B3", show="Invest", days_ago=60),
        # Show C: one save -> below MIN_SAVES_TO_SUGGEST, dropped
        saved_episode("999", "6", "C1", show="Once", days_ago=0),
        # Show D: followed in another app -> explicit, 1.0
        LibraryItem(
            provider="opml",
            item_kind="show",
            title="Followed Show",
            feed_url="https://f.example.com/d.xml",
        ),
        # Documents never count
        LibraryItem(provider="instapaper", item_kind="document", title="An article"),
    ]
    ranked = rank_show_suggestions(items, NOW, subscribed_feeds=["https://F.example.com/d.xml"])
    assert [(s.title, s.save_count, s.score) for s in ranked] == [
        ("Deep Talk", 2, 1.5),
        ("Followed Show", 0, 1.0),
        ("Invest", 3, 0.75),
    ]
    followed = ranked[1]
    assert followed.explicit and followed.already_subscribed
    assert ranked[0].last_saved_at == NOW


def test_rank_show_suggestions_attaches_resolved_sources() -> None:
    items = [
        saved_episode(SHOW_A_ID, "1", "A1", show="Deep Talk", days_ago=0),
        saved_episode(SHOW_A_ID, "2", "A2", show="Deep Talk", days_ago=1),
    ]
    source = RssSource(kind="rss", feed_url=FEED_A, title="Deep Talk")
    [suggestion] = rank_show_suggestions(
        items, NOW, sources={f"apple:{SHOW_A_ID}": source}, subscribed_feeds=[FEED_A]
    )
    assert suggestion.source == source and suggestion.already_subscribed


def test_a_resolved_feed_title_beats_the_saved_publisher_name() -> None:
    # Reader podcast saves carry the publisher in `author`, not the show name.
    items = [
        saved_episode(SHOW_B_ID, "1", "B1", show="Colossus | Investing Podcasts", days_ago=0),
        saved_episode(SHOW_B_ID, "2", "B2", show="Colossus | Investing Podcasts", days_ago=1),
    ]
    source = RssSource(kind="rss", feed_url=FEED_B, title="Invest Like the Best")
    [suggestion] = rank_show_suggestions(items, NOW, sources={f"apple:{SHOW_B_ID}": source})
    assert suggestion.title == "Invest Like the Best"


def test_rank_show_suggestions_requires_an_aware_now() -> None:
    with pytest.raises(ValueError):
        rank_show_suggestions([], datetime(2026, 1, 1))  # noqa: DTZ001 - naive on purpose


# --- pure: merge, queue, corpus -----------------------------------------------------


def _resolved(item: LibraryItem, feed: str = FEED_A, guid: str | None = None) -> SavedItem:
    entry = merge_item(None, item, "master", NOW)
    return entry.model_copy(
        update={
            "status": "resolved",
            "episode": EpisodeInput(feed_url=feed, guid=guid or item.item_key(), title=item.title),
            "show_source": RssSource(kind="rss", feed_url=feed),
        }
    )


def test_merge_item_keeps_resolution_and_takes_new_flags() -> None:
    item = saved_episode(SHOW_A_ID, "1", "A1", show="Deep Talk", days_ago=1)
    stored = _resolved(item)
    replayed = item.model_copy(update={"consumed": True, "tags": ["ai"]})
    merged = merge_item(stored, replayed, "master", NOW + timedelta(hours=1))
    assert merged.status == "resolved" and merged.episode == stored.episode
    assert merged.item.consumed is True and merged.item.tags == ["ai"]
    assert merged.imported_at == NOW and merged.updated_at == NOW + timedelta(hours=1)


def test_merge_item_retries_unresolved_and_resets_on_new_identity() -> None:
    item = saved_episode(SHOW_A_ID, "1", "A1", show="Deep Talk", days_ago=1)
    failed = merge_item(None, item, "master", NOW).model_copy(
        update={"status": "unresolved", "reason": "x"}
    )
    assert merge_item(failed, item, "master", NOW).status == "pending"
    retitled = item.model_copy(update={"title": "A completely different title"})
    assert merge_item(_resolved(item), retitled, "master", NOW).status == "pending"


def test_documents_are_stored_as_corpus() -> None:
    doc = LibraryItem(provider="instapaper", item_kind="document", title="Essay")
    assert merge_item(None, doc, "master", NOW).status == "corpus"


def test_saved_queue_takes_resolved_unheard_recent_saves_newest_first() -> None:
    fresh = _resolved(saved_episode(SHOW_A_ID, "1", "Fresh", show="Deep Talk", days_ago=1))
    older = _resolved(saved_episode(SHOW_A_ID, "2", "Older", show="Deep Talk", days_ago=10))
    heard = _resolved(
        saved_episode(SHOW_A_ID, "3", "Heard", show="Deep Talk", days_ago=2, consumed=True)
    )
    stale = _resolved(saved_episode(SHOW_A_ID, "4", "Stale", show="Deep Talk", days_ago=400))
    spotify = _resolved(
        saved_episode(SHOW_A_ID, "5", "Spot", show="Deep Talk", days_ago=3, provider="spotify")
    )
    pending = merge_item(
        None, saved_episode(SHOW_A_ID, "6", "Pending", show="X", days_ago=0), "master", NOW
    )
    entries = [older, heard, stale, spotify, pending, fresh]

    queue = SavedQueueSource(kind="saved")
    titles = [e.title for e in saved_queue_episodes(entries, queue, NOW, 10)]
    assert titles == ["Fresh", "Spot", "Older"]

    only_readwise = SavedQueueSource(kind="saved", providers=["readwise"])
    assert [e.title for e in saved_queue_episodes(entries, only_readwise, NOW, 10)] == [
        "Fresh",
        "Older",
    ]

    assert fresh.episode is not None
    excluded = frozenset({fresh.episode.resolved_id()})
    assert [e.title for e in saved_queue_episodes(entries, queue, NOW, 1, excluded)] == ["Spot"]
    [first] = saved_queue_episodes(entries, queue, NOW, 1)
    assert first.published_at == NOW - timedelta(days=1) and first.source_title == "Deep Talk"


def test_corpus_texts_include_show_tags_notes_and_highlights() -> None:
    item = LibraryItem(
        provider="readwise",
        item_kind="episode",
        title="Small funds vs multi-managers",
        show_title="Odds On Open",
        tags=["investing", "ai"],
        notes="Revisit the sizing argument",
        highlights=["Edge decays when capital floods in"],
        saved_at=NOW,
    )
    older = LibraryItem(
        provider="readwise", item_kind="document", title="Older", saved_at=NOW - timedelta(days=9)
    )
    texts = corpus_texts([older, item])
    assert texts[0] == (
        "Small funds vs multi-managers (Odds On Open)\nTags: investing, ai\n"
        "Notes: Revisit the sizing argument\nHighlight: Edge decays when capital floods in"
    )
    assert texts[1] == "Older"


# --- store ----------------------------------------------------------------------------


def test_sqlite_saved_item_store_upserts_lists_and_scopes_by_owner(tmp_path: Path) -> None:
    store = SqliteSavedItemStore(tmp_path / "saved.db")
    a = merge_item(
        None, saved_episode(SHOW_A_ID, "1", "A", show="S", days_ago=5), "alice@example.com", NOW
    )
    b = merge_item(
        None, saved_episode(SHOW_A_ID, "2", "B", show="S", days_ago=1), "alice@example.com", NOW
    )
    undated = merge_item(
        None,
        LibraryItem(provider="pushed", item_kind="episode", title="U"),
        "alice@example.com",
        NOW,
    )
    other = merge_item(
        None, saved_episode(SHOW_A_ID, "1", "A", show="S", days_ago=5), "bob@example.com", NOW
    )
    store.put_many([a, b, undated, other])
    store.put_many([a.model_copy(update={"status": "unresolved"})])  # upsert, not a duplicate

    listed = store.list("alice@example.com")
    assert [i.item.title for i in listed] == ["B", "A", "U"]
    assert [i.item.title for i in store.list("alice@example.com", status="unresolved")] == ["A"]
    assert len(store.list("alice@example.com", limit=1)) == 1
    found = store.get_many("alice@example.com", [a.key, "missing"])
    assert list(found) == [a.key] and found[a.key].status == "unresolved"
    assert [i.owner for i in store.list("bob@example.com")] == ["bob@example.com"]
    assert store.get_many("alice@example.com", []) == {}
    store.close()


# --- resolution -----------------------------------------------------------------------


def _directory() -> PodcastDirectory:
    return PodcastDirectory(clock=lambda: 0.0)


def _pending(*items: LibraryItem) -> list[SavedItem]:
    return [merge_item(None, item, "master", NOW) for item in items]


def test_apple_episodes_resolve_through_one_lookup_per_show(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    web = install_fake_web(monkeypatch)
    web.set(
        episode_lookup_url(SHOW_A_ID),
        lookup_body(
            SHOW_A_ID,
            "Deep Talk",
            FEED_A,
            [("11", "First", "guid-11"), ("12", "Second", "guid-12")],
        ),
    )
    entries = _pending(
        saved_episode(SHOW_A_ID, "11", "First", show="Deep Talk", days_ago=1),
        saved_episode(SHOW_A_ID, "12", "Second", show="Deep Talk", days_ago=2),
    )
    resolved = LibraryResolver(_directory()).resolve(entries)
    assert [r.status for r in resolved] == ["resolved", "resolved"]
    first = resolved[0].episode
    assert first is not None
    assert (first.feed_url, first.guid, first.audio_url) == (
        FEED_A,
        "guid-11",
        "https://audio.example.com/11.mp3",
    )
    assert first.show == "Deep Talk"
    assert resolved[0].show_source == RssSource(kind="rss", feed_url=FEED_A, title="Deep Talk")
    assert web.calls == [episode_lookup_url(SHOW_A_ID)]


def test_an_episode_missing_from_apples_listing_is_matched_in_the_feed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    web = install_fake_web(monkeypatch)
    web.set(episode_lookup_url(SHOW_A_ID), lookup_body(SHOW_A_ID, "Deep Talk", FEED_A, []))
    web.set(
        FEED_A,
        rss_feed(
            "Deep Talk",
            [
                rss_item("Unrelated", pub=NOW, guid="g-1"),
                rss_item("Ajeya Cotra – Inside the agent swarm", pub=NOW, guid="g-2"),
            ],
        ),
    )
    [entry] = _pending(
        saved_episode(
            SHOW_A_ID, "99", "Ajeya Cotra - Inside the agent swarm", show="Deep Talk", days_ago=1
        )
    )
    [resolved] = LibraryResolver(_directory()).resolve([entry])
    assert resolved.status == "resolved" and resolved.episode is not None
    assert (resolved.episode.feed_url, resolved.episode.guid) == (FEED_A, "g-2")


def test_an_item_that_names_its_feed_item_needs_no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    web = install_fake_web(monkeypatch)
    item = LibraryItem(
        provider="pushed",
        item_kind="episode",
        title="Direct",
        show_title="Beta",
        feed_url=FEED_B,
        guid="b-1",
    )
    [resolved] = LibraryResolver(_directory()).resolve(_pending(item))
    assert resolved.status == "resolved" and web.calls == []
    assert resolved.episode == EpisodeInput(
        feed_url=FEED_B, guid="b-1", show="Beta", title="Direct"
    )


def test_a_throttled_import_leaves_the_rest_pending(monkeypatch: pytest.MonkeyPatch) -> None:
    web = install_fake_web(monkeypatch)
    web.set(
        episode_lookup_url(SHOW_A_ID),
        lookup_body(SHOW_A_ID, "Deep Talk", FEED_A, [("11", "First", "g")]),
    )
    directory = PodcastDirectory(clock=lambda: 0.0, max_calls_per_minute=1)
    entries = _pending(
        saved_episode(SHOW_A_ID, "11", "First", show="Deep Talk", days_ago=1),
        saved_episode(SHOW_B_ID, "21", "Other", show="Invest", days_ago=1),
        saved_episode("777", "31", "Third", show="Third", days_ago=1),
    )
    resolved = LibraryResolver(directory).resolve(entries)
    assert [r.status for r in resolved] == ["resolved", "pending", "pending"]
    assert resolved[1].reason == THROTTLED_REASON
    assert web.calls == [episode_lookup_url(SHOW_A_ID)]  # nothing after the throttle


def test_a_title_only_save_finds_its_show_by_exact_title(monkeypatch: pytest.MonkeyPatch) -> None:
    web = install_fake_web(monkeypatch)
    web.set(
        search_url("odd lots"),
        json.dumps(
            {
                "results": [
                    {
                        "collectionName": "Odd Lots Fan Show",
                        "feedUrl": "https://f.example.com/fan.xml",
                    },
                    {"collectionName": "Odd Lots", "artistName": "Bloomberg", "feedUrl": FEED_B},
                ]
            }
        ),
    )
    web.set(
        FEED_B,
        rss_feed(
            "Odd Lots", [rss_item("How LA is becoming an industrial hub", pub=NOW, guid="ol-1")]
        ),
    )
    item = LibraryItem(
        provider="spotify",
        item_kind="episode",
        title="How LA Is Becoming an Industrial Hub",
        show_title="Odd Lots",
    )
    unknown = LibraryItem(
        provider="spotify", item_kind="episode", title="x", show_title="Nowhere Show"
    )
    web.set(
        search_url("nowhere show"),
        json.dumps({"results": [{"collectionName": "Somewhere", "feedUrl": FEED_A}]}),
    )
    found, missing = LibraryResolver(_directory()).resolve(_pending(item, unknown))
    assert found.status == "resolved" and found.episode is not None and found.episode.guid == "ol-1"
    assert missing.status == "unresolved" and missing.reason and "no podcast" in missing.reason


def test_a_show_item_resolves_to_its_source(monkeypatch: pytest.MonkeyPatch) -> None:
    web = install_fake_web(monkeypatch)
    web.set(episode_lookup_url(SHOW_A_ID), lookup_body(SHOW_A_ID, "Deep Talk", FEED_A, []))
    show = LibraryItem(
        provider="apple", item_kind="show", title="Deep Talk", url=apple_url(SHOW_A_ID)
    )
    [resolved] = LibraryResolver(_directory()).resolve(_pending(show))
    assert resolved.status == "resolved" and resolved.episode is None
    assert resolved.show_source is not None and resolved.show_source.feed_url == FEED_A


def test_a_show_without_a_public_feed_is_unresolved_with_a_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    web = install_fake_web(monkeypatch)
    web.set(episode_lookup_url(SHOW_A_ID), json.dumps({"results": []}))
    [entry] = _pending(saved_episode(SHOW_A_ID, "1", "x", show="Gone", days_ago=1))
    [resolved] = LibraryResolver(_directory()).resolve([entry])
    assert (
        resolved.status == "unresolved" and resolved.reason and "no public feed" in resolved.reason
    )


# --- service, HTTP, MCP -----------------------------------------------------------------


def _reader_library() -> list[dict[str, object]]:
    """What an agent pushes after reading Reader `category=podcast` documents."""
    return [
        saved_episode(
            SHOW_A_ID, "11", "First", show="Deep Talk", days_ago=1, tags=["ai"]
        ).model_dump(mode="json"),
        saved_episode(SHOW_A_ID, "12", "Second", show="Deep Talk", days_ago=3).model_dump(
            mode="json"
        ),
        saved_episode(SHOW_B_ID, "21", "Investing one", show="Invest", days_ago=2).model_dump(
            mode="json"
        ),
    ]


def _install_library_web(monkeypatch: pytest.MonkeyPatch) -> FakeWeb:
    web = install_fake_web(monkeypatch)
    web.set(
        episode_lookup_url(SHOW_A_ID),
        lookup_body(
            SHOW_A_ID, "Deep Talk", FEED_A, [("11", "First", "a-11"), ("12", "Second", "a-12")]
        ),
    )
    web.set(
        episode_lookup_url(SHOW_B_ID),
        lookup_body(SHOW_B_ID, "Invest", FEED_B, [("21", "Investing one", "b-21")]),
    )
    return web


def _client(tmp_path: Path) -> tuple[TestClient, Path]:
    db = tmp_path / "chorus.db"
    app = create_app(
        SqliteJobStore(db),
        _deps(tmp_path),
        api_token="master-token",
        key_store=SqliteKeyStore(db),
        email_sender=MockEmailSender(),
        subscription_store=SqliteSubscriptionStore(db),
        saved_item_store=SqliteSavedItemStore(db),
    )
    return TestClient(app), db


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("POST", "/library/import", {"items": []}),
        ("GET", "/library/items", None),
        ("POST", "/library/soul", None),
    ],
)
def test_library_routes_require_a_bearer_token(
    tmp_path: Path, method: str, path: str, body: object
) -> None:
    client, _ = _client(tmp_path)
    assert client.request(method, path, json=body).status_code == 401


def test_import_returns_suggestions_and_a_saved_queue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_library_web(monkeypatch)
    client, _ = _client(tmp_path)
    r = client.post("/library/import", json={"items": _reader_library()}, headers=MASTER_HEADERS)
    assert r.status_code == 200, r.text
    preview = r.json()
    assert (
        preview["received"],
        preview["new"],
        preview["queued_episodes"],
        preview["pending"],
    ) == (3, 3, 3, 0)
    [deep_talk] = preview["suggested_sources"]  # Invest has a single save
    assert deep_talk["title"] == "Deep Talk" and deep_talk["save_count"] == 2
    assert deep_talk["source"] == {
        "kind": "rss",
        "feed_url": FEED_A,
        "title": "Deep Talk",
        "artwork_url": None,
    }
    assert preview["saved_queue_source"] == {
        "kind": "saved",
        "providers": None,
        "title": "Saved episodes",
    }
    assert "saved_queue_source" in preview["next_steps"]

    items = client.get("/library/items?status=resolved", headers=MASTER_HEADERS).json()
    assert {i["item"]["title"] for i in items} == {"First", "Second", "Investing one"}


def test_reimport_is_idempotent_and_consumed_saves_leave_the_queue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    web = _install_library_web(monkeypatch)
    client, _ = _client(tmp_path)
    client.post("/library/import", json={"items": _reader_library()}, headers=MASTER_HEADERS)
    lookups = len(web.calls)

    replay = _reader_library()
    replay[0]["consumed"] = True
    r = client.post("/library/import", json={"items": replay}, headers=MASTER_HEADERS).json()
    assert (r["new"], r["queued_episodes"]) == (0, 2)
    assert len(web.calls) == lookups  # resolved items cost no new lookups


def test_import_is_scoped_to_the_callers_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_library_web(monkeypatch)
    client, db = _client(tmp_path)
    token = SqliteKeyStore(db).issue("alice@example.com")
    alice = {"Authorization": f"Bearer {token}"}
    client.post("/library/import", json={"items": _reader_library()}, headers=alice)
    assert len(client.get("/library/items", headers=alice).json()) == 3
    assert client.get("/library/items", headers=MASTER_HEADERS).json() == []


def test_soul_from_library_proposes_a_soul_and_needs_items(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_library_web(monkeypatch)
    client, _ = _client(tmp_path)
    assert client.post("/library/soul", headers=MASTER_HEADERS).status_code == 422
    client.post("/library/import", json={"items": _reader_library()}, headers=MASTER_HEADERS)
    r = client.post("/library/soul", headers=MASTER_HEADERS)
    assert r.status_code == 200 and r.json()["based_on"] == 3
    assert "## Curation Guidance" in r.json()["soul"]


def test_import_validation_rejects_bad_items(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    bad = {"items": [{"provider": "myspace", "item_kind": "episode", "title": "x"}]}
    assert client.post("/library/import", json=bad, headers=MASTER_HEADERS).status_code == 422


def _tools(tmp_path: Path) -> tuple[ChorusTools, SqliteSavedItemStore, SqliteSubscriptionStore]:
    db = tmp_path / f"t-{uuid.uuid4().hex}.db"
    subs = SqliteSubscriptionStore(db)
    saved = SqliteSavedItemStore(db)
    directory = _directory()
    tools = ChorusTools(
        SqliteJobStore(db),
        _deps(tmp_path),
        subscription_store=subs,
        podcasts=directory,
        library=LibraryService(saved, directory, subs),
    )
    return tools, saved, subs


def test_mcp_import_list_soul_and_opml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _install_library_web(monkeypatch)
    tools, _, _ = _tools(tmp_path)
    items = [LibraryItem.model_validate(i) for i in _reader_library()]
    preview = tools.import_library(items)
    assert preview.queued_episodes == 3
    assert len(tools.list_library_items(status="resolved")) == 3
    assert tools.soul_from_library().based_on == 3

    opml = (
        '<opml version="2.0"><body>'
        f'<outline text="Deep Talk" type="rss" xmlUrl="{FEED_A}"/></body></opml>'
    )
    assert [getattr(s, "feed_url", None) for s in tools.import_opml(opml).sources] == [FEED_A]


def test_mcp_registers_the_library_tools(tmp_path: Path) -> None:
    import asyncio

    db = tmp_path / "chorus.db"
    server, _ = create_mcp_server(SqliteJobStore(db), _deps(tmp_path))
    names = {tool.name for tool in asyncio.run(server.list_tools())}
    assert {"import_library", "import_opml", "list_library_items", "soul_from_library"} <= names


def test_mcp_preview_reads_the_callers_saved_queue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_library_web(monkeypatch)
    tools, _, _ = _tools(tmp_path)
    tools.import_library([LibraryItem.model_validate(i) for i in _reader_library()])
    preview = tools.preview_subscription([SavedQueueSource(kind="saved")], max_episodes_per_run=2)
    assert [e.title for e in preview.episodes] == ["First", "Investing one"]
    assert preview.errors == []


# --- feeds: saved sources without a store ----------------------------------------------


def test_a_saved_source_without_a_store_reports_unavailable() -> None:
    gathered = gather_episodes([SavedQueueSource(kind="saved")], NOW, 10)
    assert gathered.per_source == [[]]
    assert gathered.errors[0].reason == "the saved-episode queue is not available here"


# --- scheduler: a subscription drains the saved queue ----------------------------------


class _AnyEpisodeProvider:
    def get(self, episode: EpisodeInput) -> Transcript:
        return Transcript(
            video_id=episode.resolved_id(), segments=SAMPLE_SEGMENTS, source="fixture"
        )


def _deps(tmp_path: Path) -> Deps:
    return Deps(
        provider=_AnyEpisodeProvider(),  # type: ignore[arg-type]
        llm=MockLLMClient(),
        composer=MockScriptComposer(),
        renderer=MockAudioRenderer(out_dir=tmp_path / "artifacts"),
        artifacts=LocalArtifactStore(tmp_path / "artifacts"),
    )


def test_a_subscription_run_digests_saved_episodes_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_library_web(monkeypatch)
    db = tmp_path / "run.db"
    store, subs, saved = SqliteJobStore(db), SqliteSubscriptionStore(db), SqliteSavedItemStore(db)
    service = LibraryService(saved, _directory(), subs)
    service.import_items(
        "master", LibraryImport.model_validate({"items": _reader_library()}), now=NOW
    )

    sender = MockEmailSender()
    sub = Subscription(
        subscription_id="sub-saved",
        owner="master",
        email="principal@example.com",
        soul=SOUL,
        context="",
        sources=[SavedQueueSource(kind="saved")],
        max_episodes_per_run=2,
        next_run_at=NOW,
    )
    subs.create(sub)
    deps = _deps(tmp_path)

    first = run_subscription(
        sub, store, deps, subs, sender, "https://chorus.example.com", NOW, saved_items=saved
    )
    assert first is not None
    after_first = subs.get("sub-saved")
    assert after_first is not None and after_first.last_run_summary is not None
    assert after_first.last_run_summary.new_episodes == 2
    assert len(after_first.seen_episode_ids) == 2

    later = NOW + timedelta(days=7)
    second = run_subscription(
        after_first,
        store,
        deps,
        subs,
        sender,
        "https://chorus.example.com",
        later,
        saved_items=saved,
    )
    assert second is not None
    after_second = subs.get("sub-saved")
    assert after_second is not None and after_second.last_run_summary is not None
    assert after_second.last_run_summary.new_episodes == 1  # the remaining save, never a repeat
    assert len(set(after_second.seen_episode_ids)) == 3


def test_saved_queue_lister_is_bound_to_one_owner(tmp_path: Path) -> None:
    saved = SqliteSavedItemStore(tmp_path / "s.db")
    mine = _resolved(saved_episode(SHOW_A_ID, "1", "Mine", show="S", days_ago=1))
    theirs = _resolved(saved_episode(SHOW_A_ID, "2", "Theirs", show="S", days_ago=1)).model_copy(
        update={"owner": "bob@example.com"}
    )
    saved.put_many([mine, theirs])
    lister = saved_queue_lister(saved, "master", NOW)
    assert [e.title for e in lister(SavedQueueSource(kind="saved"), 10, frozenset())] == ["Mine"]
