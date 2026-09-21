"""Phase C — provider ladder, caching, and the ingest/API integration points
that lean on it. All HTTP is mocked (monkeypatching httpx.get/httpx.post on
chorus.transcripts) — nothing here touches the network.
"""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from chorus import transcripts as tc
from chorus.app import create_app
from chorus.audio import MockAudioRenderer
from chorus.ingest import ingest
from chorus.jobs import JobStore
from chorus.llm import MockLLMClient
from chorus.models import EpisodeInput
from chorus.pipeline import Deps, default_deps
from chorus.script import MockScriptComposer
from chorus.transcript_cache import CachingTranscriptProvider, SqliteTranscriptCache
from chorus.transcripts import (
    ChainTranscriptProvider,
    DeepgramTranscriptProvider,
    FixtureTranscriptProvider,
    ManagedCaptionsProvider,
    RssTranscriptProvider,
    TranscriptNotFound,
    TranscriptProviderError,
    extract_video_id,
)

# --------------------------------------------------------------------------
# extract_video_id regressions
# --------------------------------------------------------------------------


def test_v_param_matches_only_as_a_query_param() -> None:
    assert extract_video_id("https://www.youtube.com/watch?v=abcdefghijk") == "abcdefghijk"
    assert extract_video_id("https://www.youtube.com/watch?x=1&v=abcdefghijk") == "abcdefghijk"


def test_v_substring_in_other_param_does_not_match() -> None:
    # "nav=" contains "v=" as a substring; the old regex matched it by mistake.
    with pytest.raises(ValueError):
        extract_video_id("https://example.com/?nav=abcdefghijk")


def test_live_urls_parse() -> None:
    assert extract_video_id("https://www.youtube.com/live/abcdefghijk") == "abcdefghijk"
    assert extract_video_id("youtube.com/live/abcdefghijk?feature=share") == "abcdefghijk"


def test_existing_forms_still_parse() -> None:
    assert extract_video_id("https://youtu.be/abcdefghijk") == "abcdefghijk"
    assert extract_video_id("https://www.youtube.com/shorts/abcdefghijk") == "abcdefghijk"
    assert extract_video_id("https://www.youtube.com/embed/abcdefghijk") == "abcdefghijk"
    assert extract_video_id("abcdefghijk") == "abcdefghijk"


# --------------------------------------------------------------------------
# EpisodeInput.resolved_id — RSS identity
# --------------------------------------------------------------------------


def test_resolved_id_stable_for_same_guid() -> None:
    a = EpisodeInput(feed_url="https://feed.example/rss.xml", guid="ep-1")
    b = EpisodeInput(feed_url="https://feed.example/rss.xml", guid="ep-1")
    assert a.resolved_id() == b.resolved_id()
    assert a.resolved_id().startswith("rss-")


def test_resolved_id_differs_for_different_guid() -> None:
    a = EpisodeInput(feed_url="https://feed.example/rss.xml", guid="ep-1")
    b = EpisodeInput(feed_url="https://feed.example/rss.xml", guid="ep-2")
    assert a.resolved_id() != b.resolved_id()


def test_resolved_id_falls_back_to_audio_url() -> None:
    ep = EpisodeInput(audio_url="https://cdn.example/ep1.mp3")
    assert ep.resolved_id().startswith("rss-")


def test_resolved_id_raises_when_nothing_identifies_episode() -> None:
    with pytest.raises(ValueError):
        EpisodeInput().resolved_id()


# --------------------------------------------------------------------------
# FixtureTranscriptProvider — Protocol signature change, source tag
# --------------------------------------------------------------------------


def test_fixture_provider_takes_episode_input_and_tags_source() -> None:
    provider = FixtureTranscriptProvider()
    transcript = provider.get(EpisodeInput(video_id="c4tvVKDhpiY"))
    assert transcript.video_id == "c4tvVKDhpiY"
    assert transcript.source == "fixture"


def test_fixture_provider_not_found_raises() -> None:
    provider = FixtureTranscriptProvider()
    with pytest.raises(TranscriptNotFound):
        provider.get(EpisodeInput(video_id="ZZZZZZZZZZZ"))


# --------------------------------------------------------------------------
# ManagedCaptionsProvider (Supadata)
# --------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, status_code: int, payload: object = None, text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = text
        self.content = text.encode("utf-8")

    def json(self) -> object:
        return self._payload


