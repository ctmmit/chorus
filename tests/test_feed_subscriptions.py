"""Feed-based subscriptions end to end: the scheduler's cursor, seen-list,
round-robin cap and empty-week behavior; the sources API (create, patch,
preview, run-now); the email templates; and the Monday -> Friday scenario with
a fake feed that gains an episode between runs.

No network: `chorus.feeds._fetch_bounded` is faked (tests/feedfakes.py) and
every episode, RSS or not, resolves to the synthetic `sample_public`
transcript, so these run without the private fixtures repo.
"""
from __future__ import annotations

import json
import re
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from chorus.app import create_app
from chorus.artifacts import LocalArtifactStore
from chorus.audio import MockAudioRenderer
from chorus.digest_email import render_digest_email, render_empty_email
from chorus.email import MockEmailSender
from chorus.feeds import FeedEpisode, FeedFetchError
from chorus.jobs import SqliteJobStore
from chorus.keys import SqliteKeyStore
from chorus.llm import MockLLMClient
from chorus.models import EpisodeInput, JobStatus, Transcript
from chorus.pipeline import Deps
from chorus.scheduler import next_run, run_subscription
from chorus.script import MockScriptComposer
from chorus.subscriptions import (
    MASTER_OWNER,
    SEEN_EPISODE_IDS_MAX,
    RssSource,
    ShowSource,
    SqliteSubscriptionStore,
    Subscription,
    YoutubeSource,
)
from tests.feedfakes import (
    FakeWeb,
    install_fake_web,
    rss_feed,
    rss_item,
    youtube_entry,
    youtube_feed,
)

FIX = Path(__file__).resolve().parent.parent / "fixtures"
SOUL = (FIX / "souls" / "soul_investor.md").read_text(encoding="utf-8")
SAMPLE_SEGMENTS = json.loads((FIX / "transcripts" / "sample_public.json").read_text(encoding="utf-8"))[
    "segments"
]

BASE_URL = "https://chorus.example.com"
MASTER_HEADERS = {"Authorization": "Bearer master-token"}

# 2026-09-21 is a Monday; the run hour is 13:00 UTC on Fridays.
MONDAY = datetime(2026, 9, 21, 9, 0, tzinfo=UTC)
FRIDAY_1 = datetime(2026, 9, 25, 13, 0, tzinfo=UTC)
FRIDAY_2 = datetime(2026, 10, 2, 13, 0, tzinfo=UTC)
FRIDAY_3 = datetime(2026, 10, 9, 13, 0, tzinfo=UTC)

FEED_A = "https://feeds.example.com/alpha.xml"
FEED_B = "https://feeds.example.com/beta.xml"


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHORUS_UNSUBSCRIBE_SECRET", "test-unsubscribe-secret")
    for name in ("CHORUS_MAX_JOBS_PER_DAY", "CHORUS_MAX_INFLIGHT_JOBS", "CHORUS_VIEWER_URL"):
        monkeypatch.delenv(name, raising=False)


class _AnyEpisodeProvider:
    """Serves the synthetic sample transcript for any episode (an RSS episode's
    id is a hash, so the fixture-by-id provider cannot find it)."""

    def get(self, episode: EpisodeInput) -> Transcript:
        return Transcript(
            video_id=episode.resolved_id(), segments=SAMPLE_SEGMENTS, source="fixture"
        )


class _FailingProvider:
    def get(self, episode: EpisodeInput) -> Transcript:
        from chorus.transcripts import TranscriptNotFound

        raise TranscriptNotFound("no transcript for this episode")


def _deps(tmp_path: Path, provider: object | None = None) -> Deps:
    return Deps(
        provider=provider or _AnyEpisodeProvider(),  # type: ignore[arg-type]
        llm=MockLLMClient(),
        composer=MockScriptComposer(),
        renderer=MockAudioRenderer(out_dir=tmp_path / "artifacts"),
        artifacts=LocalArtifactStore(tmp_path / "artifacts"),
    )


class _World:
    """Stores, sender and deps for one scheduler test."""

    def __init__(self, tmp_path: Path, provider: object | None = None) -> None:
        db = tmp_path / f"w-{uuid.uuid4().hex}.db"
        self.store = SqliteJobStore(db)
        self.subs = SqliteSubscriptionStore(db)
        self.sender = MockEmailSender()
        self.deps = _deps(tmp_path, provider)

    def run(self, sub: Subscription, now: datetime, **kwargs: object) -> str | None:
        return run_subscription(
            sub, self.store, self.deps, self.subs, self.sender, BASE_URL, now, **kwargs  # type: ignore[arg-type]
        )

    def reload(self, sub: Subscription) -> Subscription:
        fresh = self.subs.get(sub.subscription_id)
        assert fresh is not None
        return fresh


