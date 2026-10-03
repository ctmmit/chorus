"""The transcript ladder rework (reports/Podcast transcript sources.md,
02 Oct 2026): AssemblyAI provider, the new chain order, Supadata native mode,
RSS parser fixes, speaker/source_audio_url provenance, and late-published
transcripts. Every HTTP call is mocked (httpx.stream for RSS/Deepgram/
AssemblyAI, httpx.get for Supadata) and every provider gets an injected
resolver and clock, so nothing touches the network, real DNS, or real time.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Self

import httpx
import pytest

from chorus import config_env
from chorus import transcripts as tc
from chorus.curation import window_segments
from chorus.models import EpisodeInput, Segment, Transcript
from chorus.pipeline import default_deps
from chorus.transcript_cache import CachingTranscriptProvider, SqliteTranscriptCache
from chorus.transcripts import (
    AssemblyAITranscriptProvider,
    ChainTranscriptProvider,
    DeepgramTranscriptProvider,
    ManagedCaptionsProvider,
    RssTranscriptProvider,
    TranscriptNotFound,
    TranscriptProviderError,
)

_SAFE_IP = "93.184.216.34"
_AUDIO_URL = "https://cdn.example.com/ep1.mp3"
_FEED_URL = "https://feed.example/rss.xml"


def _safe_resolver(host: str) -> list[str]:
    return [_SAFE_IP]


class _FakeStreamResponse:
    """Minimal `httpx.stream()` context-manager result for `_fetch_bounded`."""

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

    def iter_bytes(self) -> Any:
        yield self._body


def _text(status: int, text: str) -> _FakeStreamResponse:
    return _FakeStreamResponse(status, text.encode("utf-8"))


def _json(status: int, payload: object) -> _FakeStreamResponse:
    return _FakeStreamResponse(status, json.dumps(payload).encode("utf-8"))


Call = tuple[str, str, dict[str, Any]]


def _install_stream(monkeypatch: pytest.MonkeyPatch, handler: Any) -> list[Call]:
    """Route every httpx.stream call through `handler(method, url, kwargs)`,
    recording it. A handler that does not recognise a call should raise."""
    calls: list[Call] = []

    def fake_stream(method: str, url: str, **kwargs: Any) -> _FakeStreamResponse:
        calls.append((method, url, kwargs))
        result = handler(method, url, kwargs)
        if isinstance(result, Exception):
            raise result
        assert isinstance(result, _FakeStreamResponse)
        return result

    monkeypatch.setattr(tc.httpx, "stream", fake_stream)
    return calls


class _Clock:
    """Fake monotonic clock whose sleep advances it, so poll loops run
    instantly and deterministically."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


# --------------------------------------------------------------------------
# AssemblyAI provider
# --------------------------------------------------------------------------

_SUBMIT_URL = f"{tc.ASSEMBLYAI_BASE_URL}/transcript"
_POLL_URL = f"{tc.ASSEMBLYAI_BASE_URL}/transcript/tr-1"

_UTTERANCES = [
    {"speaker": "A", "text": "Welcome to the show.", "start": 250, "end": 2400},
    {"speaker": "B", "text": "Glad to be here.", "start": 2600, "end": 4100},
]


class _AssemblyAIServer:
    """Scripted AssemblyAI: one submit response, then a queue of poll
    responses (the last one repeats forever, to model a stuck job)."""

    def __init__(
        self,
        polls: list[_FakeStreamResponse],
        submit: _FakeStreamResponse | None = None,
    ) -> None:
        self.submit = submit or _json(200, {"id": "tr-1", "status": "queued"})
        self.polls = polls

    def __call__(self, method: str, url: str, kwargs: dict[str, Any]) -> _FakeStreamResponse:
        if method == "POST" and url == _SUBMIT_URL:
            return self.submit
        if method == "GET" and url == _POLL_URL:
            return self.polls.pop(0) if len(self.polls) > 1 else self.polls[0]
        raise AssertionError(f"unexpected request: {method} {url}")


def _assemblyai(clock: _Clock | None = None, **kwargs: Any) -> AssemblyAITranscriptProvider:
    clock = clock or _Clock()
    return AssemblyAITranscriptProvider(
        "aai-test",
        resolver=_safe_resolver,
        sleep=clock.sleep,
        clock=clock,
        **kwargs,
    )


def _episode() -> EpisodeInput:
    return EpisodeInput(audio_url=_AUDIO_URL)