def test_managed_captions_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_get(url: str, **kwargs: object) -> _FakeResponse:
        assert url == f"{tc.SUPADATA_BASE_URL}/youtube/transcript"
        assert kwargs["params"] == {"videoId": "abcdefghijk"}
        assert kwargs["headers"] == {"x-api-key": "sk-test"}
        return _FakeResponse(
            200,
            {
                "content": [
                    {"text": "hello world", "offset": 1000, "duration": 2000},
                    {"text": "second line", "offset": 3000, "duration": 2000},
                ],
                "lang": "en",
            },
        )

    monkeypatch.setattr(tc.httpx, "get", fake_get)
    provider = ManagedCaptionsProvider("sk-test")
    transcript = provider.get(EpisodeInput(video_id="abcdefghijk"))
    assert transcript.source == "supadata"
    assert transcript.segments[0].start == 1.0
    assert transcript.segments[0].text == "hello world"
    assert transcript.segments[1].start == 3.0


def test_managed_captions_404_is_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tc.httpx, "get", lambda *a, **kw: _FakeResponse(404))
    provider = ManagedCaptionsProvider("sk-test")
    with pytest.raises(TranscriptNotFound):
        provider.get(EpisodeInput(video_id="abcdefghijk"))


def test_managed_captions_empty_content_is_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tc.httpx, "get", lambda *a, **kw: _FakeResponse(200, {"content": []}))
    provider = ManagedCaptionsProvider("sk-test")
    with pytest.raises(TranscriptNotFound):
        provider.get(EpisodeInput(video_id="abcdefghijk"))


