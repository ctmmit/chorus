"""Goal 3.2 / ISS-001 — audio rendering: mock artifact + real ElevenLabs path
(with httpx mocked, so no spend) + renderer selection."""
from __future__ import annotations

from pathlib import Path

import pytest

from chorus.audio import ElevenLabsRenderer, MockAudioRenderer, get_audio_renderer
from chorus.models import Script, Take


def _script() -> Script:
    takes = [Take(text="A take.", take_type="idea", episode_id="abc", segment_timestamp=42.0)]
    return Script(soul_version="deadbeef", takes=takes, monologue="A take.")


def test_mock_render_writes_downloadable_artifact(tmp_path: Path) -> None:
    path = MockAudioRenderer(out_dir=tmp_path).render(_script(), soul="x")
    assert path.exists()
    assert path.read_text(encoding="utf-8") == "A take."


def test_mock_render_names_by_soul_version(tmp_path: Path) -> None:
    path = MockAudioRenderer(out_dir=tmp_path).render(_script(), soul="x")
    assert "deadbeef" in path.name


class _FakeResp:
    content = b"ID3\x03fake-mp3-bytes"

    def raise_for_status(self) -> None:  # noqa: D401
        return None


def test_elevenlabs_renderer_posts_and_writes_audio(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    def fake_post(url: str, **kw: object) -> _FakeResp:
        captured["url"] = url
        captured["json"] = kw.get("json")
        captured["params"] = kw.get("params")
        return _FakeResp()

    monkeypatch.setattr("chorus.audio.httpx.post", fake_post)
    path = ElevenLabsRenderer("key", out_dir=tmp_path, voice_id="voice123").render(_script(), soul="x")

    assert path.suffix == ".mp3"
    assert path.read_bytes() == _FakeResp.content
    assert "text-to-speech/voice123" in captured["url"]
    assert captured["json"]["text"] == "A take."  # type: ignore[index]
    assert captured["params"]["output_format"].startswith("mp3")  # type: ignore[index]


def test_get_audio_renderer_selects_elevenlabs_with_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ELEVENLABS_API_KEY", "x")
    assert isinstance(get_audio_renderer(), ElevenLabsRenderer)


def test_get_audio_renderer_mock_without_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    assert isinstance(get_audio_renderer(), MockAudioRenderer)