def test_assemblyai_submit_and_poll_success(monkeypatch: pytest.MonkeyPatch) -> None:
    server = _AssemblyAIServer(
        [
            _json(200, {"id": "tr-1", "status": "queued"}),
            _json(200, {"id": "tr-1", "status": "processing"}),
            _json(200, {"id": "tr-1", "status": "completed", "utterances": _UTTERANCES}),
        ]
    )
    calls = _install_stream(monkeypatch, server)
    clock = _Clock()
    transcript = _assemblyai(clock).get(_episode())

    assert transcript.source == "assemblyai"
    assert transcript.source_audio_url == _AUDIO_URL
    assert [(s.start, s.text, s.speaker) for s in transcript.segments] == [
        (0.25, "Welcome to the show.", "Speaker A"),
        (2.6, "Glad to be here.", "Speaker B"),
    ]
    # Submit carries the key header and the documented request fields.
    method, url, kwargs = calls[0]
    assert (method, url) == ("POST", _SUBMIT_URL)
    assert kwargs["headers"]["authorization"] == "aai-test"
    assert kwargs["json"] == {
        "audio_url": _AUDIO_URL,
        "speaker_labels": True,
        "speech_models": ["universal-3-5-pro", "universal-2"],
    }
    assert "speech_model" not in kwargs["json"]  # the deprecated singular parameter
    # Three polls, two waits at the documented interval.
    assert [c[0] for c in calls] == ["POST", "GET", "GET", "GET"]
    assert all(c[2]["headers"]["authorization"] == "aai-test" for c in calls)
    assert clock.sleeps == [tc.ASSEMBLYAI_POLL_INTERVAL_S] * 2


def test_assemblyai_documented_constants() -> None:
    assert tc.ASSEMBLYAI_POLL_INTERVAL_S == 3.0
    assert tc.ASSEMBLYAI_MAX_WAIT_S == 240.0
    assert tc.ASSEMBLYAI_BASE_URL == "https://api.assemblyai.com/v2"


def test_assemblyai_error_status_is_provider_error(monkeypatch: pytest.MonkeyPatch) -> None:
    server = _AssemblyAIServer(
        [
            _json(200, {"id": "tr-1", "status": "processing"}),
            _json(200, {"id": "tr-1", "status": "error", "error": "download failed: 404"}),
        ]
    )
    _install_stream(monkeypatch, server)
    with pytest.raises(TranscriptProviderError, match="download failed: 404"):
        _assemblyai().get(_episode())


def test_assemblyai_error_status_on_submit_is_provider_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = _AssemblyAIServer(
        [], submit=_json(200, {"id": "tr-1", "status": "error", "error": "bad audio"})
    )
    _install_stream(monkeypatch, server)
    with pytest.raises(TranscriptProviderError, match="bad audio"):
        _assemblyai().get(_episode())


def test_assemblyai_poll_timeout_is_provider_error_so_inngest_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = _AssemblyAIServer([_json(200, {"id": "tr-1", "status": "processing"})])
    calls = _install_stream(monkeypatch, server)
    clock = _Clock()
    provider = _assemblyai(clock, max_wait_s=10.0, poll_interval_s=3.0)
    with pytest.raises(TranscriptProviderError, match="still processing after 10s"):
        provider.get(_episode())
    # Polled at t=0,3,6,9,12; gave up at the first poll past the 10 s budget.
    assert clock.sleeps == [3.0, 3.0, 3.0, 3.0]
    assert [c[0] for c in calls].count("GET") == 5


def test_assemblyai_unknown_status_is_provider_error(monkeypatch: pytest.MonkeyPatch) -> None:
    server = _AssemblyAIServer([_json(200, {"id": "tr-1", "status": "mystery"})])
    _install_stream(monkeypatch, server)
    with pytest.raises(TranscriptProviderError, match="unexpected status"):
        _assemblyai().get(_episode())


@pytest.mark.parametrize(
    "submit",
    [
        _json(200, {"status": "queued"}),  # no id
        _json(200, {"id": 7, "status": "queued"}),  # id not a string
        _FakeStreamResponse(200, b"<html>not json</html>"),
    ],
)
def test_assemblyai_malformed_submit_is_provider_error(
    monkeypatch: pytest.MonkeyPatch, submit: _FakeStreamResponse
) -> None:
    _install_stream(monkeypatch, _AssemblyAIServer([], submit=submit))
    with pytest.raises(TranscriptProviderError, match="malformed submit"):
        _assemblyai().get(_episode())


@pytest.mark.parametrize(
    "poll",
    [
        _json(200, {"status": "completed", "utterances": [{"text": "hi", "start": "soon"}]}),
        _json(200, {"status": "completed", "utterances": "nope"}),
        _json(200, {"id": "tr-1"}),  # no status
        _FakeStreamResponse(200, b"{not json"),
    ],
)
def test_assemblyai_malformed_transcript_is_provider_error(
    monkeypatch: pytest.MonkeyPatch, poll: _FakeStreamResponse
) -> None:
    _install_stream(monkeypatch, _AssemblyAIServer([poll]))
    with pytest.raises(TranscriptProviderError, match="malformed transcript"):
        _assemblyai().get(_episode())


