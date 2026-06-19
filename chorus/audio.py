"""Audio rendering: voice the script into a downloadable episode.

Spine floor (ENGINEERING_REVIEW Q1): a single-voice opinionated monologue. We
voice it with a direct ElevenLabs TTS call (httpx, no heavy deps). The two-host
dialogue LAYER is reserved for podcast-creator + esperanto later. Offline (no
key) a MockAudioRenderer writes a placeholder so the lifecycle + path_test work.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Protocol, runtime_checkable

import httpx

from chorus.models import Script

log = logging.getLogger("chorus.audio")

ARTIFACT_DIR = Path(__file__).resolve().parent.parent / "artifacts"

ELEVEN_TTS_URL = "https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
DEFAULT_VOICE_ID = "21m00Tcm4TlvDq8ikWAM"  # ElevenLabs "Rachel" (override via ELEVENLABS_VOICE_ID)
DEFAULT_MODEL = "eleven_multilingual_v2"
OUTPUT_FORMAT = "mp3_44100_128"
RENDER_TIMEOUT_S = 120.0


@runtime_checkable
class AudioRenderer(Protocol):
    def render(self, script: Script, soul: str) -> Path: ...


class MockAudioRenderer:
    """Writes the monologue as a text artifact and returns its path. Stands in
    for the TTS engine offline; lets the job reach status=done without a key."""

    def __init__(self, out_dir: Path = ARTIFACT_DIR) -> None:
        self.out_dir = out_dir

    def render(self, script: Script, soul: str) -> Path:
        self.out_dir.mkdir(parents=True, exist_ok=True)
        path = self.out_dir / f"episode_{script.soul_version}.txt"
        path.write_text(script.monologue, encoding="utf-8")
        log.info("audio(mock): wrote %s", path)
        return path


class ElevenLabsRenderer:
    """Real single-voice TTS via ElevenLabs. Activated when a key is present.
    Voices `script.monologue` and writes an mp3 the API can serve from /artifacts."""

    def __init__(
        self,
        api_key: str,
        out_dir: Path = ARTIFACT_DIR,
        voice_id: str | None = None,
        model_id: str = DEFAULT_MODEL,
    ) -> None:
        self.api_key = api_key
        self.out_dir = out_dir
        self.voice_id = voice_id or os.environ.get("ELEVENLABS_VOICE_ID", DEFAULT_VOICE_ID)
        self.model_id = model_id

    def render(self, script: Script, soul: str) -> Path:
        self.out_dir.mkdir(parents=True, exist_ok=True)
        resp = httpx.post(
            ELEVEN_TTS_URL.format(voice_id=self.voice_id),
            headers={
                "xi-api-key": self.api_key,
                "accept": "audio/mpeg",
                "content-type": "application/json",
            },
            params={"output_format": OUTPUT_FORMAT},
            json={
                "text": script.monologue,
                "model_id": self.model_id,
                "voice_settings": {"stability": 0.5, "similarity_boost": 0.75},
            },
            timeout=RENDER_TIMEOUT_S,
        )
        resp.raise_for_status()
        path = self.out_dir / f"episode_{script.soul_version}.mp3"
        path.write_bytes(resp.content)
        log.info("audio(elevenlabs): wrote %s (%d bytes)", path, len(resp.content))
        return path


def get_audio_renderer() -> AudioRenderer:
    key = os.environ.get("ELEVENLABS_API_KEY")
    if key:
        return ElevenLabsRenderer(key)
    log.warning("audio: ELEVENLABS_API_KEY absent — using MockAudioRenderer")
    return MockAudioRenderer()
