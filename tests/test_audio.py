"""Goal 3.2 / ISS-001 — audio rendering: mock artifact + real ElevenLabs path
(with httpx mocked, so no spend) + renderer selection.

Phase B: renderers return `RenderedAudio` (bytes + metadata) instead of
writing files themselves — chorus.artifacts.ArtifactStore is what "writes"
now (see tests/test_store_contract.py and the ArtifactStore-specific cases
below)."""
from __future__ import annotations

from pathlib import Path

import pytest

from chorus.artifacts import LocalArtifactStore
from chorus.audio import ElevenLabsRenderer, MockAudioRenderer, RenderedAudio, get_audio_renderer
from chorus.models import Script, Take


def _script() -> Script:
    takes = [Take(text="A take.", take_type="idea", episode_id="abc", segment_timestamp=42.0)]
    return Script(soul_version="deadbeef", takes=takes, monologue="A take.")


def test_mock_render_returns_monologue_as_text_bytes() -> None:
    rendered = MockAudioRenderer().render(_script(), soul="x", job_id="job1")
    assert isinstance(rendered, RenderedAudio)
    assert rendered.data == b"A take."
    assert rendered.media_type == "text/plain"
    assert rendered.extension == "txt"


def test_render_rejects_unsafe_job_id() -> None:
    with pytest.raises(ValueError):
        MockAudioRenderer().render(_script(), soul="x", job_id="../etc")


class _FakeResp:
    content = b"ID3\x03fake-mp3-bytes"

    def raise_for_status(self) -> None:  # noqa: D401
        return None


def test_elevenlabs_renderer_posts_and_returns_audio_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    def fake_post(url: str, **kw: object) -> _FakeResp:
        captured["url"] = url
        captured["json"] = kw.get("json")
        captured["params"] = kw.get("params")
        return _FakeResp()

    monkeypatch.setattr("chorus.audio.httpx.post", fake_post)
    rendered = ElevenLabsRenderer("key", voice_id="voice123").render(_script(), soul="x", job_id="job1")

    assert isinstance(rendered, RenderedAudio)
    assert rendered.media_type == "audio/mpeg"
    assert rendered.extension == "mp3"
    assert rendered.data == _FakeResp.content
    assert "text-to-speech/voice123" in captured["url"]
    assert captured["json"]["text"] == "A take."  # type: ignore[index]
    assert captured["params"]["output_format"].startswith("mp3")  # type: ignore[index]


def test_get_audio_renderer_selects_elevenlabs_with_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ELEVENLABS_API_KEY", "x")
    assert isinstance(get_audio_renderer(), ElevenLabsRenderer)


def test_get_audio_renderer_mock_without_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    assert isinstance(get_audio_renderer(), MockAudioRenderer)


# --- ArtifactStore: what actually writes the rendered bytes now -----------


def test_local_artifact_store_writes_and_returns_local_url(tmp_path: Path) -> None:
    store = LocalArtifactStore(directory=tmp_path)
    url = store.put("episode_job1.txt", b"hello", "text/plain")
    assert url == "/artifacts/episode_job1.txt"
    assert (tmp_path / "episode_job1.txt").read_bytes() == b"hello"


def test_local_artifact_store_names_are_unique_per_job(tmp_path: Path) -> None:
    # Two jobs with the SAME soul must not overwrite each other's artifact —
    # the artifact NAME (built from artifact_stem(job_id) by the pipeline),
    # not the store, is what guarantees this; check the store honors whatever
    # name it is given.
    store = LocalArtifactStore(directory=tmp_path)
    a = store.put("episode_job1.txt", b"x", "text/plain")
    b = store.put("episode_job2.txt", b"x", "text/plain")
    assert a != b
    assert "job1" in a and "job2" in b