@pytest.mark.parametrize("status", [400, 401, 403, 429, 500, 503])
def test_assemblyai_http_errors_are_provider_errors(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    _install_stream(monkeypatch, _AssemblyAIServer([], submit=_FakeStreamResponse(status)))
    with pytest.raises(TranscriptProviderError):
        _assemblyai().get(_episode())


def test_assemblyai_transport_timeout_is_provider_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_stream(monkeypatch, lambda *a: httpx.TimeoutException("timed out"))
    with pytest.raises(TranscriptProviderError):
        _assemblyai().get(_episode())


def test_assemblyai_oversized_response_is_provider_error(monkeypatch: pytest.MonkeyPatch) -> None:
    big = _FakeStreamResponse(
        200, b"x" * 10, headers={"content-length": str(tc.MAX_STT_RESPONSE_BYTES + 1)}
    )
    _install_stream(monkeypatch, _AssemblyAIServer([big]))
    with pytest.raises(TranscriptProviderError):
        _assemblyai().get(_episode())


def test_assemblyai_words_fallback_groups_by_speaker_and_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    words = [
        {"text": "hello", "start": 0, "end": 300, "speaker": "A"},
        {"text": "world", "start": 400, "end": 800, "speaker": "A"},
        {"text": "hi", "start": 1000, "end": 1200, "speaker": "B"},  # speaker change
        {"text": "there", "start": 1300, "end": 1500, "speaker": "B"},
        {"text": "much", "start": 12_000, "end": 12_300, "speaker": "B"},  # >=10 s window
    ]
    server = _AssemblyAIServer(
        [_json(200, {"id": "tr-1", "status": "completed", "utterances": None, "words": words})]
    )
    _install_stream(monkeypatch, server)
    transcript = _assemblyai().get(_episode())
    assert [(s.start, s.text, s.speaker) for s in transcript.segments] == [
        (0.0, "hello world", "Speaker A"),
        (1.0, "hi there", "Speaker B"),
        (12.0, "much", "Speaker B"),
    ]


def test_assemblyai_words_fallback_without_speakers(monkeypatch: pytest.MonkeyPatch) -> None:
    words = [{"text": "hello", "start": 0, "end": 300}, {"text": "world", "start": 400, "end": 800}]
    server = _AssemblyAIServer(
        [_json(200, {"id": "tr-1", "status": "completed", "utterances": [], "words": words})]
    )
    _install_stream(monkeypatch, server)
    transcript = _assemblyai().get(_episode())
    assert [(s.text, s.speaker) for s in transcript.segments] == [("hello world", None)]


def test_assemblyai_empty_completed_transcript_is_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = _AssemblyAIServer(
        [_json(200, {"id": "tr-1", "status": "completed", "utterances": [], "words": []})]
    )
    _install_stream(monkeypatch, server)
    with pytest.raises(TranscriptNotFound):
        _assemblyai().get(_episode())


def test_assemblyai_resolves_audio_url_from_rss_enclosure(monkeypatch: pytest.MonkeyPatch) -> None:
    feed = _feed_with("")
    server = _AssemblyAIServer(
        [_json(200, {"id": "tr-1", "status": "completed", "utterances": _UTTERANCES})]
    )

    def handler(method: str, url: str, kwargs: dict[str, Any]) -> _FakeStreamResponse:
        if method == "GET" and url == _FEED_URL:
            return _text(200, feed)
        return server(method, url, kwargs)

    calls = _install_stream(monkeypatch, handler)
    transcript = _assemblyai().get(EpisodeInput(feed_url=_FEED_URL, guid="ep-guid-1"))
    assert transcript.source_audio_url == "https://cdn.example.com/ep1.mp3"
    post = next(c for c in calls if c[0] == "POST")
    assert post[2]["json"]["audio_url"] == "https://cdn.example.com/ep1.mp3"


def test_assemblyai_refuses_unsafe_audio_url_without_touching_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(*a: object, **kw: object) -> None:
        raise AssertionError("must not reach the network for an unsafe audio_url")

    monkeypatch.setattr(tc.httpx, "stream", fail)
    with pytest.raises(TranscriptProviderError, match="unsafe"):
        _assemblyai().get(EpisodeInput(audio_url="https://169.254.169.254/latest"))


def test_assemblyai_youtube_episode_has_no_audio_so_not_found() -> None:
    with pytest.raises(TranscriptNotFound):
        _assemblyai().get(EpisodeInput(video_id="abcdefghijk"))


def test_assemblyai_enforces_transcript_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tc, "MAX_SEGMENTS", 1)
    server = _AssemblyAIServer(
        [_json(200, {"id": "tr-1", "status": "completed", "utterances": _UTTERANCES})]
    )
    _install_stream(monkeypatch, server)
    with pytest.raises(TranscriptProviderError, match="MAX_SEGMENTS"):
        _assemblyai().get(_episode())


# --------------------------------------------------------------------------
# Deepgram: speaker capture and provenance
# --------------------------------------------------------------------------


def _deepgram_server(payload: object) -> Any:
    def handler(method: str, url: str, kwargs: dict[str, Any]) -> _FakeStreamResponse:
        assert (method, url) == ("POST", tc.DEEPGRAM_LISTEN_URL)
        return _json(200, payload)

    return handler


def test_deepgram_requests_diarization_with_the_current_parameter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {"results": {"utterances": [{"start": 0.5, "transcript": "hi", "speaker": 1}]}}
    calls = _install_stream(monkeypatch, _deepgram_server(payload))
    DeepgramTranscriptProvider("dg", resolver=_safe_resolver).get(_episode())
    params = calls[0][2]["params"]
    assert params["diarize_model"] == "latest"
    assert "diarize" not in params  # setting both is rejected by Deepgram


