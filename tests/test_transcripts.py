"""Phase C — provider ladder, caching, and the ingest/API integration points
that lean on it. All HTTP is mocked (monkeypatching httpx.get on
chorus.transcripts for Supadata, httpx.stream for RSS/Deepgram, which now go
through chorus.netguard.safe_url + a byte-bounded stream) — nothing here
touches the network or real DNS.

Also covers the docs/REVIEW_WAVE1.md remediations owned by this file:
R10 (SSRF guard + redirect revalidation), R11 (one identity family, cache
poisoning), R15 (bounded fetches), R16 (typed provider boundaries + chain
taxonomy).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Self

import httpx
import pytest
from fastapi.testclient import TestClient

from chorus import transcripts as tc
from chorus.app import create_app
from chorus.artifacts import LocalArtifactStore
from chorus.audio import MockAudioRenderer
from chorus.errors import RetryableError, TerminalError
from chorus.ingest import TRANSIENT_REASON_PREFIX, ingest
from chorus.jobs import SqliteJobStore
from chorus.llm import MockLLMClient
from chorus.models import EpisodeInput
from chorus.netguard import UnsafeURLError, safe_url
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

# A normal public unicast address — the fake resolver hands this back for any
# hostname so RSS/Deepgram tests exercise the real safe_url() allow path
# without ever touching real DNS.
_SAFE_IP = "93.184.216.34"


def _safe_resolver(host: str) -> list[str]:
    return [_SAFE_IP]


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
# EpisodeInput — R11: one identity family, canonical-feed-url resolved_id
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


def test_same_guid_in_two_different_feeds_no_longer_collides() -> None:
    # R11's headline scenario: without the feed URL in the hash, the same
    # guid in two different feeds used to resolve to the same cache key.
    a = EpisodeInput(feed_url="https://feed-a.example/rss.xml", guid="ep-1")
    b = EpisodeInput(feed_url="https://feed-b.example/rss.xml", guid="ep-1")
    assert a.resolved_id() != b.resolved_id()


def test_resolved_id_ignores_fragment_but_keeps_query_and_lowercases_host() -> None:
    a = EpisodeInput(feed_url="https://Feed.Example/rss.xml?x=1#frag", guid="ep-1")
    b = EpisodeInput(feed_url="https://feed.example/rss.xml?x=1", guid="ep-1")
    c = EpisodeInput(feed_url="https://feed.example/rss.xml?x=2", guid="ep-1")
    assert a.resolved_id() == b.resolved_id()  # case + fragment don't matter
    assert a.resolved_id() != c.resolved_id()  # query does


def test_mixing_youtube_and_rss_fields_raises() -> None:
    with pytest.raises(ValueError):
        EpisodeInput(video_id="abcdefghijk", feed_url="https://feed.example/rss.xml", guid="ep-1")
    with pytest.raises(ValueError):
        EpisodeInput(url="https://youtu.be/abcdefghijk", audio_url="https://cdn.example/a.mp3")


def test_feed_url_alone_without_guid_or_audio_url_raises() -> None:
    with pytest.raises(ValueError):
        EpisodeInput(feed_url="https://feed.example/rss.xml")


def test_no_identity_at_all_raises_at_construction() -> None:
    with pytest.raises(ValueError):
        EpisodeInput(show="Some Show")


# --------------------------------------------------------------------------
# chorus.netguard.safe_url — R10
# --------------------------------------------------------------------------


def test_safe_url_allows_https_public_host() -> None:
    assert safe_url("https://example.com/feed.xml", resolver=_safe_resolver) == (
        "https://example.com/feed.xml"
    )


def test_safe_url_rejects_plain_http_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CHORUS_ALLOW_HTTP", raising=False)
    with pytest.raises(UnsafeURLError):
        safe_url("http://example.com/feed.xml", resolver=_safe_resolver)


def test_safe_url_allows_http_when_env_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHORUS_ALLOW_HTTP", "1")
    assert safe_url("http://example.com/feed.xml", resolver=_safe_resolver) == (
        "http://example.com/feed.xml"
    )


@pytest.mark.parametrize(
    "host",
    [
        "127.0.0.1",
        "169.254.169.254",  # cloud metadata range
        "10.0.0.5",
        "192.168.1.1",
        "0.0.0.0",
        "[::1]",
    ],
)
def test_safe_url_rejects_disallowed_ip_literals(host: str) -> None:
    with pytest.raises(UnsafeURLError):
        safe_url(f"https://{host}/x", resolver=_safe_resolver)


@pytest.mark.parametrize(
    "host", ["localhost", "metadata.google.internal", "svc.internal", "printer.local"]
)
def test_safe_url_rejects_blocked_hostnames(host: str) -> None:
    with pytest.raises(UnsafeURLError):
        safe_url(f"https://{host}/x", resolver=_safe_resolver)


def test_safe_url_rejects_hostname_resolving_to_private_address() -> None:
    with pytest.raises(UnsafeURLError):
        safe_url("https://sneaky.example/x", resolver=lambda host: ["10.1.2.3"])


def test_safe_url_rejects_oversized_url() -> None:
    huge = "https://example.com/" + ("a" * 3000)
    with pytest.raises(UnsafeURLError):
        safe_url(huge, resolver=_safe_resolver)


def test_safe_url_rejects_unresolvable_host() -> None:
    def _fail(host: str) -> list[str]:
        raise OSError("nxdomain")

    with pytest.raises(UnsafeURLError):
        safe_url("https://nowhere.example/x", resolver=_fail)


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
# ManagedCaptionsProvider (Supadata) — unchanged transport (plain httpx.get);
# now parsed through a typed boundary model (R16).
# --------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, status_code: int, payload: object = None, text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = text
        self.content = text.encode("utf-8")

    def json(self) -> object:
        if self._payload is _RAISES_JSON_ERROR:
            raise json.JSONDecodeError("bad json", "doc", 0)
        return self._payload


_RAISES_JSON_ERROR = object()


def test_managed_captions_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_get(url: str, **kwargs: object) -> _FakeResponse:
        assert url == f"{tc.SUPADATA_BASE_URL}/transcript"
        assert kwargs["params"] == {
            "url": "https://www.youtube.com/watch?v=abcdefghijk",
            "mode": "native",
            "text": "false",
        }
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


def test_managed_captions_malformed_field_is_provider_error(monkeypatch: pytest.MonkeyPatch) -> None:
    # R16: a boundary-model validation failure (offset isn't numeric) becomes
    # TranscriptProviderError, not an escaping exception or a silent NotFound.
    monkeypatch.setattr(
        tc.httpx,
        "get",
        lambda *a, **kw: _FakeResponse(200, {"content": [{"text": "x", "offset": "nope"}]}),
    )
    provider = ManagedCaptionsProvider("sk-test")
    with pytest.raises(TranscriptProviderError):
        provider.get(EpisodeInput(video_id="abcdefghijk"))


def test_managed_captions_invalid_json_is_provider_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tc.httpx, "get", lambda *a, **kw: _FakeResponse(200, _RAISES_JSON_ERROR))
    provider = ManagedCaptionsProvider("sk-test")
    with pytest.raises(TranscriptProviderError):
        provider.get(EpisodeInput(video_id="abcdefghijk"))


def test_managed_captions_oversized_response_is_provider_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Huge(_FakeResponse):
        def __init__(self) -> None:
            super().__init__(200, {"content": []})
            self.content = b"x" * (tc.MAX_TRANSCRIPT_BYTES + 1)

    monkeypatch.setattr(tc.httpx, "get", lambda *a, **kw: _Huge())
    provider = ManagedCaptionsProvider("sk-test")
    with pytest.raises(TranscriptProviderError):
        provider.get(EpisodeInput(video_id="abcdefghijk"))


# --------------------------------------------------------------------------
# RssTranscriptProvider — item matching + JSON/VTT/SRT parsing.
# Transport is httpx.stream now (SSRF-guarded, byte-bounded); mocked with a
# minimal fake context-manager response.
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


class _FakeStreamResponse:
    """Just enough of httpx's `stream()` context-manager result for
    `_fetch_bounded`: status/headers, plus `iter_bytes()` split into a
    couple of chunks so the accumulate-and-cap loop is actually exercised."""

    def __init__(
        self, status_code: int, body: bytes = b"", headers: dict[str, str] | None = None
    ) -> None:
        self.status_code = status_code
        self._body = body
        self.headers = headers or {}

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def iter_bytes(self):
        mid = len(self._body) // 2
        if mid:
            yield self._body[:mid]
            yield self._body[mid:]
        elif self._body:
            yield self._body


def _stream_text(status_code: int, text: str, headers: dict[str, str] | None = None) -> _FakeStreamResponse:
    return _FakeStreamResponse(status_code, text.encode("utf-8"), headers)


def _mock_stream(monkeypatch: pytest.MonkeyPatch, mapping: dict[str, _FakeStreamResponse]) -> None:
    """Map GET url -> fake response. Raises if a URL not in the map (or a
    POST) is streamed, so an unexpected/unsafe fetch fails loudly."""

    def fake_stream(method: str, url: str, **kwargs: object) -> _FakeStreamResponse:
        if method != "GET" or url not in mapping:
            raise AssertionError(f"unexpected stream: {method} {url}")
        return mapping[url]

    monkeypatch.setattr(tc.httpx, "stream", fake_stream)


def test_rss_prefers_json_over_vtt_and_srt(monkeypatch: pytest.MonkeyPatch) -> None:
    feed = _feed_with(
        '<podcast:transcript url="https://cdn.example.com/ep1.json" type="application/json"/>'
        '<podcast:transcript url="https://cdn.example.com/ep1.vtt" type="text/vtt"/>'
    )
    _mock_stream(
        monkeypatch,
        {
            "https://feed.example/rss.xml": _stream_text(200, feed),
            "https://cdn.example.com/ep1.json": _stream_text(200, _JSON_TRANSCRIPT),
        },
    )
    provider = RssTranscriptProvider(resolver=_safe_resolver)
    transcript = provider.get(
        EpisodeInput(feed_url="https://feed.example/rss.xml", guid="ep-guid-1")
    )
    assert transcript.source == "rss:json"
    assert transcript.segments[0].start == 1.0
    assert transcript.segments[0].text == "hello world"


def test_rss_falls_back_to_vtt(monkeypatch: pytest.MonkeyPatch) -> None:
    feed = _feed_with('<podcast:transcript url="https://cdn.example.com/ep1.vtt" type="text/vtt"/>')
    _mock_stream(
        monkeypatch,
        {
            "https://feed.example/rss.xml": _stream_text(200, feed),
            "https://cdn.example.com/ep1.vtt": _stream_text(200, _VTT_TRANSCRIPT),
        },
    )
    provider = RssTranscriptProvider(resolver=_safe_resolver)
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
    _mock_stream(
        monkeypatch,
        {
            "https://feed.example/rss.xml": _stream_text(200, feed),
            "https://cdn.example.com/ep1.srt": _stream_text(200, _SRT_TRANSCRIPT),
        },
    )
    provider = RssTranscriptProvider(resolver=_safe_resolver)
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
    _mock_stream(
        monkeypatch,
        {
            "https://feed.example/rss.xml": _stream_text(200, feed),
            "https://cdn.example.com/ep1.json": _stream_text(200, _JSON_TRANSCRIPT),
        },
    )
    provider = RssTranscriptProvider(resolver=_safe_resolver)
    transcript = provider.get(
        EpisodeInput(
            feed_url="https://feed.example/rss.xml",
            audio_url="https://cdn.example.com/ep1.mp3",
        )
    )
    assert transcript.source == "rss:json"


def test_rss_no_transcript_tag_is_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    feed = _feed_with("")
    _mock_stream(monkeypatch, {"https://feed.example/rss.xml": _stream_text(200, feed)})
    provider = RssTranscriptProvider(resolver=_safe_resolver)
    with pytest.raises(TranscriptNotFound):
        provider.get(EpisodeInput(feed_url="https://feed.example/rss.xml", guid="ep-guid-1"))


def test_rss_no_item_match_is_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    feed = _feed_with("")
    _mock_stream(monkeypatch, {"https://feed.example/rss.xml": _stream_text(200, feed)})
    provider = RssTranscriptProvider(resolver=_safe_resolver)
    with pytest.raises(TranscriptNotFound):
        provider.get(EpisodeInput(feed_url="https://feed.example/rss.xml", guid="no-such-guid"))


def test_rss_server_error_is_provider_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_stream(monkeypatch, {"https://feed.example/rss.xml": _FakeStreamResponse(500)})
    provider = RssTranscriptProvider(resolver=_safe_resolver)
    with pytest.raises(TranscriptProviderError):
        provider.get(EpisodeInput(feed_url="https://feed.example/rss.xml", guid="ep-guid-1"))


def test_rss_enclosure_audio_url_helper(monkeypatch: pytest.MonkeyPatch) -> None:
    feed = _feed_with("")
    _mock_stream(monkeypatch, {"https://feed.example/rss.xml": _stream_text(200, feed)})
    provider = RssTranscriptProvider(resolver=_safe_resolver)
    url = provider.enclosure_audio_url(
        EpisodeInput(feed_url="https://feed.example/rss.xml", guid="ep-guid-1")
    )
    assert url == "https://cdn.example.com/ep1.mp3"


def test_rss_enclosure_audio_url_prefers_direct_audio_url() -> None:
    provider = RssTranscriptProvider(resolver=_safe_resolver)
    url = provider.enclosure_audio_url(EpisodeInput(audio_url="https://direct.example/a.mp3"))
    assert url == "https://direct.example/a.mp3"


# --- R10: SSRF guard applied to RSS fetches --------------------------------


def test_rss_refuses_loopback_feed_url(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_stream(*a: object, **kw: object) -> None:
        raise AssertionError("must not reach the network for an unsafe url")

    monkeypatch.setattr(tc.httpx, "stream", fail_stream)
    provider = RssTranscriptProvider(resolver=_safe_resolver)
    with pytest.raises(TranscriptProviderError):
        provider.get(EpisodeInput(feed_url="https://127.0.0.1/rss.xml", guid="ep-1"))


def test_rss_refuses_feed_url_resolving_to_private_address(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_stream(*a: object, **kw: object) -> None:
        raise AssertionError("must not reach the network for an unsafe url")

    monkeypatch.setattr(tc.httpx, "stream", fail_stream)
    provider = RssTranscriptProvider(resolver=lambda host: ["10.0.0.9"])
    with pytest.raises(TranscriptProviderError):
        provider.get(EpisodeInput(feed_url="https://sneaky.example/rss.xml", guid="ep-1"))


def test_rss_redirect_to_unsafe_target_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_stream(method: str, url: str, **kwargs: object) -> _FakeStreamResponse:
        if url == "https://feed.example/rss.xml":
            return _FakeStreamResponse(302, headers={"location": "https://169.254.169.254/latest"})
        raise AssertionError(f"unexpected stream: {url}")

    monkeypatch.setattr(tc.httpx, "stream", fake_stream)
    provider = RssTranscriptProvider(resolver=_safe_resolver)
    with pytest.raises(TranscriptProviderError):
        provider.get(EpisodeInput(feed_url="https://feed.example/rss.xml", guid="ep-1"))


def test_rss_redirect_to_safe_target_is_followed(monkeypatch: pytest.MonkeyPatch) -> None:
    feed = _feed_with("")

    def fake_stream(method: str, url: str, **kwargs: object) -> _FakeStreamResponse:
        if url == "https://feed.example/rss.xml":
            return _FakeStreamResponse(
                301, headers={"location": "https://feed.example/rss-new.xml"}
            )
        if url == "https://feed.example/rss-new.xml":
            return _stream_text(200, feed)
        raise AssertionError(f"unexpected stream: {url}")

    monkeypatch.setattr(tc.httpx, "stream", fake_stream)
    provider = RssTranscriptProvider(resolver=_safe_resolver)
    with pytest.raises(TranscriptNotFound):  # feed has no transcript tags; proves the fetch worked
        provider.get(EpisodeInput(feed_url="https://feed.example/rss.xml", guid="ep-guid-1"))


def test_rss_too_many_redirects_is_provider_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_stream(method: str, url: str, **kwargs: object) -> _FakeStreamResponse:
        n = int(url.rsplit("-", 1)[-1]) if "-" in url else 0
        return _FakeStreamResponse(302, headers={"location": f"https://feed.example/hop-{n + 1}"})

    monkeypatch.setattr(tc.httpx, "stream", fake_stream)
    provider = RssTranscriptProvider(resolver=_safe_resolver)
    with pytest.raises(TranscriptProviderError):
        provider.get(EpisodeInput(feed_url="https://feed.example/hop-0", guid="ep-1"))


# --- R15: byte ceilings ------------------------------------------------------


def test_rss_feed_content_length_over_cap_is_provider_error(monkeypatch: pytest.MonkeyPatch) -> None:
    resp = _FakeStreamResponse(
        200, body=b"<rss></rss>", headers={"content-length": str(tc.MAX_FEED_BYTES + 1)}
    )
    _mock_stream(monkeypatch, {"https://feed.example/rss.xml": resp})
    provider = RssTranscriptProvider(resolver=_safe_resolver)
    with pytest.raises(TranscriptProviderError):
        provider.get(EpisodeInput(feed_url="https://feed.example/rss.xml", guid="ep-1"))


def test_rss_feed_body_over_cap_without_content_length_is_provider_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    big_body = b"<rss>" + b"x" * (tc.MAX_FEED_BYTES + 10) + b"</rss>"
    _mock_stream(
        monkeypatch, {"https://feed.example/rss.xml": _FakeStreamResponse(200, big_body)}
    )
    provider = RssTranscriptProvider(resolver=_safe_resolver)
    with pytest.raises(TranscriptProviderError):
        provider.get(EpisodeInput(feed_url="https://feed.example/rss.xml", guid="ep-1"))


def test_rss_transcript_segment_cap_is_enforced(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tc, "MAX_SEGMENTS", 1)
    feed = _feed_with(
        '<podcast:transcript url="https://cdn.example.com/ep1.json" type="application/json"/>'
    )
    _mock_stream(
        monkeypatch,
        {
            "https://feed.example/rss.xml": _stream_text(200, feed),
            "https://cdn.example.com/ep1.json": _stream_text(200, _JSON_TRANSCRIPT),
        },
    )
    provider = RssTranscriptProvider(resolver=_safe_resolver)
    with pytest.raises(TranscriptProviderError):
        provider.get(EpisodeInput(feed_url="https://feed.example/rss.xml", guid="ep-guid-1"))


def test_rss_transcript_char_cap_is_enforced(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tc, "MAX_TRANSCRIPT_CHARS", 5)
    feed = _feed_with(
        '<podcast:transcript url="https://cdn.example.com/ep1.json" type="application/json"/>'
    )
    _mock_stream(
        monkeypatch,
        {
            "https://feed.example/rss.xml": _stream_text(200, feed),
            "https://cdn.example.com/ep1.json": _stream_text(200, _JSON_TRANSCRIPT),
        },
    )
    provider = RssTranscriptProvider(resolver=_safe_resolver)
    with pytest.raises(TranscriptProviderError):
        provider.get(EpisodeInput(feed_url="https://feed.example/rss.xml", guid="ep-guid-1"))


def test_rss_transcript_duration_cap_is_enforced(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tc, "MAX_DURATION_SECONDS", 3)
    feed = _feed_with(
        '<podcast:transcript url="https://cdn.example.com/ep1.json" type="application/json"/>'
    )
    _mock_stream(
        monkeypatch,
        {
            "https://feed.example/rss.xml": _stream_text(200, feed),
            "https://cdn.example.com/ep1.json": _stream_text(200, _JSON_TRANSCRIPT),
        },
    )
    provider = RssTranscriptProvider(resolver=_safe_resolver)
    with pytest.raises(TranscriptProviderError):
        provider.get(EpisodeInput(feed_url="https://feed.example/rss.xml", guid="ep-guid-1"))


# --- R16: malformed payloads -------------------------------------------------


def test_rss_malformed_transcript_json_is_provider_error(monkeypatch: pytest.MonkeyPatch) -> None:
    feed = _feed_with(
        '<podcast:transcript url="https://cdn.example.com/ep1.json" type="application/json"/>'
    )
    _mock_stream(
        monkeypatch,
        {
            "https://feed.example/rss.xml": _stream_text(200, feed),
            "https://cdn.example.com/ep1.json": _stream_text(200, "{not valid json"),
        },
    )
    provider = RssTranscriptProvider(resolver=_safe_resolver)
    with pytest.raises(TranscriptProviderError):
        provider.get(EpisodeInput(feed_url="https://feed.example/rss.xml", guid="ep-guid-1"))


# --------------------------------------------------------------------------
# DeepgramTranscriptProvider — transport is httpx.stream now (POST).
# --------------------------------------------------------------------------


def _mock_deepgram_post(monkeypatch: pytest.MonkeyPatch, resp: _FakeStreamResponse | Exception) -> list[dict]:
    calls: list[dict] = []

    def fake_stream(method: str, url: str, **kwargs: object) -> _FakeStreamResponse:
        calls.append({"method": method, "url": url, **kwargs})
        assert method == "POST"
        assert url == tc.DEEPGRAM_LISTEN_URL
        if isinstance(resp, Exception):
            raise resp
        return resp

    monkeypatch.setattr(tc.httpx, "stream", fake_stream)
    return calls


def test_deepgram_happy_path_with_utterances(monkeypatch: pytest.MonkeyPatch) -> None:
    body = json.dumps(
        {
            "results": {
                "utterances": [
                    {"start": 0.5, "end": 3.0, "transcript": "hello world"},
                    {"start": 3.5, "end": 6.0, "transcript": "second line"},
                ]
            }
        }
    ).encode("utf-8")
    calls = _mock_deepgram_post(monkeypatch, _FakeStreamResponse(200, body))
    provider = DeepgramTranscriptProvider("dg-test", resolver=_safe_resolver)
    transcript = provider.get(
        EpisodeInput(audio_url="https://cdn.example.com/ep1.mp3")
    )
    assert transcript.source == "deepgram"
    assert transcript.segments[0].start == 0.5
    assert transcript.segments[0].text == "hello world"
    assert calls[0]["headers"]["Authorization"] == "Token dg-test"
    assert calls[0]["json"] == {"url": "https://cdn.example.com/ep1.mp3"}


def test_deepgram_falls_back_to_grouped_words(monkeypatch: pytest.MonkeyPatch) -> None:
    words = [
        {"word": "hello", "start": 0.0, "end": 0.3},
        {"word": "world", "start": 0.4, "end": 0.8},
        {"word": "later", "start": 12.0, "end": 12.4},
    ]
    body = json.dumps(
        {"results": {"utterances": [], "channels": [{"alternatives": [{"words": words}]}]}}
    ).encode("utf-8")
    _mock_deepgram_post(monkeypatch, _FakeStreamResponse(200, body))
    provider = DeepgramTranscriptProvider("dg-test", resolver=_safe_resolver)
    transcript = provider.get(
        EpisodeInput(audio_url="https://cdn.example.com/ep1.mp3")
    )
    assert len(transcript.segments) == 2  # window split at the >=10s gap
    assert transcript.segments[0].text == "hello world"
    assert transcript.segments[1].text == "later"


def test_deepgram_resolves_audio_url_from_rss_enclosure(monkeypatch: pytest.MonkeyPatch) -> None:
    feed = _feed_with("")
    body = json.dumps({"results": {"utterances": [{"start": 0.0, "transcript": "hi"}]}}).encode(
        "utf-8"
    )

    def fake_stream(method: str, url: str, **kwargs: object) -> _FakeStreamResponse:
        if method == "GET" and url == "https://feed.example/rss.xml":
            return _stream_text(200, feed)
        if method == "POST" and url == tc.DEEPGRAM_LISTEN_URL:
            assert kwargs["json"] == {"url": "https://cdn.example.com/ep1.mp3"}
            return _FakeStreamResponse(200, body)
        raise AssertionError(f"unexpected stream: {method} {url}")

    monkeypatch.setattr(tc.httpx, "stream", fake_stream)
    provider = DeepgramTranscriptProvider("dg-test", resolver=_safe_resolver)
    transcript = provider.get(
        EpisodeInput(feed_url="https://feed.example/rss.xml", guid="ep-guid-1")
    )
    assert transcript.source == "deepgram"


def test_deepgram_no_audio_url_is_not_found() -> None:
    provider = DeepgramTranscriptProvider("dg-test", resolver=_safe_resolver)
    with pytest.raises(TranscriptNotFound):
        provider.get(EpisodeInput(video_id="abcdefghijk"))


@pytest.mark.parametrize("status", [401, 500])
def test_deepgram_auth_and_server_errors_are_provider_errors(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    _mock_deepgram_post(monkeypatch, _FakeStreamResponse(status))
    provider = DeepgramTranscriptProvider("dg-test", resolver=_safe_resolver)
    with pytest.raises(TranscriptProviderError):
        provider.get(EpisodeInput(audio_url="https://cdn.example.com/ep1.mp3"))


def test_deepgram_timeout_is_provider_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_deepgram_post(monkeypatch, httpx.TimeoutException("timed out"))
    provider = DeepgramTranscriptProvider("dg-test", resolver=_safe_resolver)
    with pytest.raises(TranscriptProviderError):
        provider.get(EpisodeInput(audio_url="https://cdn.example.com/ep1.mp3"))


def test_deepgram_refuses_unsafe_audio_url(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_stream(*a: object, **kw: object) -> None:
        raise AssertionError("must not reach the network for an unsafe audio_url")

    monkeypatch.setattr(tc.httpx, "stream", fail_stream)
    provider = DeepgramTranscriptProvider("dg-test", resolver=_safe_resolver)
    with pytest.raises(TranscriptProviderError):
        provider.get(EpisodeInput(audio_url="https://169.254.169.254/latest"))


def test_deepgram_malformed_response_is_provider_error(monkeypatch: pytest.MonkeyPatch) -> None:
    body = json.dumps({"results": {"utterances": [{"start": "nope"}]}}).encode("utf-8")
    _mock_deepgram_post(monkeypatch, _FakeStreamResponse(200, body))
    provider = DeepgramTranscriptProvider("dg-test", resolver=_safe_resolver)
    with pytest.raises(TranscriptProviderError):
        provider.get(EpisodeInput(audio_url="https://cdn.example.com/ep1.mp3"))


def test_deepgram_oversized_response_is_provider_error(monkeypatch: pytest.MonkeyPatch) -> None:
    resp = _FakeStreamResponse(
        200,
        b"x" * 10,
        headers={"content-length": str(tc.MAX_STT_RESPONSE_BYTES + 1)},
    )
    _mock_deepgram_post(monkeypatch, resp)
    provider = DeepgramTranscriptProvider("dg-test", resolver=_safe_resolver)
    with pytest.raises(TranscriptProviderError):
        provider.get(EpisodeInput(audio_url="https://cdn.example.com/ep1.mp3"))


# --------------------------------------------------------------------------
# ChainTranscriptProvider — R11 identity check, R16 taxonomy
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


class _ReturnsMismatchedId:
    def get(self, episode: EpisodeInput) -> object:
        from chorus.models import Transcript

        return Transcript(video_id="not-what-was-asked-for", segments=[], source="stub")


def test_chain_falls_through_notfound_and_providererror_in_order() -> None:
    chain = ChainTranscriptProvider([_RaisingNotFound(), _RaisingProviderError(), _Succeeds()])
    transcript = chain.get(EpisodeInput(video_id="x"))
    assert transcript.source == "stub"  # type: ignore[union-attr]


def test_chain_all_fail_with_a_provider_error_raises_provider_error() -> None:
    # R16: at least one ProviderError among the failures means the whole
    # thing is retryable, not a terminal "nothing exists" verdict.
    chain = ChainTranscriptProvider([_RaisingNotFound(), _RaisingProviderError()])
    with pytest.raises(TranscriptProviderError) as excinfo:
        chain.get(EpisodeInput(video_id="x"))
    message = str(excinfo.value)
    assert "_RaisingNotFound" in message
    assert "_RaisingProviderError" in message
    assert "nope, not here" in message
    assert "outage" in message


def test_chain_all_notfound_raises_notfound() -> None:
    chain = ChainTranscriptProvider([_RaisingNotFound(), _RaisingNotFound()])
    with pytest.raises(TranscriptNotFound):
        chain.get(EpisodeInput(video_id="x"))


def test_chain_empty_providers_raises() -> None:
    chain = ChainTranscriptProvider([])
    with pytest.raises(TranscriptNotFound):
        chain.get(EpisodeInput(video_id="x"))


def test_chain_refuses_mismatched_video_id_and_tries_next_provider() -> None:
    chain = ChainTranscriptProvider([_ReturnsMismatchedId(), _Succeeds()])
    transcript = chain.get(EpisodeInput(video_id="x"))
    assert transcript.source == "stub"  # type: ignore[union-attr]


def test_chain_all_mismatched_raises_provider_error() -> None:
    chain = ChainTranscriptProvider([_ReturnsMismatchedId()])
    with pytest.raises(TranscriptProviderError) as excinfo:
        chain.get(EpisodeInput(video_id="x"))
    assert "not-what-was-asked-for" in str(excinfo.value)


def test_transcript_not_found_and_provider_error_are_categorized() -> None:
    # R7/R16: retryable vs terminal, load-bearing for the Inngest runner
    # (owned elsewhere) that decides whether to retry.
    assert issubclass(TranscriptProviderError, RetryableError)
    assert issubclass(TranscriptNotFound, TerminalError)


# --------------------------------------------------------------------------
# Transcript cache — R11 namespacing + mismatch guard
# --------------------------------------------------------------------------


class _CountingProvider:
    def __init__(self) -> None:
        self.calls = 0

    def get(self, episode: EpisodeInput) -> object:
        from chorus.models import Transcript

        self.calls += 1
        return Transcript(video_id=episode.resolved_id(), segments=[], source="counted")


class _ReturnsWrongIdProvider:
    def get(self, episode: EpisodeInput) -> object:
        from chorus.models import Transcript

        return Transcript(video_id="someone-elses-id", segments=[], source="counted")


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


def test_cache_namespaces_yt_and_rss_ids_separately(tmp_path: Path) -> None:
    cache = SqliteTranscriptCache(tmp_path / "cache.db")
    from chorus.models import Transcript

    cache.put(Transcript(video_id="abc", segments=[], source="yt"))
    cache.put(Transcript(video_id="rss-abc", segments=[], source="rss"))
    assert cache.get("abc").source == "yt"  # type: ignore[union-attr]
    assert cache.get("rss-abc").source == "rss"  # type: ignore[union-attr]


def test_cache_refuses_to_store_mismatched_transcript(tmp_path: Path) -> None:
    cache = SqliteTranscriptCache(tmp_path / "cache.db")
    provider = CachingTranscriptProvider(_ReturnsWrongIdProvider(), cache)
    with pytest.raises(TranscriptProviderError):
        provider.get(EpisodeInput(video_id="abc"))
    assert cache.get("abc") is None


# --------------------------------------------------------------------------
# ingest() records provenance — R16 transient-skip taxonomy
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
    assert result.skipped[0].reason.startswith(TRANSIENT_REASON_PREFIX)
    assert "outage" in result.skipped[0].reason


class _AlwaysProviderError:
    def get(self, episode: EpisodeInput) -> object:
        raise TranscriptProviderError("outage")


class _AlwaysNotFound:
    def get(self, episode: EpisodeInput) -> object:
        raise TranscriptNotFound("gone")


def test_ingest_all_transient_raises_provider_error_not_all_episodes_failed() -> None:
    with pytest.raises(TranscriptProviderError):
        ingest([EpisodeInput(video_id="a"), EpisodeInput(video_id="b")], _AlwaysProviderError())


def test_ingest_mixed_notfound_and_transient_raises_all_episodes_failed() -> None:
    from chorus.ingest import AllEpisodesFailed

    class _Mixed:
        def get(self, episode: EpisodeInput) -> object:
            if episode.video_id == "a":
                raise TranscriptNotFound("gone")
            raise TranscriptProviderError("outage")

    with pytest.raises(AllEpisodesFailed):
        ingest([EpisodeInput(video_id="a"), EpisodeInput(video_id="b")], _Mixed())


def test_ingest_all_notfound_raises_all_episodes_failed() -> None:
    from chorus.ingest import AllEpisodesFailed

    with pytest.raises(AllEpisodesFailed):
        ingest([EpisodeInput(video_id="a")], _AlwaysNotFound())


# --------------------------------------------------------------------------
# Full API lifecycle over a NON-fixture episode via a mocked managed-captions
# provider — the Phase C exit criterion.
# --------------------------------------------------------------------------

NON_FIXTURE_VIDEO_ID = "zzzzzzzzzzz"


def test_digest_over_non_fixture_episode_reaches_done(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_get(url: str, **kwargs: object) -> _FakeResponse:
        assert kwargs["params"] == {
            "url": f"https://www.youtube.com/watch?v={NON_FIXTURE_VIDEO_ID}",
            "mode": "native",
            "text": "false",
        }
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
        artifacts=LocalArtifactStore(tmp_path / "artifacts"),
    )
    client = TestClient(create_app(SqliteJobStore(tmp_path / "jobs.db"), deps))

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
    for key in (
        "TRANSCRIPT_API_KEY",
        "DEEPGRAM_API_KEY",
        "ASSEMBLYAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "ELEVENLABS_API_KEY",
    ):
        monkeypatch.delenv(key, raising=False)
    providers = _unwrap_chain(default_deps())
    kinds = [type(p).__name__ for p in providers]
    assert kinds == ["FixtureTranscriptProvider", "RssTranscriptProvider"]


def test_default_deps_adds_every_keyed_provider_in_ladder_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TRANSCRIPT_API_KEY", "sk-test")
    monkeypatch.setenv("DEEPGRAM_API_KEY", "dg-test")
    monkeypatch.setenv("ASSEMBLYAI_API_KEY", "aai-test")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    providers = _unwrap_chain(default_deps())
    kinds = [type(p).__name__ for p in providers]
    assert kinds == [
        "FixtureTranscriptProvider",
        "RssTranscriptProvider",
        "AssemblyAITranscriptProvider",
        "DeepgramTranscriptProvider",
        "ManagedCaptionsProvider",
    ]
