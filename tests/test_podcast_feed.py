"""The private podcast feed (chorus/podcast_feed.py): tokens, the RSS
document, byte ranges, the token-authorized routes, and the local file."""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest
from fastapi.testclient import TestClient

from chorus.app import create_app
from chorus.artifacts import LocalArtifactStore
from chorus.audio import MockAudioRenderer
from chorus.jobs import MASTER_OWNER, FinishedJob, SqliteJobStore
from chorus.llm import MockLLMClient
from chorus.models import (
    Chapter,
    Digest,
    EpisodeDigest,
    Highlight,
    Job,
    JobStatus,
    Script,
)
from chorus.pipeline import Deps
from chorus.podcast_feed import (
    FEED_MAX_ITEMS,
    ITUNES_NS,
    PODCAST_NS,
    FeedEpisode,
    build_episodes,
    byte_range,
    episode_notes,
    episode_title,
    feed_token,
    owner_for_token,
    render_feed,
    write_local_feed,
)
from chorus.script import MockScriptComposer
from chorus.transcripts import FixtureTranscriptProvider

FRAME = bytes([0xFF, 0xFB, 0x90, 0x00]) + b"\x00" * 413  # 26 ms of MPEG-1 Layer III
AUDIO = FRAME * 100
TOKEN = "s3cret"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture(autouse=True)
def _feed_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHORUS_FEED_SECRET", "feed-secret-for-tests")


def _digest() -> Digest:
    highlight = Highlight(
        episode_id="abc",
        episode_title="Pricing",
        segment_timestamp=754.0,
        quote="pricing power is the whole game",
        relevance_score=0.9,
        why_surface="names the moat",
        show="Acquired",
    )
    episode = EpisodeDigest(
        episode_id="abc",
        episode_title="Pricing",
        highlights=[highlight],
        show="Acquired",
        url="https://www.youtube.com/watch?v=abc",
    )
    return Digest(soul_version="v", episodes=[episode])


def _done_job(job_id: str, owner: str = MASTER_OWNER, ext: str = "mp3") -> Job:
    return Job(
        job_id=job_id,
        status=JobStatus.done,
        owner=owner,
        digest=_digest(),
        script=Script(soul_version="v", takes=[], monologue="The whole episode, as text."),
        audio_url=f"/artifacts/episode_{job_id}.{ext}",
        chapters=[Chapter(start_seconds=0.0, title="Intro")],
    )


# --- tokens --------------------------------------------------------------------


def test_tokens_are_stable_per_owner_and_hide_the_owner() -> None:
    token = feed_token("pat@example.com")
    assert token == feed_token("pat@example.com")
    assert token != feed_token("sam@example.com")
    assert "pat" not in token and len(token) == 40


def test_owner_for_token(monkeypatch: pytest.MonkeyPatch) -> None:
    owners = ["master", "pat@example.com"]
    assert owner_for_token(feed_token("pat@example.com"), owners) == "pat@example.com"
    assert owner_for_token("0" * 40, owners) is None
    old = feed_token("pat@example.com")
    monkeypatch.setenv("CHORUS_FEED_SECRET", "rotated")
    assert owner_for_token(old, owners) is None  # rotating the secret revokes feeds


# --- items and the document -------------------------------------------------------


def test_title_and_notes() -> None:
    job = _done_job("j1")
    published = datetime(2026, 10, 4, 7, 0, tzinfo=UTC)
    assert episode_title(job, published) == "Chorus · 04 Oct 2026 · Acquired"
    notes = episode_notes(job)
    assert "Acquired: Pricing (12:34): names the moat" in notes
    assert '"pricing power is the whole game"' in notes
    assert "https://www.youtube.com/watch?v=abc&t=754s" in notes


def _episode(job_id: str, day: int) -> FeedEpisode:
    return FeedEpisode(
        job_id=job_id,
        title=f"Episode {job_id}",
        published=datetime(2026, 10, day, tzinfo=UTC),
        notes="notes & more",
        audio_url=f"https://h/feed/t/{job_id}.mp3",
        audio_bytes=1234,
        duration_seconds=61.6,
        chapters_url=f"https://h/feed/t/{job_id}/chapters.json",
        transcript_url=f"https://h/feed/t/{job_id}/transcript.txt",
    )