def _sub(sources: list[object], **overrides: object) -> Subscription:
    fields: dict[str, object] = {
        "subscription_id": uuid.uuid4().hex,
        "owner": MASTER_OWNER,
        "email": "principal@example.com",
        "soul": SOUL,
        "context": "",
        "sources": sources,
        "cadence": "weekly",
        "next_run_at": FRIDAY_1,
        "created_at": MONDAY,
    }
    fields.update(overrides)
    return Subscription.model_validate(fields)


def _rss(url: str, title: str | None = None) -> RssSource:
    return RssSource(kind="rss", feed_url=url, title=title)


def _items(prefix: str, count: int, newest: datetime, step_hours: int = 6) -> list[str]:
    return [
        rss_item(
            f"{prefix} episode {i}",
            pub=newest - timedelta(hours=step_hours * i),
            guid=f"{prefix}-{i}",
            audio=f"https://cdn.example.com/{prefix}-{i}.mp3",
        )
        for i in range(count)
    ]


def _digest_titles(world: _World, job_id: str | None) -> list[str]:
    assert job_id is not None
    job = world.store.get(job_id)
    assert job is not None and job.digest is not None
    return [ep.episode_title or "" for ep in job.digest.episodes]


# --- first run: lookback window ------------------------------------------------------


def test_first_run_uses_the_lookback_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    web = install_fake_web(monkeypatch)
    web.set(
        FEED_A,
        rss_feed(
            "Alpha",
            [
                rss_item("Inside window", pub=FRIDAY_1 - timedelta(days=3), guid="in", audio="https://cdn/in.mp3"),
                rss_item("Outside window", pub=FRIDAY_1 - timedelta(days=10), guid="out", audio="https://cdn/out.mp3"),
            ],
        ),
    )
    world = _World(tmp_path)
    sub = _sub([_rss(FEED_A)], lookback_days_first_run=7)
    world.subs.create(sub)

    job_id = world.run(sub, FRIDAY_1)

    assert _digest_titles(world, job_id) == ["Inside window"]
    stored = world.reload(sub)
    assert stored.last_run_at == FRIDAY_1
    assert stored.next_run_at == next_run("weekly", FRIDAY_1)
    assert len(stored.seen_episode_ids) == 1
    assert stored.last_run_summary is not None
    assert stored.last_run_summary.new_episodes == 1
    assert stored.last_run_summary.job_id == job_id
    assert stored.last_run_summary.skipped_reason is None
    assert stored.last_run_summary.ran_at == FRIDAY_1


def test_a_narrower_lookback_excludes_older_episodes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    web = install_fake_web(monkeypatch)
    web.set(FEED_A, rss_feed("Alpha", _items("a", 3, FRIDAY_1 - timedelta(days=1), step_hours=48)))
    world = _World(tmp_path)
    sub = _sub([_rss(FEED_A)], lookback_days_first_run=2)
    world.subs.create(sub)
    job_id = world.run(sub, FRIDAY_1)
    assert _digest_titles(world, job_id) == ["a episode 0"]


# --- seen dedupe across runs ---------------------------------------------------------


def test_a_seen_episode_is_never_digested_again_even_if_the_cursor_is_behind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    web = install_fake_web(monkeypatch)
    web.set(FEED_A, rss_feed("Alpha", _items("a", 2, FRIDAY_1 - timedelta(days=1))))
    world = _World(tmp_path)
    sub = _sub([_rss(FEED_A)])
    world.subs.create(sub)
    assert world.run(sub, FRIDAY_1) is not None

    # Rewind the cursor, as a manual edit or a re-dated feed could: the seen
    # list alone must keep the same episodes from being sent twice.
    stored = world.reload(sub)
    stored.last_run_at = FRIDAY_1 - timedelta(days=30)
    world.subs.save(stored)
    world.sender.sent.clear()

    assert world.run(stored, FRIDAY_2) is None
    assert len(world.sender.sent) == 1
    assert world.sender.sent[0].subject.startswith("Nothing new")


def test_seen_list_is_bounded(tmp_path: Path) -> None:
    sub = _sub([_rss(FEED_A)], seen_episode_ids=[f"id-{i}" for i in range(SEEN_EPISODE_IDS_MAX + 50)])
    assert len(sub.seen_episode_ids) == SEEN_EPISODE_IDS_MAX
    assert sub.seen_episode_ids[-1] == f"id-{SEEN_EPISODE_IDS_MAX + 49}"  # most recent kept


# --- cap and round robin --------------------------------------------------------------


