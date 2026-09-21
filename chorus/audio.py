"""Audio rendering: voice the script into episode bytes.

Spine floor (ENGINEERING_REVIEW Q1): a single-voice opinionated monologue. We
voice it with a direct ElevenLabs TTS call (httpx, no heavy deps). The two-host
dialogue LAYER is reserved for podcast-creator + esperanto later. Offline (no
key) a MockAudioRenderer returns the monologue as text so the lifecycle +
path_test work.

Phase B: a renderer no longer writes files — Vercel's filesystem is ephemeral,
so `render` returns the bytes (`RenderedAudio`) and the pipeline hands them to
an `ArtifactStore` (chorus/artifacts.py), which is the thing that knows
whether "writing" means local disk or a Vercel Blob PUT.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Protocol, runtime_checkable

import httpx
from pydantic import BaseModel

# ARTIFACT_DIR and artifact_stem now live in chorus.artifacts (naming/storage
# concerns, not rendering); re-exported here since callers historically did
# `from chorus.audio import ARTIFACT_DIR` / `artifact_stem`.
from chorus.artifacts import ARTIFACT_DIR, artifact_stem
from chorus.models import Script

log = logging.getLogger("chorus.audio")

ELEVEN_TTS_URL = "https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
DEFAULT_VOICE_ID = "21m00Tcm4TlvDq8ikWAM"  # ElevenLabs "Rachel" (override via ELEVENLABS_VOICE_ID)
DEFAULT_MODEL = "eleven_multilingual_v2"
OUTPUT_FORMAT = "mp3_44100_128"
RENDER_TIMEOUT_S = 120.0


class RenderedAudio(BaseModel):
    """What a renderer produces: bytes plus enough metadata for the caller to
    store them (content type) and name the artifact (extension)."""

    data: bytes
    media_type: str
    extension: str


@runtime_checkable
class AudioRenderer(Protocol):
    def render(self, script: Script, soul: str, job_id: str) -> RenderedAudio: ...


class MockAudioRenderer:
    """Returns the monologue as UTF-8 text bytes. Stands in for the TTS engine
    offline; lets the job reach status=done without a key."""

    def __init__(self, out_dir: Path = ARTIFACT_DIR) -> None:
        # `out_dir` is accepted (and unused beyond bookkeeping) for backward
        # compatibility with callers/tests that still construct this with a
        # scratch directory; rendering itself no longer touches disk.
        self.out_dir = out_dir

    def render(self, script: Script, soul: str, job_id: str) -> RenderedAudio:
        artifact_stem(job_id)  # validate job_id is filename-safe, as before
        data = script.monologue.encode("utf-8")
        log.info("audio(mock): rendered %d bytes for job %s", len(data), job_id)
        return RenderedAudio(data=data, media_type="text/plain", extension="txt")


class ElevenLabsRenderer:
    """Real single-voice TTS via ElevenLabs. Activated when a key is present.
    Voices `script.monologue` and returns the mp3 bytes."""

    def __init__(
        self,
        api_key: str,
        out_dir: Path = ARTIFACT_DIR,
        voice_id: str | None = None,
        model_id: str = DEFAULT_MODEL,
    ) -> None:
        self.api_key = api_key
        self.out_dir = out_dir  # unused beyond bookkeeping; see MockAudioRenderer
        self.voice_id = voice_id or os.environ.get("ELEVENLABS_VOICE_ID", DEFAULT_VOICE_ID)
        self.model_id = model_id

    def render(self, script: Script, soul: str, job_id: str) -> RenderedAudio:
        artifact_stem(job_id)  # validate job_id is filename-safe, as before
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
        log.info("audio(elevenlabs): rendered %d bytes for job %s", len(resp.content), job_id)
        return RenderedAudio(data=resp.content, media_type="audio/mpeg", extension="mp3")


def get_audio_renderer() -> AudioRenderer:
    key = os.environ.get("ELEVENLABS_API_KEY")
    if key:
        return ElevenLabsRenderer(key)
    log.warning("audio: ELEVENLABS_API_KEY absent — using MockAudioRenderer")
    return MockAudioRenderer()