def test_render_feed_is_valid_private_rss_newest_first() -> None:
    xml = render_feed([_episode("old", 1), _episode("new", 3)], "https://h/feed/t.xml")
    root = ET.fromstring(xml)
    channel = root.find("channel")
    assert root.tag == "rss" and root.get("version") == "2.0" and channel is not None
    assert channel.findtext(f"{{{ITUNES_NS}}}block") == "Yes"
    assert channel.findtext(f"{{{PODCAST_NS}}}locked") == "yes"
    items = channel.findall("item")
    assert [i.findtext("guid") for i in items] == ["new", "old"]
    first = items[0]
    enclosure = first.find("enclosure")
    assert enclosure is not None
    assert enclosure.attrib == {
        "url": "https://h/feed/t/new.mp3", "length": "1234", "type": "audio/mpeg",
    }
    assert first.findtext(f"{{{ITUNES_NS}}}duration") == "62"
    assert first.findtext("description") == "notes & more"
    assert first.findtext("pubDate") == "Sat, 03 Oct 2026 00:00:00 +0000"
    chapters = first.find(f"{{{PODCAST_NS}}}chapters")
    assert chapters is not None and chapters.get("type") == "application/json+chapters"
    assert first.find(f"{{{PODCAST_NS}}}transcript") is not None


def test_render_feed_caps_items() -> None:
    episodes = [_episode(str(i), 1) for i in range(FEED_MAX_ITEMS + 5)]
    assert len(ET.fromstring(render_feed(episodes, "u")).findall("channel/item")) == FEED_MAX_ITEMS


def test_build_episodes_measures_audio_and_skips_missing(tmp_path: Path) -> None:
    artifacts = LocalArtifactStore(tmp_path)
    artifacts.put("episode_j1.mp3", AUDIO, "audio/mpeg")
    published = datetime(2026, 10, 4, tzinfo=UTC)
    finished = [FinishedJob(_done_job("j1"), published), FinishedJob(_done_job("gone"), published)]
    [episode] = build_episodes(finished, "https://h/", "tok", artifacts)
    assert episode.audio_url == "https://h/feed/tok/j1.mp3"
    assert episode.audio_bytes == len(AUDIO)
    assert episode.duration_seconds == pytest.approx(100 * 1152 / 44_100)
    assert episode.chapters_url == "https://h/feed/tok/j1/chapters.json"


# --- byte ranges ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        (None, None),
        ("bytes=0-99", (0, 99)),
        ("bytes=100-", (100, 999)),
        ("bytes=-10", (990, 999)),
        ("bytes=900-5000", (900, 999)),
        ("bytes=0-1,5-6", None),
        ("items=0-1", None),
    ],
)
def test_byte_range(header: str | None, expected: tuple[int, int] | None) -> None:
    assert byte_range(header, 1000) == expected


def test_unsatisfiable_range() -> None:
    with pytest.raises(ValueError):
        byte_range("bytes=1000-", 1000)


# --- routes -----------------------------------------------------------------------


def _client(tmp_path: Path) -> tuple[TestClient, SqliteJobStore, LocalArtifactStore]:
    store = SqliteJobStore(tmp_path / "jobs.db")
    artifacts = LocalArtifactStore(tmp_path / "artifacts")
    deps = Deps(
        FixtureTranscriptProvider(), MockLLMClient(), MockScriptComposer(),
        MockAudioRenderer(), artifacts,
    )
    return TestClient(create_app(store, deps, api_token=TOKEN)), store, artifacts


def _seed(store: SqliteJobStore, artifacts: LocalArtifactStore, owner: str, ext: str = "mp3") -> str:
    job_id = store.create(owner=owner)
    store.save(_done_job(job_id, owner, ext))
    artifacts.put(f"episode_{job_id}.{ext}", AUDIO if ext == "mp3" else b"text", "audio/mpeg")
    return job_id