def test_cap_is_shared_round_robin_across_sources(tmp_path: Path) -> None:
    prolific = _items("alpha", 6, FRIDAY_1 - timedelta(hours=1), step_hours=1)
    quiet = _items("beta", 1, FRIDAY_1 - timedelta(days=2))

    def lister(source, since, limit, *, resolver=None):  # type: ignore[no-untyped-def]
        items = prolific if source.feed_url == FEED_A else quiet
        from chorus.feeds import parse_rss

        title = "Alpha" if source.feed_url == FEED_A else "Beta"
        root_xml = rss_feed(title, items)
        import xml.etree.ElementTree as ET

        episodes = parse_rss(ET.fromstring(root_xml), source.feed_url).episodes
        return [e for e in episodes if e.published_at >= since][:limit]

    world = _World(tmp_path)
    sub = _sub([_rss(FEED_A), _rss(FEED_B)], max_episodes_per_run=3)
    world.subs.create(sub)

    job_id = world.run(sub, FRIDAY_1, lister=lister)

    titles = _digest_titles(world, job_id)
    assert len(titles) == 3
    assert "beta episode 0" in titles  # the quiet show is not crowded out
    assert sum(t.startswith("alpha") for t in titles) == 2
    # The footer is honest about what the cap left out: 4 more alpha episodes.
    assert "4 more new episodes were not included (cap of 3 per run)" in world.sender.sent[0].text


# --- empty weeks ------------------------------------------------------------------------


def test_empty_week_sends_a_short_note_and_creates_no_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    web = install_fake_web(monkeypatch)
    web.set(FEED_A, rss_feed("Alpha", [rss_item("Ancient", pub=MONDAY - timedelta(days=90), guid="x", audio="https://cdn/x.mp3")]))
    world = _World(tmp_path)
    sub = _sub([_rss(FEED_A, "Alpha Pod")])
    world.subs.create(sub)

    assert world.run(sub, FRIDAY_1) is None

    assert len(world.sender.sent) == 1
    mail = world.sender.sent[0]
    assert mail.subject == "Nothing new from your shows this week"
    assert "Alpha Pod" in mail.text  # the sources that were checked
    assert "Unsubscribe: " in mail.text
    assert mail.headers is not None and "List-Unsubscribe" in mail.headers
    assert world.store.count_for_owner(MASTER_OWNER, MONDAY) == 0  # no job

    stored = world.reload(sub)
    assert stored.last_run_summary is not None
    assert stored.last_run_summary.new_episodes == 0
    assert stored.last_run_summary.job_id is None
    assert stored.last_run_summary.skipped_reason == "no new episodes"
    assert stored.last_run_at == FRIDAY_1
    assert stored.next_run_at == next_run("weekly", FRIDAY_1)


def test_empty_week_email_is_suppressed_when_notify_is_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    web = install_fake_web(monkeypatch)
    web.set(FEED_A, rss_feed("Alpha", []))
    world = _World(tmp_path)
    sub = _sub([_rss(FEED_A)], notify_when_empty=False)
    world.subs.create(sub)

    assert world.run(sub, FRIDAY_1) is None

    assert world.sender.sent == []
    stored = world.reload(sub)
    assert stored.last_run_summary is not None
    assert stored.last_run_summary.skipped_reason == "no new episodes"
    assert stored.next_run_at == next_run("weekly", FRIDAY_1)