def test_deepgram_utterances_carry_speaker_and_source_audio_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {
        "results": {
            "utterances": [
                {"start": 0.5, "transcript": "hello", "speaker": 0},
                {"start": 3.0, "transcript": "hi back", "speaker": 1},
                {"start": 6.0, "transcript": "no speaker"},
            ]
        }
    }
    _install_stream(monkeypatch, _deepgram_server(payload))
    transcript = DeepgramTranscriptProvider("dg", resolver=_safe_resolver).get(_episode())
    assert [s.speaker for s in transcript.segments] == ["Speaker 0", "Speaker 1", None]
    assert transcript.source == "deepgram"
    assert transcript.source_audio_url == _AUDIO_URL


def test_deepgram_words_fallback_splits_on_speaker_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    words = [
        {"word": "hello", "start": 0.0, "speaker": 0},
        {"word": "world", "start": 0.4, "speaker": 0},
        {"word": "hey", "start": 1.0, "speaker": 1},
    ]
    payload = {"results": {"utterances": [], "channels": [{"alternatives": [{"words": words}]}]}}
    _install_stream(monkeypatch, _deepgram_server(payload))
    transcript = DeepgramTranscriptProvider("dg", resolver=_safe_resolver).get(_episode())
    assert [(s.text, s.speaker) for s in transcript.segments] == [
        ("hello world", "Speaker 0"),
        ("hey", "Speaker 1"),
    ]


# --------------------------------------------------------------------------
# Chain order
# --------------------------------------------------------------------------

_KEY_ENVS = ("ASSEMBLYAI_API_KEY", "DEEPGRAM_API_KEY", "TRANSCRIPT_API_KEY")


def _kinds(provider: object) -> list[str]:
    assert isinstance(provider, CachingTranscriptProvider)
    assert isinstance(provider.inner, ChainTranscriptProvider)
    return [type(p).__name__ for p in provider.inner.providers]