def test_feed_end_to_end_without_a_bearer_token(tmp_path: Path) -> None:
    client, store, artifacts = _client(tmp_path)
    job_id = _seed(store, artifacts, MASTER_OWNER)
    _seed(store, artifacts, MASTER_OWNER, ext="txt")  # placeholder audio: not in the feed

    assert client.get("/feed").status_code == 401  # discovering the URL needs auth
    mine = client.get("/feed", headers=AUTH).json()
    assert mine["episodes"] == 1
    path = mine["feed_url"].replace("http://testserver", "")
    token = feed_token(MASTER_OWNER)
    assert path == f"/feed/{token}.xml"

    feed = client.get(path)
    assert feed.status_code == 200 and feed.headers["content-type"].startswith("application/rss+xml")
    [item] = ET.fromstring(feed.text).findall("channel/item")
    enclosure = item.find("enclosure")
    assert enclosure is not None and enclosure.get("length") == str(len(AUDIO))

    audio = client.get(f"/feed/{token}/{job_id}.mp3")
    assert audio.status_code == 200 and audio.content == AUDIO
    partial = client.get(f"/feed/{token}/{job_id}.mp3", headers={"Range": "bytes=0-9"})
    assert partial.status_code == 206 and partial.content == AUDIO[:10]
    assert partial.headers["content-range"] == f"bytes 0-9/{len(AUDIO)}"
    too_far = client.get(f"/feed/{token}/{job_id}.mp3", headers={"Range": f"bytes={len(AUDIO)}-"})
    assert too_far.status_code == 416

    chapters = client.get(f"/feed/{token}/{job_id}/chapters.json")
    assert chapters.json()["chapters"] == [{"startTime": 0.0, "title": "Intro"}]
    transcript = client.get(f"/feed/{token}/{job_id}/transcript.txt")
    assert transcript.text == "The whole episode, as text."


def test_feed_routes_404_on_a_wrong_token_or_another_owners_job(tmp_path: Path) -> None:
    client, store, artifacts = _client(tmp_path)
    mine = _seed(store, artifacts, MASTER_OWNER)
    theirs = _seed(store, artifacts, "pat@example.com")
    token = feed_token(MASTER_OWNER)
    assert client.get(f"/feed/{'0' * 40}.xml").status_code == 404
    assert client.get(f"/feed/{'0' * 40}/{mine}.mp3").status_code == 404
    assert client.get(f"/feed/{token}/{theirs}.mp3").status_code == 404
    assert client.get(f"/feed/{token}/{theirs}/transcript.txt").status_code == 404
    their_token = feed_token("pat@example.com")
    assert client.get(f"/feed/{their_token}/{theirs}.mp3").status_code == 200


# --- the local feed and the store ---------------------------------------------------


def test_write_local_feed_uses_file_enclosures(tmp_path: Path) -> None:
    store = SqliteJobStore(tmp_path / "jobs.db")
    artifacts = LocalArtifactStore(tmp_path / "artifacts")
    _seed(store, artifacts, MASTER_OWNER)
    out, count = write_local_feed(store, tmp_path / "artifacts", tmp_path / "feed.xml")
    store.close()
    assert count == 1
    enclosure = ET.parse(out).getroot().find("channel/item/enclosure")
    assert enclosure is not None and enclosure.get("url", "").startswith("file://")


def test_list_finished_newest_first_mp3_only(tmp_path: Path) -> None:
    store = SqliteJobStore(tmp_path / "jobs.db")
    first = store.create()
    store.save(_done_job(first))
    second = store.create()
    store.save(_done_job(second))
    text = store.create()
    store.save(_done_job(text, ext="txt"))
    other = store.create(owner="pat@example.com")
    store.save(_done_job(other, owner="pat@example.com"))
    queued = store.create()

    finished = store.list_finished(MASTER_OWNER, 10)
    ids = [f.job.job_id for f in finished]
    assert set(ids) == {first, second} and queued not in ids
    assert finished[0].created_at >= finished[1].created_at
    assert len(store.list_finished(MASTER_OWNER, 1)) == 1
    assert store.owners() == ["master", "pat@example.com"]
    store.close()