def test_empty_daily_subscription_says_today(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    web = install_fake_web(monkeypatch)
    web.set(FEED_A, rss_feed("Alpha", []))
    world = _World(tmp_path)
    sub = _sub([_rss(FEED_A)], cadence="daily")
    world.subs.create(sub)
    world.run(sub, FRIDAY_1)
    assert world.sender.sent[0].subject == "Nothing new from your shows today"


# --- feed errors are reported honestly, never fatal --------------------------------------


def test_a_dead_feed_does_not_stop_the_others_and_is_named_in_the_footer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    web = install_fake_web(monkeypatch)
    web.set(FEED_A, rss_feed("Alpha", _items("a", 1, FRIDAY_1 - timedelta(days=1))))
    web.set(FEED_B, "server on fire", status=503)
    world = _World(tmp_path)
    sub = _sub([_rss(FEED_A), _rss(FEED_B, "Beta Pod")])
    world.subs.create(sub)

    job_id = world.run(sub, FRIDAY_1)

    assert _digest_titles(world, job_id) == ["a episode 0"]
    text = world.sender.sent[0].text
    assert "Could not check:" in text
    assert "Beta Pod" in text and "HTTP 503" in text


def test_when_every_source_fails_the_cursor_stays_and_the_email_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    web = install_fake_web(monkeypatch)
    web.set(FEED_A, "down", status=500)
    world = _World(tmp_path)
    sub = _sub([_rss(FEED_A, "Alpha Pod")])
    world.subs.create(sub)

    assert world.run(sub, FRIDAY_1) is None

    mail = world.sender.sent[0]
    assert mail.subject == "Could not check your shows this week"
    assert "Alpha Pod" in mail.text and "HTTP 500" in mail.text
    stored = world.reload(sub)
    assert stored.last_run_at is None  # episodes we could not see are not skipped
    assert stored.next_run_at == next_run("weekly", FRIDAY_1)
    assert stored.last_run_summary is not None
    assert stored.last_run_summary.skipped_reason == "no new episodes; 1 of 1 source(s) could not be read"


# --- failed job: the episodes are retried -------------------------------------------------


def test_a_failed_job_leaves_cursor_and_seen_list_so_the_episodes_are_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    web = install_fake_web(monkeypatch)
    web.set(FEED_A, rss_feed("Alpha", _items("a", 1, FRIDAY_1 - timedelta(days=1))))
    world = _World(tmp_path, provider=_FailingProvider())
    sub = _sub([_rss(FEED_A)])
    world.subs.create(sub)

    job_id = world.run(sub, FRIDAY_1)

    job = world.store.get(job_id or "")
    assert job is not None and job.status == JobStatus.failed
    assert world.sender.sent[0].subject == "This week's digest failed"
    stored = world.reload(sub)
    assert stored.last_run_at is None
    assert stored.seen_episode_ids == []
    assert stored.last_job_id == job_id
    assert stored.next_run_at == next_run("weekly", FRIDAY_1)  # never stuck due
    assert stored.last_run_summary is not None
    assert stored.last_run_summary.skipped_reason is not None
    assert stored.last_run_summary.skipped_reason.startswith("digest job failed")

    # A retry (a run-now, say) with a working provider sends the same episode.
    healthy = _World(tmp_path)
    healthy.subs = world.subs
    healthy.store = world.store
    retry_id = healthy.run(stored, FRIDAY_1 + timedelta(hours=2))  # e.g. a run-now
    assert _digest_titles(healthy, retry_id) == ["a episode 0"]


# --- quotas --------------------------------------------------------------------------------


def test_scheduler_enforces_the_owner_quota_without_losing_episodes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CHORUS_MAX_JOBS_PER_DAY", "1")
    web = install_fake_web(monkeypatch)
    web.set(FEED_A, rss_feed("Alpha", _items("a", 1, datetime.now(UTC) - timedelta(days=1))))
    world = _World(tmp_path)
    world.store.create(owner="a@example.com")  # the owner's one job today
    sub = _sub([_rss(FEED_A)], owner="a@example.com")
    world.subs.create(sub)

    assert world.run(sub, datetime.now(UTC)) is None

    assert world.sender.sent[0].subject == "This week's digest was skipped"
    stored = world.reload(sub)
    assert stored.last_run_summary is not None
    assert stored.last_run_summary.skipped_reason is not None
    assert stored.last_run_summary.skipped_reason.startswith("quota exceeded")
    assert stored.last_run_at is None and stored.seen_episode_ids == []


# --- catalog show sources ---------------------------------------------------------------------


@pytest.mark.skipif(
    not (Path(__file__).resolve().parent.parent / "fixtures" / "transcripts" / "c4tvVKDhpiY.json").exists(),
    reason="needs the private 20VC transcript (failed runs never mark episodes seen)",
)
def test_catalog_show_source_digests_once_then_goes_quiet(tmp_path: Path) -> None:
    # Catalog fixtures carry no dates; the seen list is what stops repeats.
    # The fixture-by-id provider serves the catalog episodes' own transcripts.
    from chorus.transcripts import FixtureTranscriptProvider

    world = _World(tmp_path, provider=FixtureTranscriptProvider())
    sub = _sub([ShowSource(kind="show", show="20VC with Harry Stebbings")])
    world.subs.create(sub)

    first = world.run(sub, FRIDAY_1)
    if first is None:
        pytest.skip("catalog show has no episodes in this checkout")
    stored = world.reload(sub)
    assert stored.seen_episode_ids
    assert world.run(stored, FRIDAY_2) is None


# --- legacy subscriptions are unchanged -----------------------------------------------------------


def test_legacy_episode_subscription_still_redigests_each_run(tmp_path: Path) -> None:
    from chorus.transcripts import FixtureTranscriptProvider

    world = _World(tmp_path, provider=FixtureTranscriptProvider())
    legacy = Subscription.model_validate(
        {
            "subscription_id": uuid.uuid4().hex,
            "owner": MASTER_OWNER,
            "email": "principal@example.com",
            "soul": SOUL,
            "context": "",
            "episodes": [EpisodeInput(video_id="sample_public")],
            "cadence": "weekly",
            "next_run_at": FRIDAY_1,
        }
    )
    world.subs.create(legacy)

    first = world.run(legacy, FRIDAY_1)
    second = world.run(world.reload(legacy), FRIDAY_2)

    assert first is not None and second is not None and first != second
    stored = world.reload(legacy)
    assert stored.seen_episode_ids == []
    assert stored.last_run_summary is None
    assert len(world.sender.sent) == 2


# --- Monday -> Friday -> Friday -> Friday end to end --------------------------------------------------


def test_monday_subscription_digests_new_episodes_then_only_the_new_one_then_goes_quiet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    web = install_fake_web(monkeypatch)
    week_one = [
        rss_item(f"Week one ep {i}", pub=FRIDAY_1 - timedelta(days=i + 1), guid=f"w1-{i}", audio=f"https://cdn/w1-{i}.mp3")
        for i in range(3)
    ]
    web.set(FEED_A, rss_feed("Alpha", week_one))

    world = _World(tmp_path)
    scheduled_for = next_run("weekly", MONDAY)
    assert scheduled_for == FRIDAY_1
    sub = _sub([_rss(FEED_A)], next_run_at=scheduled_for)
    world.subs.create(sub)

    # Friday 1: three episodes were published in the lookback window.
    first_job = world.run(sub, FRIDAY_1)
    assert sorted(_digest_titles(world, first_job)) == [f"Week one ep {i}" for i in range(3)]
    assert world.sender.sent[-1].subject.startswith("3 of 3 episodes")

    # The feed gains one episode during the following week.
    new_item = rss_item("Brand new", pub=FRIDAY_2 - timedelta(days=2), guid="w2-0", audio="https://cdn/w2-0.mp3")
    web.set(FEED_A, rss_feed("Alpha", [new_item, *week_one]))

    # Friday 2: only the new episode is digested.
    second_job = world.run(world.reload(sub), FRIDAY_2)
    assert _digest_titles(world, second_job) == ["Brand new"]
    assert world.sender.sent[-1].subject.startswith("1 of 1 episode")
    assert "Brand new" in world.sender.sent[-1].text
    assert "Week one ep" not in world.sender.sent[-1].text

    # Friday 3: nothing new, so the empty-week note goes out and no job runs.
    jobs_before = world.store.count_for_owner(MASTER_OWNER, MONDAY)
    assert world.run(world.reload(sub), FRIDAY_3) is None
    assert world.store.count_for_owner(MASTER_OWNER, MONDAY) == jobs_before
    assert world.sender.sent[-1].subject == "Nothing new from your shows this week"
    assert len(world.sender.sent) == 3

    final = world.reload(sub)
    assert len(final.seen_episode_ids) == 4
    assert final.last_run_at == FRIDAY_3
    assert final.next_run_at == next_run("weekly", FRIDAY_3)


# --- email templates ------------------------------------------------------------------------------------


def _done_job_for(world: _World, sub: Subscription, now: datetime) -> tuple[str, Subscription]:
    job_id = world.run(sub, now)
    assert job_id is not None
    return job_id, world.reload(sub)


def test_digest_email_groups_highlights_by_source_title(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    web = install_fake_web(monkeypatch)
    web.set(FEED_A, rss_feed("Alpha Pod", _items("a", 2, FRIDAY_1 - timedelta(days=1))))
    web.set(FEED_B, rss_feed("Beta Pod", _items("b", 1, FRIDAY_1 - timedelta(days=2))))
    world = _World(tmp_path)
    sub = _sub([_rss(FEED_A), _rss(FEED_B)])
    world.subs.create(sub)

    world.run(sub, FRIDAY_1)

    text = world.sender.sent[0].text
    assert "# Alpha Pod" in text and "# Beta Pod" in text
    alpha, beta = text.index("# Alpha Pod"), text.index("# Beta Pod")
    assert text.index("## a episode 0") > alpha and text.index("## a episode 1") > alpha
    assert text.index("## b episode 0") > beta
    # An RSS highlight links to its enclosure audio.
    assert "https://cdn.example.com/a-0.mp3" in text
    html = world.sender.sent[0].html
    assert "<h2" in html and "Alpha Pod" in html


def test_render_empty_email_lists_sources_and_errors() -> None:
    sub = _sub([_rss(FEED_A)])
    content = render_empty_email(
        sub,
        f"{BASE_URL}/u",
        sources_checked=["Alpha Pod", "Beta Pod"],
        since=FRIDAY_1,
        feed_errors=["Beta Pod: HTTP 500"],
    )
    assert content.subject == "Nothing new from your shows this week"
    assert "No new episodes were published since 25 Sep 2026." in content.text
    assert "- Alpha Pod" in content.text and "- Beta Pod" in content.text
    assert "- Beta Pod: HTTP 500" in content.text
    assert f"{BASE_URL}/u" in content.html


def test_render_digest_email_without_source_titles_keeps_the_flat_layout(tmp_path: Path) -> None:
    from chorus.models import Digest, EpisodeDigest, Job

    sub = _sub([_rss(FEED_A)])
    job = Job(
        job_id="j",
        status=JobStatus.done,
        digest=Digest(
            soul_version="v",
            soul_origin="supplied",
            episodes=[EpisodeDigest(episode_id="abcdefghijk", episode_title="T", highlights=[], refused=True)],
        ),
    )
    text = render_digest_email(job, sub, BASE_URL, f"{BASE_URL}/u").text
    assert "## T" in text
    assert "\n# " not in text  # no source heading when no source title is known


# --- the sources API -----------------------------------------------------------------------------------------


def _client(tmp_path: Path):  # type: ignore[no-untyped-def]
    db = tmp_path / "chorus.db"
    store = SqliteJobStore(db)
    key_store = SqliteKeyStore(db)
    subs = SqliteSubscriptionStore(db)
    sender = MockEmailSender()
    app = create_app(
        store,
        _deps(tmp_path),
        api_token="master-token",
        key_store=key_store,
        email_sender=sender,
        subscription_store=subs,
    )
    return TestClient(app), key_store, subs, sender


def _create_body(**overrides: object) -> dict:
    body: dict[str, object] = {
        "email": "principal@example.com",
        "soul": SOUL,
        "context": "",
        "sources": [{"kind": "rss", "feed_url": FEED_A, "title": "Alpha", "artwork_url": None}],
    }
    body.update(overrides)
    return body


def test_create_with_sources_applies_the_contract_defaults(tmp_path: Path) -> None:
    client, *_ = _client(tmp_path)
    r = client.post("/subscriptions", json=_create_body(), headers=MASTER_HEADERS)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["sources"] == [{"kind": "rss", "feed_url": FEED_A, "title": "Alpha", "artwork_url": None}]
    assert body["max_episodes_per_run"] == 8
    assert body["lookback_days_first_run"] == 7
    assert body["notify_when_empty"] is True
    assert body["seen_episode_ids"] == []
    assert body["last_run_summary"] is None
    assert body["episodes"] is None and body["shows"] is None


def test_all_three_source_kinds_round_trip(tmp_path: Path) -> None:
    client, *_ = _client(tmp_path)
    sources = [
        {"kind": "rss", "feed_url": FEED_A},
        {"kind": "youtube", "channel_id": "UC" + "b" * 22, "title": "A channel"},
        {"kind": "show", "show": "20VC with Harry Stebbings"},
    ]
    r = client.post("/subscriptions", json=_create_body(sources=sources), headers=MASTER_HEADERS)
    assert r.status_code == 200, r.text
    assert [s["kind"] for s in r.json()["sources"]] == ["rss", "youtube", "show"]


@pytest.mark.parametrize(
    "overrides",
    [
        {"sources": []},
        {"sources": [{"kind": "rss", "feed_url": f"https://x.example.com/{i}"} for i in range(51)]},
        {"sources": [{"kind": "youtube", "channel_id": "not-a-channel-id"}]},
        {"sources": [{"kind": "podcastindex", "feed_id": 1}]},
        {"sources": [{"kind": "rss", "feed_url": "ftp://example.com/feed"}]},
        {"max_episodes_per_run": 0},
        {"max_episodes_per_run": 21},
        {"lookback_days_first_run": 0},
        {"lookback_days_first_run": 31},
        {"episodes": [{"video_id": "sample_public"}]},  # sources AND episodes: exactly one
        {"shows": ["20VC with Harry Stebbings"]},
    ],
)
def test_create_rejects_invalid_source_requests(tmp_path: Path, overrides: dict) -> None:
    client, *_ = _client(tmp_path)
    r = client.post("/subscriptions", json=_create_body(**overrides), headers=MASTER_HEADERS)
    assert r.status_code == 422


def test_legacy_episodes_and_shows_are_still_accepted(tmp_path: Path) -> None:
    client, *_ = _client(tmp_path)
    for legacy in ({"episodes": [{"video_id": "sample_public"}]}, {"shows": ["20VC with Harry Stebbings"]}):
        body = _create_body(**legacy)
        del body["sources"]
        r = client.post("/subscriptions", json=body, headers=MASTER_HEADERS)
        assert r.status_code == 200, r.text
        assert r.json()["sources"] is None


def test_patch_accepts_sources_cap_and_notify_and_keeps_history(tmp_path: Path) -> None:
    client, _, subs, _ = _client(tmp_path)
    sub_id = client.post("/subscriptions", json=_create_body(), headers=MASTER_HEADERS).json()["subscription_id"]
    stored = subs.get(sub_id)
    assert stored is not None
    stored.seen_episode_ids = ["rss-aaa", "rss-bbb"]
    subs.save(stored)

    r = client.patch(
        f"/subscriptions/{sub_id}",
        json={
            "sources": [{"kind": "rss", "feed_url": FEED_B}],
            "max_episodes_per_run": 3,
            "notify_when_empty": False,
        },
        headers=MASTER_HEADERS,
    )

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["sources"] == [{"kind": "rss", "feed_url": FEED_B, "title": None, "artwork_url": None}]
    assert body["max_episodes_per_run"] == 3
    assert body["notify_when_empty"] is False
    assert body["seen_episode_ids"] == ["rss-aaa", "rss-bbb"]  # nothing is re-sent after a source swap


def test_patch_switching_a_legacy_subscription_to_sources_and_back(tmp_path: Path) -> None:
    client, *_ = _client(tmp_path)
    legacy = _create_body(episodes=[{"video_id": "sample_public"}])
    del legacy["sources"]
    sub_id = client.post("/subscriptions", json=legacy, headers=MASTER_HEADERS).json()["subscription_id"]

    to_sources = client.patch(
        f"/subscriptions/{sub_id}",
        json={"sources": [{"kind": "rss", "feed_url": FEED_A}]},
        headers=MASTER_HEADERS,
    ).json()
    assert to_sources["episodes"] is None and to_sources["sources"]

    back = client.patch(
        f"/subscriptions/{sub_id}", json={"shows": ["20VC with Harry Stebbings"]}, headers=MASTER_HEADERS
    ).json()
    assert back["sources"] is None and back["shows"] == ["20VC with Harry Stebbings"]

    assert client.patch(f"/subscriptions/{sub_id}", json={"sources": []}, headers=MASTER_HEADERS).status_code == 422


def test_preview_returns_exactly_the_first_run_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    web = install_fake_web(monkeypatch)
    now = datetime.now(UTC)
    web.set(FEED_A, rss_feed("Alpha", _items("a", 4, now - timedelta(hours=2), step_hours=24)))
    web.set(FEED_B, "broken", status=500)
    client, *_ = _client(tmp_path)

    r = client.post(
        "/subscriptions/preview",
        json={
            "sources": [
                {"kind": "rss", "feed_url": FEED_A},
                {"kind": "rss", "feed_url": FEED_B},
            ],
            "lookback_days": 2,
            "max_episodes_per_run": 8,
        },
        headers=MASTER_HEADERS,
    )

    assert r.status_code == 200, r.text
    body = r.json()
    assert [e["title"] for e in body["episodes"]] == ["a episode 0", "a episode 1"]  # 2-day window
    first = body["episodes"][0]
    assert set(first) == {"source_title", "title", "published_at", "episode"}
    assert first["source_title"] == "Alpha"
    assert first["episode"]["feed_url"] == FEED_A and first["episode"]["guid"] == "a-0"
    assert body["errors"][0]["source"] == {"kind": "rss", "feed_url": FEED_B, "title": None, "artwork_url": None}
    assert "HTTP 500" in body["errors"][0]["reason"]
    assert web.calls.count(FEED_A) == 1


def test_preview_defaults_and_validation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    web = install_fake_web(monkeypatch)
    web.set(FEED_A, rss_feed("Alpha", []))
    client, *_ = _client(tmp_path)
    ok = client.post("/subscriptions/preview", json={"sources": [{"kind": "rss", "feed_url": FEED_A}]}, headers=MASTER_HEADERS)
    assert ok.status_code == 200 and ok.json() == {"episodes": [], "errors": []}
    for bad in ({"sources": []}, {"sources": [{"kind": "rss", "feed_url": FEED_A}], "lookback_days": 31}):
        assert client.post("/subscriptions/preview", json=bad, headers=MASTER_HEADERS).status_code == 422


def test_run_now_on_a_feed_subscription_digests_then_reports_nothing_new(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    web = install_fake_web(monkeypatch)
    web.set(FEED_A, rss_feed("Alpha", _items("a", 2, datetime.now(UTC) - timedelta(hours=3))))
    client, *_ = _client(tmp_path)
    sub_id = client.post("/subscriptions", json=_create_body(), headers=MASTER_HEADERS).json()["subscription_id"]

    first = client.post(f"/subscriptions/{sub_id}/run", headers=MASTER_HEADERS)
    assert first.status_code == 200
    assert first.json()["job_id"] is not None and first.json()["skipped_reason"] is None
    job = client.get(f"/digest/{first.json()['job_id']}", headers=MASTER_HEADERS).json()
    assert job["status"] == "done"

    second = client.post(f"/subscriptions/{sub_id}/run", headers=MASTER_HEADERS)
    assert second.json() == {"job_id": None, "skipped_reason": "no new episodes"}

    sub = client.get(f"/subscriptions/{sub_id}", headers=MASTER_HEADERS).json()
    assert len(sub["seen_episode_ids"]) == 2
    assert sub["last_run_summary"]["skipped_reason"] == "no new episodes"
    assert sub["last_run_summary"]["new_episodes"] == 0


def test_preview_requires_a_bearer_token(tmp_path: Path) -> None:
    client, *_ = _client(tmp_path)
    r = client.post("/subscriptions/preview", json={"sources": [{"kind": "rss", "feed_url": FEED_A}]})
    assert r.status_code == 401


def test_cron_tick_runs_feed_subscriptions_and_reports_a_null_job_when_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CRON_SECRET", "tick-secret")
    web = install_fake_web(monkeypatch)
    web.set(FEED_A, rss_feed("Alpha", []))
    client, _, subs, sender = _client(tmp_path)
    due = _sub([_rss(FEED_A)], next_run_at=datetime.now(UTC) - timedelta(hours=1), created_at=datetime.now(UTC))
    subs.create(due)

    r = client.post("/internal/cron/tick", headers={"Authorization": "Bearer tick-secret"})

    assert r.status_code == 200
    assert r.json() == {"ran": 1, "subscriptions": [{"subscription_id": due.subscription_id, "job_id": None}]}
    assert sender.sent[0].subject == "Nothing new from your shows this week"


# --- POST /keys: the web sign-in link --------------------------------------------------------------------------


def test_key_email_carries_a_fragment_sign_in_link_when_the_viewer_url_is_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CHORUS_VIEWER_URL", "https://app.example.com/")
    client, _, _, sender = _client(tmp_path)

    assert client.post("/keys", json={"email": "new@example.com"}).status_code == 202

    text = sender.sent[0].text
    token = re.search(r"(chorus_[A-Za-z0-9_\-]+)", text)
    assert token is not None
    assert f"https://app.example.com/subscribe#token={token.group(1)}" in text
    assert "?token=" not in text  # a fragment, never a query string
    assert text.count(token.group(1)) == 2  # the plain token text is still there


def test_key_email_has_no_link_without_the_viewer_url(tmp_path: Path) -> None:
    client, _, _, sender = _client(tmp_path)
    assert client.post("/keys", json={"email": "new@example.com"}).status_code == 202
    assert "subscribe#token" not in sender.sent[0].text
    assert sender.sent[0].text.count("chorus_") == 1


# --- small invariants -----------------------------------------------------------------------------------------------


def test_feed_episode_and_source_error_models_are_exported_for_the_ui() -> None:
    assert FeedEpisode.model_fields.keys() == {"source_title", "title", "published_at", "episode"}
    assert issubclass(FeedFetchError, Exception)
    assert YoutubeSource.model_fields["channel_id"].metadata  # pattern-constrained


def test_fakeweb_refuses_unrouted_urls() -> None:
    from chorus.transcripts import TranscriptProviderError

    with pytest.raises(TranscriptProviderError):
        FakeWeb()("GET", "https://nowhere.example.com", timeout_s=1, what="x", max_bytes=10)


def test_skill_docs_no_longer_claim_catalog_shows_pick_up_new_episodes() -> None:
    root = Path(__file__).resolve().parent.parent
    for relative in ("SKILL.md", "skills/chorus-weekly/SKILL.md"):
        text = (root / relative).read_text(encoding="utf-8")
        assert "picked up automatically" not in text
    skill = (root / "SKILL.md").read_text(encoding="utf-8")
    assert "search_podcasts" in skill and "preview_subscription" in skill
    weekly = (root / "skills/chorus-weekly/SKILL.md").read_text(encoding="utf-8")
    assert "`search_podcasts` -> `preview_subscription` -> `subscribe`" in weekly


def test_youtube_entry_helpers_produce_atom() -> None:
    assert "<yt:videoId>AAAAAAAAAAA</yt:videoId>" in youtube_feed("T", [youtube_entry("AAAAAAAAAAA", "t", "2026-01-01T00:00:00+00:00")])