def test_chain_order_with_all_keys_is_the_research_ladder(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("ASSEMBLYAI_API_KEY", "aai")
    monkeypatch.setenv("DEEPGRAM_API_KEY", "dg")
    monkeypatch.setenv("TRANSCRIPT_API_KEY", "sup")
    chain = config_env.build_transcript_chain(SqliteTranscriptCache(tmp_path / "c.db"))
    assert _kinds(chain) == [
        "FixtureTranscriptProvider",
        "RssTranscriptProvider",
        "AssemblyAITranscriptProvider",
        "DeepgramTranscriptProvider",
        "ManagedCaptionsProvider",
    ]


@pytest.mark.parametrize(
    ("present", "expected"),
    [
        ((), ["FixtureTranscriptProvider", "RssTranscriptProvider"]),
        (
            ("ASSEMBLYAI_API_KEY",),
            ["FixtureTranscriptProvider", "RssTranscriptProvider", "AssemblyAITranscriptProvider"],
        ),
        (
            ("TRANSCRIPT_API_KEY", "DEEPGRAM_API_KEY"),
            [
                "FixtureTranscriptProvider",
                "RssTranscriptProvider",
                "DeepgramTranscriptProvider",
                "ManagedCaptionsProvider",
            ],
        ),
    ],
)
def test_chain_skips_unkeyed_providers_and_keeps_order(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    present: tuple[str, ...],
    expected: list[str],
) -> None:
    for env in _KEY_ENVS:
        monkeypatch.delenv(env, raising=False)
    for env in present:
        monkeypatch.setenv(env, "k")
    chain = config_env.build_transcript_chain(SqliteTranscriptCache(tmp_path / "c.db"))
    assert _kinds(chain) == expected


def test_stt_providers_share_the_chain_rss_provider(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("ASSEMBLYAI_API_KEY", "aai")
    monkeypatch.setenv("DEEPGRAM_API_KEY", "dg")
    chain = config_env.build_transcript_chain(SqliteTranscriptCache(tmp_path / "c.db"))
    assert isinstance(chain, CachingTranscriptProvider)
    assert isinstance(chain.inner, ChainTranscriptProvider)
    _, rss, assemblyai, deepgram = chain.inner.providers
    assert isinstance(assemblyai, AssemblyAITranscriptProvider)
    assert isinstance(deepgram, DeepgramTranscriptProvider)
    assert assemblyai.rss_provider is rss
    assert deepgram.rss_provider is rss


def test_default_deps_and_build_deps_share_one_chain_builder(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """They cannot drift: both go through build_transcript_chain."""
    for env in _KEY_ENVS:
        monkeypatch.setenv(env, "k")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(
        "chorus.pipeline.SqliteTranscriptCache",
        lambda: SqliteTranscriptCache(tmp_path / "a.db"),
    )
    monkeypatch.setattr(
        config_env,
        "select_transcript_cache",
        lambda: SqliteTranscriptCache(tmp_path / "b.db"),
    )
    assert _kinds(default_deps().provider) == _kinds(config_env.build_deps().provider)


def test_active_chain_is_logged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    for env in _KEY_ENVS:
        monkeypatch.setenv(env, "k")
    with caplog.at_level(logging.INFO, logger="chorus.config_env"):
        config_env.build_transcript_chain(SqliteTranscriptCache(tmp_path / "c.db"))
    assert "fixture -> rss -> assemblyai -> deepgram -> supadata" in caplog.text


# --------------------------------------------------------------------------
# Supadata native mode
# --------------------------------------------------------------------------


class _SupadataResponse:
    def __init__(self, status_code: int, payload: object = None) -> None:
        self.status_code = status_code
        self._payload = payload
        self.content = json.dumps(payload).encode("utf-8")

    def json(self) -> object:
        return self._payload


def _supadata(clock: _Clock | None = None, **kwargs: Any) -> ManagedCaptionsProvider:
    clock = clock or _Clock()
    return ManagedCaptionsProvider("sk-test", sleep=clock.sleep, clock=clock, **kwargs)


_YT = EpisodeInput(video_id="abcdefghijk")
_CAPTIONS = {"content": [{"text": "hello", "offset": 1500, "duration": 2000}], "lang": "en"}


def test_supadata_request_uses_native_mode_so_it_never_bills_ai(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[dict[str, Any]] = []

    def fake_get(url: str, **kwargs: Any) -> _SupadataResponse:
        seen.append({"url": url, **kwargs})
        return _SupadataResponse(200, _CAPTIONS)

    monkeypatch.setattr(tc.httpx, "get", fake_get)
    transcript = _supadata().get(_YT)
    assert seen[0]["url"] == "https://api.supadata.ai/v1/transcript"
    assert seen[0]["params"]["mode"] == "native"
    assert seen[0]["params"]["url"] == "https://www.youtube.com/watch?v=abcdefghijk"
    assert seen[0]["headers"] == {"x-api-key": "sk-test"}
    assert transcript.segments[0].start == 1.5
    assert transcript.source == "supadata"


@pytest.mark.parametrize("status", [206, 404])
def test_supadata_missing_captions_is_not_found(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    body = {"error": "transcript-unavailable", "message": "no captions"}
    monkeypatch.setattr(tc.httpx, "get", lambda *a, **kw: _SupadataResponse(status, body))
    with pytest.raises(TranscriptNotFound, match="no native captions"):
        _supadata().get(_YT)


def test_supadata_long_video_polls_the_job_until_completed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    results = [
        _SupadataResponse(202, {"jobId": "job-1"}),
        _SupadataResponse(200, {"status": "queued"}),
        _SupadataResponse(200, {"status": "active"}),
        _SupadataResponse(200, {"status": "completed", **_CAPTIONS}),
    ]
    urls: list[str] = []

    def fake_get(url: str, **kwargs: Any) -> _SupadataResponse:
        urls.append(url)
        return results.pop(0)

    monkeypatch.setattr(tc.httpx, "get", fake_get)
    clock = _Clock()
    transcript = _supadata(clock).get(_YT)
    assert urls == [
        "https://api.supadata.ai/v1/transcript",
        "https://api.supadata.ai/v1/transcript/job-1",
        "https://api.supadata.ai/v1/transcript/job-1",
        "https://api.supadata.ai/v1/transcript/job-1",
    ]
    assert transcript.segments[0].text == "hello"
    assert clock.sleeps == [tc.SUPADATA_POLL_INTERVAL_S] * 2


def test_supadata_job_failed_unavailable_is_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    results = [
        _SupadataResponse(202, {"jobId": "job-1"}),
        _SupadataResponse(
            200, {"status": "failed", "error": {"error": "transcript-unavailable", "message": "x"}}
        ),
    ]
    monkeypatch.setattr(tc.httpx, "get", lambda *a, **kw: results.pop(0))
    with pytest.raises(TranscriptNotFound):
        _supadata().get(_YT)


def test_supadata_job_failed_other_is_provider_error(monkeypatch: pytest.MonkeyPatch) -> None:
    results = [
        _SupadataResponse(202, {"jobId": "job-1"}),
        _SupadataResponse(200, {"status": "failed", "error": {"error": "internal-error"}}),
    ]
    monkeypatch.setattr(tc.httpx, "get", lambda *a, **kw: results.pop(0))
    with pytest.raises(TranscriptProviderError):
        _supadata().get(_YT)


def test_supadata_job_poll_timeout_is_provider_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_get(url: str, **kwargs: Any) -> _SupadataResponse:
        if url.endswith("/transcript"):
            return _SupadataResponse(202, {"jobId": "job-1"})
        return _SupadataResponse(200, {"status": "active"})

    monkeypatch.setattr(tc.httpx, "get", fake_get)
    with pytest.raises(TranscriptProviderError, match="still active"):
        _supadata(max_wait_s=5.0, poll_interval_s=2.0).get(_YT)


def test_supadata_malformed_202_is_provider_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tc.httpx, "get", lambda *a, **kw: _SupadataResponse(202, {"nope": 1}))
    with pytest.raises(TranscriptProviderError):
        _supadata().get(_YT)


# --------------------------------------------------------------------------
# RSS parser fixes
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
  </channel>
</rss>
"""


def _feed_with(tags: str) -> str:
    return _FEED_TEMPLATE.format(transcripts=tags)


def _tag(url: str, mime: str) -> str:
    return f'<podcast:transcript url="{url}" type="{mime}"/>'


_RSS_EPISODE = EpisodeInput(feed_url=_FEED_URL, guid="ep-guid-1")


def _serve(monkeypatch: pytest.MonkeyPatch, files: dict[str, str]) -> list[Call]:
    """GET-only static server: any URL not in `files` fails the test, which is
    how 'this transcript must never be fetched' is asserted."""

    def handler(method: str, url: str, kwargs: dict[str, Any]) -> _FakeStreamResponse:
        if method != "GET" or url not in files:
            raise AssertionError(f"unexpected fetch: {method} {url}")
        return _text(200, files[url])

    return _install_stream(monkeypatch, handler)


def _rss() -> RssTranscriptProvider:
    return RssTranscriptProvider(resolver=_safe_resolver)


@pytest.mark.parametrize(
    ("cue", "start"),
    [
        ("0:00:00.450", 0.45),  # Omny: single-digit hour
        ("1:02:03.500", 3723.5),
        ("00:05.120", 5.12),  # no hour at all
        ("0:05.120", 5.12),
        ("12:34:56.789", 45296.789),
        ("00:00:01.000", 1.0),  # the original shape still parses
        ("00:00:01.5", 1.5),  # short fraction
    ],
)
def test_vtt_cue_timestamps_with_short_or_missing_hour(cue: str, start: float) -> None:
    segments = tc._parse_cues(f"WEBVTT\n\n{cue} --> 23:59:59.999\nhello there\n")
    assert [(s.start, s.text) for s in segments] == [(start, "hello there")]


@pytest.mark.parametrize(
    ("cue", "start"),
    [("0:00:00,450", 0.45), ("00:05,120", 5.12), ("00:00:07,000", 7.0)],
)
def test_srt_cue_timestamps_with_short_or_missing_hour(cue: str, start: float) -> None:
    segments = tc._parse_cues(f"1\n{cue} --> 00:10:00,000\nhello there\n\n2\n00:11:00,000 --> 00:12:00,000\nnext\n")
    assert segments[0].start == start
    assert segments[0].text == "hello there"
    assert segments[1].start == 660.0


def test_a_clock_time_inside_cue_text_is_not_a_cue() -> None:
    text = "WEBVTT\n\n00:00:01.000 --> 00:00:04.000\nthe call is at 5:00:00.000 --> late\n"
    segments = tc._parse_cues(text)
    assert len(segments) == 1
    assert segments[0].text == "the call is at 5:00:00.000 --> late"


def test_rss_end_to_end_omny_style_vtt(monkeypatch: pytest.MonkeyPatch) -> None:
    vtt = (
        "WEBVTT\n\n"
        "0:00:00.450 --> 0:00:03.100\n<v Speaker 1>Welcome to Odd Lots.</v>\n\n"
        "0:00:03.200 --> 0:00:05.000\n<v Speaker 2>Thanks for having me.</v>\n"
    )
    feed = _feed_with(_tag("https://cdn.example.com/ep1.vtt", "text/vtt"))
    _serve(monkeypatch, {_FEED_URL: feed, "https://cdn.example.com/ep1.vtt": vtt})
    transcript = _rss().get(_RSS_EPISODE)
    assert transcript.source == "rss:vtt"
    assert [(s.start, s.text, s.speaker) for s in transcript.segments] == [
        (0.45, "Welcome to Odd Lots.", "Speaker 1"),
        (3.2, "Thanks for having me.", "Speaker 2"),
    ]


def test_rss_accepts_x_subrip_as_srt(monkeypatch: pytest.MonkeyPatch) -> None:
    srt = "1\n00:00:01,000 --> 00:00:04,000\nhello world\n"
    feed = _feed_with(_tag("https://cdn.example.com/ep1.srt", "application/x-subrip"))
    _serve(monkeypatch, {_FEED_URL: feed, "https://cdn.example.com/ep1.srt": srt})
    transcript = _rss().get(_RSS_EPISODE)
    assert transcript.source == "rss:srt"
    assert transcript.segments[0].text == "hello world"


def test_rss_mime_type_is_normalised(monkeypatch: pytest.MonkeyPatch) -> None:
    vtt = "WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nhi\n"
    feed = _feed_with(_tag("https://cdn.example.com/ep1.vtt", "Text/VTT; charset=utf-8"))
    _serve(monkeypatch, {_FEED_URL: feed, "https://cdn.example.com/ep1.vtt": vtt})
    assert _rss().get(_RSS_EPISODE).source == "rss:vtt"


@pytest.mark.parametrize("mime", ["text/plain", "text/html"])
def test_rss_untimed_transcripts_are_skipped_with_a_reason(
    monkeypatch: pytest.MonkeyPatch, mime: str
) -> None:
    feed = _feed_with(_tag("https://cdn.example.com/ep1.txt", mime))
    # The .txt URL is not served: fetching an untimed transcript fails the test.
    _serve(monkeypatch, {_FEED_URL: feed})
    with pytest.raises(TranscriptNotFound) as excinfo:
        _rss().get(_RSS_EPISODE)
    message = str(excinfo.value)
    assert f"skipped untimed {mime}" in message
    assert "no timestamps" in message
    assert "citations" in message


def test_rss_untimed_transcript_falls_through_to_the_next_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Next:
        def get(self, episode: EpisodeInput) -> Transcript:
            return Transcript(
                video_id=episode.resolved_id(),
                segments=[Segment(start=0.0, text="asr")],
                source="assemblyai",
            )

    feed = _feed_with(_tag("https://cdn.example.com/ep1.txt", "text/plain"))
    _serve(monkeypatch, {_FEED_URL: feed})
    chain = ChainTranscriptProvider([_rss(), _Next()])
    assert chain.get(_RSS_EPISODE).source == "assemblyai"


def test_rss_untimed_reason_survives_into_the_chain_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    feed = _feed_with(_tag("https://cdn.example.com/ep1.txt", "text/plain"))
    _serve(monkeypatch, {_FEED_URL: feed})
    with pytest.raises(TranscriptNotFound, match="skipped untimed text/plain"):
        ChainTranscriptProvider([_rss()]).get(_RSS_EPISODE)


def test_rss_prefers_timed_transcript_over_untimed_one(monkeypatch: pytest.MonkeyPatch) -> None:
    vtt = "WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nhi\n"
    feed = _feed_with(
        _tag("https://cdn.example.com/ep1.txt", "text/plain")
        + _tag("https://cdn.example.com/ep1.vtt", "text/vtt")
    )
    _serve(monkeypatch, {_FEED_URL: feed, "https://cdn.example.com/ep1.vtt": vtt})
    assert _rss().get(_RSS_EPISODE).source == "rss:vtt"


def test_rss_unsupported_type_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    feed = _feed_with(_tag("https://cdn.example.com/ep1.ttml", "application/ttml+xml"))
    _serve(monkeypatch, {_FEED_URL: feed})
    with pytest.raises(TranscriptNotFound, match="unsupported transcript type"):
        _rss().get(_RSS_EPISODE)


@pytest.mark.parametrize("order", ["vtt_then_srt", "srt_then_vtt"])
def test_rss_two_transcript_tags_pick_the_preferred_one(
    monkeypatch: pytest.MonkeyPatch, order: str
) -> None:
    """Regression guard for the feedparser bug the research found: feedparser
    keeps only the LAST repeated podcast:transcript tag, so a feed listing VTT
    then SRT yields the SRT and loses the speakers. Chorus walks every tag with
    xml.etree and must choose VTT either way, never fetching the SRT."""
    vtt_tag = _tag("https://cdn.example.com/ep1.vtt", "text/vtt")
    srt_tag = _tag("https://cdn.example.com/ep1.srt", "application/x-subrip")
    feed = _feed_with(vtt_tag + srt_tag if order == "vtt_then_srt" else srt_tag + vtt_tag)
    vtt = "WEBVTT\n\n00:00:01.000 --> 00:00:04.000\n<v Jack>from the vtt</v>\n"
    calls = _serve(monkeypatch, {_FEED_URL: feed, "https://cdn.example.com/ep1.vtt": vtt})
    transcript = _rss().get(_RSS_EPISODE)
    assert transcript.source == "rss:vtt"
    assert transcript.segments[0].speaker == "Jack"
    assert [c[1] for c in calls] == [_FEED_URL, "https://cdn.example.com/ep1.vtt"]


def test_rss_falls_through_to_srt_when_the_vtt_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    feed = _feed_with(
        _tag("https://cdn.example.com/ep1.vtt", "text/vtt")
        + _tag("https://cdn.example.com/ep1.srt", "application/x-subrip")
    )
    srt = "1\n00:00:01,000 --> 00:00:04,000\nfrom the srt\n"

    def handler(method: str, url: str, kwargs: dict[str, Any]) -> _FakeStreamResponse:
        if url == _FEED_URL:
            return _text(200, feed)
        if url.endswith(".vtt"):
            return _FakeStreamResponse(404)
        if url.endswith(".srt"):
            return _text(200, srt)
        raise AssertionError(url)

    _install_stream(monkeypatch, handler)
    transcript = _rss().get(_RSS_EPISODE)
    assert transcript.source == "rss:srt"
    assert transcript.segments[0].text == "from the srt"


def test_vtt_voice_spans_become_a_speaker_field_not_text() -> None:
    vtt = (
        "WEBVTT\n\n"
        "00:00:01.000 --> 00:00:03.000\n<v Jack Smith>Hello <i>there</i>.</v>\n\n"
        "00:00:03.500 --> 00:00:05.000\n<v.loud Dana>Fish &amp; chips</v>\n\n"
        "00:00:06.000 --> 00:00:08.000\nno voice here <00:00:07.000>at all\n"
    )
    segments = tc._parse_cues(vtt)
    assert [(s.start, s.text, s.speaker) for s in segments] == [
        (1.0, "Hello there.", "Jack Smith"),
        (3.5, "Fish & chips", "Dana"),
        (6.0, "no voice here at all", None),
    ]
    assert all("<" not in s.text for s in segments)


def test_json_transcript_speaker_is_kept(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = json.dumps(
        {
            "version": "1.0.0",
            "segments": [
                {"speaker": "Jack", "startTime": 1.0, "endTime": 4.0, "body": "hello"},
                {"speaker": 2, "startTime": 4.5, "endTime": 7.0, "body": "numeric speaker"},
                {"startTime": 8.0, "endTime": 9.0, "body": "anonymous"},
            ],
        }
    )
    feed = _feed_with(_tag("https://cdn.example.com/ep1.json", "application/json"))
    _serve(monkeypatch, {_FEED_URL: feed, "https://cdn.example.com/ep1.json": payload})
    transcript = _rss().get(_RSS_EPISODE)
    assert [s.speaker for s in transcript.segments] == ["Jack", "2", None]


# --------------------------------------------------------------------------
# Provenance and downstream compatibility
# --------------------------------------------------------------------------


def test_new_fields_are_optional_so_cached_payloads_still_validate() -> None:
    old = '{"video_id": "abc", "segments": [{"start": 1.0, "text": "hi"}], "source": "deepgram"}'
    transcript = Transcript.model_validate_json(old)
    assert transcript.source_audio_url is None
    assert transcript.segments[0].speaker is None


def test_speaker_and_source_audio_url_round_trip_through_the_cache(tmp_path: Path) -> None:
    cache = SqliteTranscriptCache(tmp_path / "c.db")
    cache.put(
        Transcript(
            video_id="rss-abc",
            segments=[Segment(start=1.0, text="hi", speaker="Speaker A")],
            source="assemblyai",
            source_audio_url=_AUDIO_URL,
        )
    )
    loaded = cache.get("rss-abc")
    assert loaded is not None
    assert loaded.segments[0].speaker == "Speaker A"
    assert loaded.source_audio_url == _AUDIO_URL


def test_curation_windows_ignore_speaker_so_text_stays_clean() -> None:
    segments = [
        Segment(start=0.0, text="first line", speaker="Speaker A"),
        Segment(start=10.0, text="second line", speaker="Speaker B"),
        Segment(start=100.0, text="next window", speaker="Jack"),
    ]
    windows = window_segments(segments)
    assert [w.text for w in windows] == ["first line second line", "next window"]
    assert all("Speaker" not in w.text and "Jack" not in w.text for w in windows)


# --------------------------------------------------------------------------
# Late-published transcripts: the cache never stores a negative result
# --------------------------------------------------------------------------


def test_negative_results_are_never_cached_so_a_late_transcript_is_picked_up(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cache = SqliteTranscriptCache(tmp_path / "c.db")
    provider = CachingTranscriptProvider(ChainTranscriptProvider([_rss()]), cache)
    episode_id = _RSS_EPISODE.resolved_id()

    # Request 1: the feed item has no transcript tag yet.
    _serve(monkeypatch, {_FEED_URL: _feed_with("")})
    with pytest.raises(TranscriptNotFound):
        provider.get(_RSS_EPISODE)
    assert cache.get(episode_id) is None  # nothing stored for the miss

    # Request 2: the publisher has since added a VTT. Cache miss -> chain re-runs.
    vtt = "WEBVTT\n\n00:00:01.000 --> 00:00:04.000\n<v Jack>now it exists</v>\n"
    vtt_url = "https://cdn.example.com/ep1.vtt"
    _serve(
        monkeypatch,
        {_FEED_URL: _feed_with(_tag(vtt_url, "text/vtt")), vtt_url: vtt},
    )
    transcript = provider.get(_RSS_EPISODE)
    assert transcript.source == "rss:vtt"
    assert transcript.segments[0].text == "now it exists"

    # Request 3: now a hit; the network must not be touched.
    def fail(*a: object, **kw: object) -> None:
        raise AssertionError("cache hit must not reach the network")

    monkeypatch.setattr(tc.httpx, "stream", fail)
    assert provider.get(_RSS_EPISODE).segments[0].text == "now it exists"


def test_provider_errors_are_not_cached_either(tmp_path: Path) -> None:
    class _Flaky:
        def __init__(self) -> None:
            self.calls = 0

        def get(self, episode: EpisodeInput) -> Transcript:
            self.calls += 1
            if self.calls == 1:
                raise TranscriptProviderError("outage")
            return Transcript(
                video_id=episode.resolved_id(), segments=[Segment(start=0, text="ok")]
            )

    cache = SqliteTranscriptCache(tmp_path / "c.db")
    inner = _Flaky()
    provider = CachingTranscriptProvider(inner, cache)
    with pytest.raises(TranscriptProviderError):
        provider.get(_YT)
    assert cache.get(_YT.resolved_id()) is None
    assert provider.get(_YT).segments[0].text == "ok"
    assert inner.calls == 2
