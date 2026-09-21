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
from chorus.audio import (
    DIALOGUE_MAX_CHARS,
    ElevenLabsDialogueRenderer,
    ElevenLabsRenderer,
    MockAudioRenderer,
    ProfileAwareRenderer,
    RenderedAudio,
    get_audio_renderer,
)
from chorus.models import Script, Take, Turn


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
    # Phase E: get_audio_renderer() now returns a ProfileAwareRenderer that
    # dispatches on script.format; assert the real renderers were wired in.
    monkeypatch.setenv("ELEVENLABS_API_KEY", "x")
    renderer = get_audio_renderer()
    assert isinstance(renderer, ProfileAwareRenderer)
    assert isinstance(renderer.monologue_renderer, ElevenLabsRenderer)
    assert isinstance(renderer.dialogue_renderer, ElevenLabsDialogueRenderer)


def test_get_audio_renderer_mock_without_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    renderer = get_audio_renderer()
    assert isinstance(renderer, ProfileAwareRenderer)
    assert isinstance(renderer.monologue_renderer, MockAudioRenderer)
    assert isinstance(renderer.dialogue_renderer, MockAudioRenderer)


# --- Phase E: two-host dialogue rendering -----------------------------------


def _dialogue_script() -> Script:
    turns = [
        Turn(speaker="host", text="Margins are expanding.", episode_id="abc", segment_timestamp=42.0),
        Turn(speaker="cohost", text="Where's the number?", episode_id="abc", segment_timestamp=42.0),
    ]
    return Script(
        soul_version="deadbeef",
        takes=[],
        monologue="HOST: Margins are expanding.\n\nCOHOST: Where's the number?",
        turns=turns,
        format="dialogue",
    )


def test_mock_dialogue_render_returns_transcript_text() -> None:
    rendered = MockAudioRenderer().render(_dialogue_script(), soul="x", job_id="job1")
    assert rendered.data == b"HOST: Margins are expanding.\n\nCOHOST: Where's the number?"
    assert rendered.media_type == "text/plain"


def test_profile_aware_renderer_dispatches_on_script_format() -> None:
    class _Tagging:
        def __init__(self, tag: str) -> None:
            self.tag = tag
            self.calls: list[str] = []

        def render(self, script: Script, soul: str, job_id: str) -> RenderedAudio:
            self.calls.append(job_id)
            return RenderedAudio(data=self.tag.encode(), media_type="text/plain", extension="txt")

    mono, dia = _Tagging("mono"), _Tagging("dia")
    renderer = ProfileAwareRenderer(mono, dia)

    mono_script = Script(soul_version="x", takes=[], monologue="m", format="monologue")
    out = renderer.render(mono_script, soul="x", job_id="job1")
    assert out.data == b"mono" and mono.calls == ["job1"] and dia.calls == []

    dia_script = _dialogue_script()
    out = renderer.render(dia_script, soul="x", job_id="job2")
    assert out.data == b"dia" and dia.calls == ["job2"] and mono.calls == ["job1"]


def test_elevenlabs_dialogue_renderer_posts_expected_request_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}

    def fake_post(url: str, **kw: object) -> _FakeResp:
        captured["url"] = url
        captured["json"] = kw.get("json")
        captured["params"] = kw.get("params")
        captured["headers"] = kw.get("headers")
        return _FakeResp()

    monkeypatch.setattr("chorus.audio.httpx.post", fake_post)
    renderer = ElevenLabsDialogueRenderer("key", host_voice_id="hostvoice", cohost_voice_id="cohostvoice")
    rendered = renderer.render(_dialogue_script(), soul="x", job_id="job1")

    assert isinstance(rendered, RenderedAudio)
    assert rendered.media_type == "audio/mpeg" and rendered.extension == "mp3"
    assert rendered.data == _FakeResp.content
    assert captured["url"] == "https://api.elevenlabs.io/v1/text-to-dialogue"
    assert captured["headers"]["xi-api-key"] == "key"  # type: ignore[index]
    body = captured["json"]
    assert body["model_id"] == "eleven_v3"  # type: ignore[index]
    assert body["inputs"] == [  # type: ignore[index]
        {"text": "Margins are expanding.", "voice_id": "hostvoice"},
        {"text": "Where's the number?", "voice_id": "cohostvoice"},
    ]
    assert captured["params"]["output_format"].startswith("mp3")  # type: ignore[index]


def test_elevenlabs_dialogue_renderer_rejects_empty_turns() -> None:
    empty = Script(soul_version="x", takes=[], monologue="", turns=[], format="dialogue")
    with pytest.raises(ValueError):
        ElevenLabsDialogueRenderer("key").render(empty, soul="x", job_id="job1")


def test_elevenlabs_dialogue_renderer_chunks_at_documented_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # One turn's text right at the limit, then more turns that must spill
    # into a second request rather than exceeding DIALOGUE_MAX_CHARS total.
    big = "x" * DIALOGUE_MAX_CHARS
    turns = [
        Turn(speaker="host", text=big, episode_id="abc", segment_timestamp=1.0),
        Turn(speaker="cohost", text="one more line", episode_id="abc", segment_timestamp=2.0),
    ]
    script = Script(soul_version="x", takes=[], monologue="x", turns=turns, format="dialogue")

    requests: list[dict] = []

    def fake_post(url: str, **kw: object) -> _FakeResp:
        requests.append(kw.get("json"))  # type: ignore[arg-type]
        return _FakeResp()

    monkeypatch.setattr("chorus.audio.httpx.post", fake_post)
    rendered = ElevenLabsDialogueRenderer("key").render(script, soul="x", job_id="job1")

    assert len(requests) == 2  # chunked, not one oversized request
    assert requests[0]["inputs"] == [{"text": big, "voice_id": ElevenLabsDialogueRenderer("key").host_voice_id}]
    assert requests[1]["inputs"][0]["text"] == "one more line"
    # mp3 bytes concatenated across chunks (see module docstring).
    assert rendered.data == _FakeResp.content + _FakeResp.content


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