@pytest.mark.parametrize("status", [401, 403, 500, 503])
def test_managed_captions_auth_and_server_errors_are_provider_errors(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    monkeypatch.setattr(tc.httpx, "get", lambda *a, **kw: _FakeResponse(status))
    provider = ManagedCaptionsProvider("sk-test")
    with pytest.raises(TranscriptProviderError):
        provider.get(EpisodeInput(video_id="abcdefghijk"))


def test_managed_captions_timeout_is_provider_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_get(*a: object, **kw: object) -> _FakeResponse:
        raise httpx.TimeoutException("timed out")

    monkeypatch.setattr(tc.httpx, "get", fake_get)
    provider = ManagedCaptionsProvider("sk-test")
    with pytest.raises(TranscriptProviderError):
        provider.get(EpisodeInput(video_id="abcdefghijk"))


def test_managed_captions_skips_rss_episodes() -> None:
    # No feed_url/guid means no YouTube id either -> resolved_id() raises ->
    # NotFound before any HTTP call is attempted.
    provider = ManagedCaptionsProvider("sk-test")
    with pytest.raises(TranscriptNotFound):
        provider.get(EpisodeInput(feed_url="https://feed.example/rss.xml", guid="ep-1"))


# --------------------------------------------------------------------------
# RssTranscriptProvider — item matching + JSON/VTT/SRT parsing
# --------------------------------------------------------------------------

_FEED_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:podcast="https://podcastindex.org/namespace/1.0">
  <channel>
    <title>Test Show</title>
    <item>
      <title>Episode 1</title>
      <guid>ep-guid-1</guid>
      <enclosure url="https://cdn.example.com/ep1.mp3" type="audio/mpeg" length="1000"/>
      {transcripts}
    </item>
    <item>
      <title>Episode 2 (no transcript)</title>
      <guid>ep-guid-2</guid>
      <enclosure url="https://cdn.example.com/ep2.mp3" type="audio/mpeg" length="1000"/>
    </item>
  </channel>
</rss>
"""

_JSON_TRANSCRIPT = json.dumps(
    {
        "version": "1.0.0",
        "segments": [
            {"speaker": "Host", "startTime": 1.0, "endTime": 4.0, "body": "hello world"},
            {"speaker": "Host", "startTime": 4.5, "endTime": 7.0, "body": "second line"},
        ],
    }
)

_VTT_TRANSCRIPT = (
    "WEBVTT\n\n"
    "00:00:01.000 --> 00:00:04.000\n"
    "hello world\n\n"
    "00:00:04.500 --> 00:00:07.000\n"
    "second line\n"
)

_SRT_TRANSCRIPT = (
    "1\n00:00:01,000 --> 00:00:04,000\nhello world\n\n"
    "2\n00:00:04,500 --> 00:00:07,000\nsecond line\n"
)


def _feed_with(transcript_tags: str) -> str:
    return _FEED_TEMPLATE.format(transcripts=transcript_tags)


def _mock_urls(monkeypatch: pytest.MonkeyPatch, mapping: dict[str, _FakeResponse]) -> None:
    def fake_get(url: str, **kwargs: object) -> _FakeResponse:
        if url not in mapping:
            raise AssertionError(f"unexpected URL fetched: {url}")
        return mapping[url]

    monkeypatch.setattr(tc.httpx, "get", fake_get)


def test_rss_prefers_json_over_vtt_and_srt(monkeypatch: pytest.MonkeyPatch) -> None:
    feed = _feed_with(
        '<podcast:transcript url="https://cdn.example.com/ep1.json" type="application/json"/>'
        '<podcast:transcript url="https://cdn.example.com/ep1.vtt" type="text/vtt"/>'
    )
    _mock_urls(
        monkeypatch,
        {
            "https://feed.example/rss.xml": _FakeResponse(200, text=feed),
            "https://cdn.example.com/ep1.json": _FakeResponse(200, text=_JSON_TRANSCRIPT),
        },
    )
    provider = RssTranscriptProvider()
    transcript = provider.get(
        EpisodeInput(feed_url="https://feed.example/rss.xml", guid="ep-guid-1")
    )
    assert transcript.source == "rss:json"
    assert transcript.segments[0].start == 1.0
    assert transcript.segments[0].text == "hello world"


def test_rss_falls_back_to_vtt(monkeypatch: pytest.MonkeyPatch) -> None:
    feed = _feed_with('<podcast:transcript url="https://cdn.example.com/ep1.vtt" type="text/vtt"/>')
    _mock_urls(
        monkeypatch,
        {
            "https://feed.example/rss.xml": _FakeResponse(200, text=feed),
            "https://cdn.example.com/ep1.vtt": _FakeResponse(200, text=_VTT_TRANSCRIPT),
        },
    )
    provider = RssTranscriptProvider()
    transcript = provider.get(
        EpisodeInput(feed_url="https://feed.example/rss.xml", guid="ep-guid-1")
    )
    assert transcript.source == "rss:vtt"
    assert transcript.segments[1].start == 4.5
    assert transcript.segments[1].text == "second line"


def test_rss_parses_srt(monkeypatch: pytest.MonkeyPatch) -> None:
    feed = _feed_with(
        '<podcast:transcript url="https://cdn.example.com/ep1.srt" type="application/srt"/>'
    )
    _mock_urls(
        monkeypatch,
        {
            "https://feed.example/rss.xml": _FakeResponse(200, text=feed),
            "https://cdn.example.com/ep1.srt": _FakeResponse(200, text=_SRT_TRANSCRIPT),
        },
    )
    provider = RssTranscriptProvider()
    transcript = provider.get(
        EpisodeInput(feed_url="https://feed.example/rss.xml", guid="ep-guid-1")
    )
    assert transcript.source == "rss:srt"
    assert len(transcript.segments) == 2
    assert transcript.segments[0].start == 1.0


def test_rss_matches_item_by_enclosure_when_guid_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    feed = _feed_with(
        '<podcast:transcript url="https://cdn.example.com/ep1.json" type="application/json"/>'
    )
    _mock_urls(
        monkeypatch,
        {
            "https://feed.example/rss.xml": _FakeResponse(200, text=feed),
            "https://cdn.example.com/ep1.json": _FakeResponse(200, text=_JSON_TRANSCRIPT),
        },
    )
    provider = RssTranscriptProvider()
    transcript = provider.get(
        EpisodeInput(
            feed_url="https://feed.example/rss.xml",
            audio_url="https://cdn.example.com/ep1.mp3",
        )
    )
    assert transcript.source == "rss:json"


def test_rss_no_transcript_tag_is_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    feed = _feed_with("")
    _mock_urls(monkeypatch, {"https://feed.example/rss.xml": _FakeResponse(200, text=feed)})
    provider = RssTranscriptProvider()
    with pytest.raises(TranscriptNotFound):
        provider.get(EpisodeInput(feed_url="https://feed.example/rss.xml", guid="ep-guid-1"))


def test_rss_no_item_match_is_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    feed = _feed_with("")
    _mock_urls(monkeypatch, {"https://feed.example/rss.xml": _FakeResponse(200, text=feed)})
    provider = RssTranscriptProvider()
    with pytest.raises(TranscriptNotFound):
        provider.get(EpisodeInput(feed_url="https://feed.example/rss.xml", guid="no-such-guid"))


def test_rss_server_error_is_provider_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_urls(monkeypatch, {"https://feed.example/rss.xml": _FakeResponse(500)})
    provider = RssTranscriptProvider()
    with pytest.raises(TranscriptProviderError):
        provider.get(EpisodeInput(feed_url="https://feed.example/rss.xml", guid="ep-guid-1"))


def test_rss_enclosure_audio_url_helper(monkeypatch: pytest.MonkeyPatch) -> None:
    feed = _feed_with("")
    _mock_urls(monkeypatch, {"https://feed.example/rss.xml": _FakeResponse(200, text=feed)})
    provider = RssTranscriptProvider()
    url = provider.enclosure_audio_url(
        EpisodeInput(feed_url="https://feed.example/rss.xml", guid="ep-guid-1")
    )
    assert url == "https://cdn.example.com/ep1.mp3"


def test_rss_enclosure_audio_url_prefers_direct_audio_url() -> None:
    provider = RssTranscriptProvider()
    url = provider.enclosure_audio_url(EpisodeInput(audio_url="https://direct.example/a.mp3"))
    assert url == "https://direct.example/a.mp3"


# --------------------------------------------------------------------------
# DeepgramTranscriptProvider
# --------------------------------------------------------------------------


def test_deepgram_happy_path_with_utterances(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_post(url: str, **kwargs: object) -> _FakeResponse:
        assert url == tc.DEEPGRAM_LISTEN_URL
        assert kwargs["headers"]["Authorization"] == "Token dg-test"  # type: ignore[index]
        assert kwargs["json"] == {"url": "https://cdn.example.com/ep1.mp3"}
        return _FakeResponse(
            200,
            {
                "results": {
                    "utterances": [
                        {"start": 0.5, "end": 3.0, "transcript": "hello world"},
                        {"start": 3.5, "end": 6.0, "transcript": "second line"},
                    ]
                }
            },
        )

    monkeypatch.setattr(tc.httpx, "post", fake_post)
    provider = DeepgramTranscriptProvider("dg-test")
    transcript = provider.get(
        EpisodeInput(video_id="abcdefghijk", audio_url="https://cdn.example.com/ep1.mp3")
    )
    assert transcript.source == "deepgram"
    assert transcript.segments[0].start == 0.5
    assert transcript.segments[0].text == "hello world"


def test_deepgram_falls_back_to_grouped_words(monkeypatch: pytest.MonkeyPatch) -> None:
    words = [
        {"word": "hello", "start": 0.0, "end": 0.3},
        {"word": "world", "start": 0.4, "end": 0.8},
        {"word": "later", "start": 12.0, "end": 12.4},
    ]

    def fake_post(url: str, **kwargs: object) -> _FakeResponse:
        return _FakeResponse(
            200,
            {
                "results": {
                    "utterances": [],
                    "channels": [{"alternatives": [{"words": words}]}],
                }
            },
        )

    monkeypatch.setattr(tc.httpx, "post", fake_post)
    provider = DeepgramTranscriptProvider("dg-test")
    transcript = provider.get(
        EpisodeInput(video_id="abcdefghijk", audio_url="https://cdn.example.com/ep1.mp3")
    )
    assert len(transcript.segments) == 2  # window split at the >=10s gap
    assert transcript.segments[0].text == "hello world"
    assert transcript.segments[1].text == "later"


def test_deepgram_resolves_audio_url_from_rss_enclosure(monkeypatch: pytest.MonkeyPatch) -> None:
    feed = _feed_with("")
    calls: list[str] = []

    def fake_get(url: str, **kwargs: object) -> _FakeResponse:
        calls.append(url)
        return _FakeResponse(200, text=feed)

    def fake_post(url: str, **kwargs: object) -> _FakeResponse:
        assert kwargs["json"] == {"url": "https://cdn.example.com/ep1.mp3"}
        return _FakeResponse(
            200, {"results": {"utterances": [{"start": 0.0, "transcript": "hi"}]}}
        )

    monkeypatch.setattr(tc.httpx, "get", fake_get)
    monkeypatch.setattr(tc.httpx, "post", fake_post)
    provider = DeepgramTranscriptProvider("dg-test")
    transcript = provider.get(
        EpisodeInput(feed_url="https://feed.example/rss.xml", guid="ep-guid-1")
    )
    assert transcript.source == "deepgram"
    assert calls  # the RSS feed was fetched to find the enclosure


def test_deepgram_no_audio_url_is_not_found() -> None:
    provider = DeepgramTranscriptProvider("dg-test")
    with pytest.raises(TranscriptNotFound):
        provider.get(EpisodeInput(video_id="abcdefghijk"))


@pytest.mark.parametrize("status", [401, 500])
def test_deepgram_auth_and_server_errors_are_provider_errors(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    monkeypatch.setattr(tc.httpx, "post", lambda *a, **kw: _FakeResponse(status))
    provider = DeepgramTranscriptProvider("dg-test")
    with pytest.raises(TranscriptProviderError):
        provider.get(EpisodeInput(video_id="x", audio_url="https://cdn.example.com/ep1.mp3"))


def test_deepgram_timeout_is_provider_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_post(*a: object, **kw: object) -> _FakeResponse:
        raise httpx.TimeoutException("timed out")

    monkeypatch.setattr(tc.httpx, "post", fake_post)
    provider = DeepgramTranscriptProvider("dg-test")
    with pytest.raises(TranscriptProviderError):
        provider.get(EpisodeInput(video_id="x", audio_url="https://cdn.example.com/ep1.mp3"))


# --------------------------------------------------------------------------
# ChainTranscriptProvider
# --------------------------------------------------------------------------


class _RaisingNotFound:
    def get(self, episode: EpisodeInput) -> object:
        raise TranscriptNotFound("nope, not here")


class _RaisingProviderError:
    def get(self, episode: EpisodeInput) -> object:
        raise TranscriptProviderError("outage")


class _Succeeds:
    def get(self, episode: EpisodeInput) -> object:
        from chorus.models import Transcript

        return Transcript(video_id="x", segments=[], source="stub")


def test_chain_falls_through_notfound_and_providererror_in_order() -> None:
    chain = ChainTranscriptProvider([_RaisingNotFound(), _RaisingProviderError(), _Succeeds()])
    transcript = chain.get(EpisodeInput(video_id="x"))
    assert transcript.source == "stub"  # type: ignore[union-attr]


def test_chain_all_fail_raises_with_aggregated_reasons() -> None:
    chain = ChainTranscriptProvider([_RaisingNotFound(), _RaisingProviderError()])
    with pytest.raises(TranscriptNotFound) as excinfo:
        chain.get(EpisodeInput(video_id="x"))
    message = str(excinfo.value)
    assert "_RaisingNotFound" in message
    assert "_RaisingProviderError" in message
    assert "nope, not here" in message
    assert "outage" in message


def test_chain_empty_providers_raises() -> None:
    chain = ChainTranscriptProvider([])
    with pytest.raises(TranscriptNotFound):
        chain.get(EpisodeInput(video_id="x"))


# --------------------------------------------------------------------------
# Transcript cache
# --------------------------------------------------------------------------


class _CountingProvider:
    def __init__(self) -> None:
        self.calls = 0

    def get(self, episode: EpisodeInput) -> object:
        from chorus.models import Transcript

        self.calls += 1
        return Transcript(video_id=episode.resolved_id(), segments=[], source="counted")


def test_cache_miss_calls_inner_and_stores(tmp_path: Path) -> None:
    cache = SqliteTranscriptCache(tmp_path / "cache.db")
    inner = _CountingProvider()
    provider = CachingTranscriptProvider(inner, cache)
    transcript = provider.get(EpisodeInput(video_id="abc"))
    assert inner.calls == 1
    assert transcript.source == "counted"  # type: ignore[union-attr]
    assert cache.get("abc") is not None


def test_cache_hit_skips_inner_provider(tmp_path: Path) -> None:
    cache = SqliteTranscriptCache(tmp_path / "cache.db")
    inner = _CountingProvider()
    provider = CachingTranscriptProvider(inner, cache)
    provider.get(EpisodeInput(video_id="abc"))
    provider.get(EpisodeInput(video_id="abc"))
    assert inner.calls == 1  # second call served entirely from cache


def test_cache_persists_across_instances_on_same_db(tmp_path: Path) -> None:
    db_path = tmp_path / "cache.db"
    cache1 = SqliteTranscriptCache(db_path)
    CachingTranscriptProvider(_CountingProvider(), cache1).get(EpisodeInput(video_id="abc"))

    cache2 = SqliteTranscriptCache(db_path)
    assert cache2.get("abc") is not None


# --------------------------------------------------------------------------
# ingest() records provenance
# --------------------------------------------------------------------------


def test_ingest_records_transcript_source() -> None:
    result = ingest([EpisodeInput(video_id="c4tvVKDhpiY")], FixtureTranscriptProvider())
    assert result.resolved[0].transcript.source == "fixture"


class _NotFoundThenSucceeds:
    """First episode's id triggers a provider outage; the rest resolve fine."""

    def get(self, episode: EpisodeInput) -> object:
        if episode.video_id == "bad":
            raise TranscriptProviderError("outage")
        return FixtureTranscriptProvider().get(episode)


def test_ingest_treats_provider_error_as_a_skip_not_a_hard_failure() -> None:
    result = ingest(
        [EpisodeInput(video_id="bad"), EpisodeInput(video_id="c4tvVKDhpiY")],
        _NotFoundThenSucceeds(),
    )
    assert len(result.resolved) == 1
    assert len(result.skipped) == 1
    assert result.skipped[0].episode.video_id == "bad"
    assert "outage" in result.skipped[0].reason


# --------------------------------------------------------------------------
# Full API lifecycle over a NON-fixture episode via a mocked managed-captions
# provider — the Phase C exit criterion.
# --------------------------------------------------------------------------

NON_FIXTURE_VIDEO_ID = "zzzzzzzzzzz"


def test_digest_over_non_fixture_episode_reaches_done(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_get(url: str, **kwargs: object) -> _FakeResponse:
        assert kwargs["params"] == {"videoId": NON_FIXTURE_VIDEO_ID}
        return _FakeResponse(
            200,
            {
                "content": [
                    {"text": s, "offset": i * 90_000, "duration": 90_000}
                    for i, s in enumerate(
                        [
                            "the market moved on inflation data today",
                            "investors weighed the central bank outlook",
                            "earnings season begins next week for tech",
                        ]
                    )
                ]
            },
        )

    monkeypatch.setattr(tc.httpx, "get", fake_get)

    chain = ChainTranscriptProvider(
        [FixtureTranscriptProvider(), ManagedCaptionsProvider("sk-test")]
    )
    cache = SqliteTranscriptCache(tmp_path / "cache.db")
    deps = Deps(
        provider=CachingTranscriptProvider(chain, cache),
        llm=MockLLMClient(),
        composer=MockScriptComposer(),
        renderer=MockAudioRenderer(out_dir=tmp_path / "artifacts"),
    )
    client = TestClient(create_app(JobStore(tmp_path / "jobs.db"), deps))

    FIX = Path(__file__).resolve().parent.parent / "fixtures"
    payload = {
        "soul": (FIX / "souls" / "soul_investor.md").read_text(encoding="utf-8"),
        "context": (FIX / "context.md").read_text(encoding="utf-8"),
        "episodes": [{"video_id": NON_FIXTURE_VIDEO_ID}],
        "highlight_count": 4,
    }
    job_id = client.post("/digest", json=payload).json()["job_id"]
    body = client.get(f"/digest/{job_id}").json()

    assert body["status"] == "done"
    assert body["usage"]["transcript_sources"] == {NON_FIXTURE_VIDEO_ID: "supadata"}
    assert "ingest" in body["usage"]["stage_seconds"]
    assert "curate" in body["usage"]["stage_seconds"]


# --------------------------------------------------------------------------
# default_deps() chain composition from the environment
# --------------------------------------------------------------------------


def _unwrap_chain(deps: Deps) -> list[object]:
    caching = deps.provider
    assert isinstance(caching, CachingTranscriptProvider)
    chain = caching.inner
    assert isinstance(chain, ChainTranscriptProvider)
    return chain.providers


def test_default_deps_fixture_only_without_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ("TRANSCRIPT_API_KEY", "DEEPGRAM_API_KEY", "ANTHROPIC_API_KEY", "ELEVENLABS_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    providers = _unwrap_chain(default_deps())
    kinds = [type(p).__name__ for p in providers]
    assert kinds == ["FixtureTranscriptProvider", "RssTranscriptProvider"]


def test_default_deps_adds_supadata_and_deepgram_when_keyed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRANSCRIPT_API_KEY", "sk-test")
    monkeypatch.setenv("DEEPGRAM_API_KEY", "dg-test")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    providers = _unwrap_chain(default_deps())
    kinds = [type(p).__name__ for p in providers]
    assert kinds == [
        "FixtureTranscriptProvider",
        "ManagedCaptionsProvider",
        "RssTranscriptProvider",
        "DeepgramTranscriptProvider",
    ]
