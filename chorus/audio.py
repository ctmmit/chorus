"""Audio rendering: voice the script into episode bytes.

Spine floor (ENGINEERING_REVIEW Q1): a single-voice opinionated monologue. We
voice it with a direct ElevenLabs TTS call (httpx, no heavy deps). Offline (no
key) a MockAudioRenderer returns the script text so the lifecycle + path_test
work.

Phase B: a renderer no longer writes files — Vercel's filesystem is ephemeral,
so `render` returns the bytes (`RenderedAudio`) and the pipeline hands them to
an `ArtifactStore` (chorus/artifacts.py), which is the thing that knows
whether "writing" means local disk or a Vercel Blob PUT.

Phase E (docs/DEVELOPMENT_PLAN.md §4): two-host dialogue LAYER, voiced with
ElevenLabs Text-to-Dialogue (`ElevenLabsDialogueRenderer`). `AudioRenderer.
render(script, soul, job_id)`'s signature does not change; which renderer
actually runs is decided by `script.format` (`ProfileAwareRenderer`), not by
threading a profile through `render` itself — `Deps`/`get_audio_renderer()`
are built once at process/app startup (chorus.pipeline.default_deps,
chorus.config_env), before any per-job `DigestRequest.profile` exists, so
per-job voice selection has to live on the script/env, not on the renderer's
construction-time wiring.

ElevenLabs Text-to-Dialogue, verified 21 Sep 2026 against
https://elevenlabs.io/docs/api-reference/text-to-dialogue/convert :

    POST https://api.elevenlabs.io/v1/text-to-dialogue
    Headers: xi-api-key: <key>, Content-Type: application/json
    Query:   output_format (optional, default mp3_44100_128 — same as the
             single-voice endpoint; we pass OUTPUT_FORMAT explicitly)
    Body:    {"inputs": [{"text": ..., "voice_id": ...}, ...],
              "model_id": "eleven_v3"}   # eleven_v3 is the documented default
    Limits:  at most 10 unique voice_ids per request (we use at most 2: host +
             cohost — well under it) and "keep the total character count
             across all inputs[].text values at or below 2,000 characters"
             for reliable generation (longer requests can terminate early or
             return a 422). DIALOGUE_MAX_CHARS below encodes that 2,000-char
             limit; turns are chunked into multiple requests when a script
             would exceed it, and the resulting mp3 chunks are concatenated —
             MP3 frames are independently decodable, so byte-concatenating
             complete frame streams plays back correctly in practice (no
             re-encoding needed), which is what we rely on here.
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
from chorus.models import Script, Turn

log = logging.getLogger("chorus.audio")

ELEVEN_TTS_URL = "https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
ELEVEN_DIALOGUE_URL = "https://api.elevenlabs.io/v1/text-to-dialogue"
DEFAULT_VOICE_ID = "21m00Tcm4TlvDq8ikWAM"  # ElevenLabs "Rachel" (override via ELEVENLABS_VOICE_ID)
# ElevenLabs "Adam" — a distinct, well-known premade voice id used across
# ElevenLabs' own examples/community docs (e.g. https://elevenlabs.io/docs/
# voicelab/pre-made-voices names Adam as one of the standard premade voices;
# the id itself, like Rachel's above, is looked up via GET /v1/voices — there
# is no static official table — and is stable/widely cited). Override via
# ELEVENLABS_COHOST_VOICE_ID.
DEFAULT_COHOST_VOICE_ID = "pNInz6obpgDQGcFmaJgB"
DEFAULT_MODEL = "eleven_multilingual_v2"
DIALOGUE_MODEL = "eleven_v3"  # Text-to-Dialogue's documented default model
OUTPUT_FORMAT = "mp3_44100_128"
RENDER_TIMEOUT_S = 120.0
# Text-to-Dialogue's documented per-request limit on total characters across
# every inputs[].text (see module docstring). Turns are chunked to stay at or
# under this.
DIALOGUE_MAX_CHARS = 2_000


class RenderedAudio(BaseModel):
    """What a renderer produces: bytes plus enough metadata for the caller to
    store them (content type) and name the artifact (extension)."""

    data: bytes
    media_type: str
    extension: str
    # True when this is not real audio (the offline mock renders the script
    # text). The pipeline surfaces it as a job warning so a caller can tell a
    # dev instance's placeholder from a rendered episode.
    placeholder: bool = False


@runtime_checkable
class AudioRenderer(Protocol):
    def render(self, script: Script, soul: str, job_id: str) -> RenderedAudio: ...


class MockAudioRenderer:
    """Returns the script's readable text as UTF-8 bytes. Stands in for the
    TTS engine offline; lets the job reach status=done without a key. Works
    unchanged for dialogue scripts: `script.monologue` already holds the
    "HOST: ...\\n\\nCOHOST: ..." transcript in that format (chorus/script.py),
    so this renders dialogue as its transcript text, same as single-voice."""

    def __init__(self, out_dir: Path = ARTIFACT_DIR) -> None:
        # `out_dir` is accepted (and unused beyond bookkeeping) for backward
        # compatibility with callers/tests that still construct this with a
        # scratch directory; rendering itself no longer touches disk.
        self.out_dir = out_dir

    def render(self, script: Script, soul: str, job_id: str) -> RenderedAudio:
        artifact_stem(job_id)  # validate job_id is filename-safe, as before
        data = script.monologue.encode("utf-8")
        log.info(
            "audio(mock): rendered %d bytes for job %s (format=%s)", len(data), job_id, script.format
        )
        return RenderedAudio(data=data, media_type="text/plain", extension="txt", placeholder=True)


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


def _chunk_turns(turns: list[Turn], max_chars: int) -> list[list[Turn]]:
    """Group turns into chunks whose summed `text` length stays at or under
    `max_chars`, preserving order. A single turn longer than max_chars still
    gets its own chunk (never dropped or truncated silently)."""
    chunks: list[list[Turn]] = []
    current: list[Turn] = []
    current_chars = 0
    for turn in turns:
        n = len(turn.text)
        if current and current_chars + n > max_chars:
            chunks.append(current)
            current = []
            current_chars = 0
        current.append(turn)
        current_chars += n
    if current:
        chunks.append(current)
    return chunks


class ElevenLabsDialogueRenderer:
    """Real two-host TTS via ElevenLabs Text-to-Dialogue. Activated when a key
    is present and `script.format == "dialogue"` (dispatched by
    `ProfileAwareRenderer`). Voices `script.turns` and returns the mp3 bytes,
    chunked at `DIALOGUE_MAX_CHARS` when the dialogue is long (see module
    docstring for the exact request shape and the documented limits).

    Voice ids: an explicit `host_voice_id`/`cohost_voice_id` (e.g. resolved by
    a caller from `EpisodeProfile.speakers[*].voice_id`) wins; otherwise falls
    back to `ELEVENLABS_VOICE_ID`/`ELEVENLABS_COHOST_VOICE_ID`, then the
    module defaults.
    """

    def __init__(
        self,
        api_key: str,
        host_voice_id: str | None = None,
        cohost_voice_id: str | None = None,
        model_id: str = DIALOGUE_MODEL,
    ) -> None:
        self.api_key = api_key
        self.host_voice_id = host_voice_id or os.environ.get("ELEVENLABS_VOICE_ID", DEFAULT_VOICE_ID)
        self.cohost_voice_id = cohost_voice_id or os.environ.get(
            "ELEVENLABS_COHOST_VOICE_ID", DEFAULT_COHOST_VOICE_ID
        )
        self.model_id = model_id

    def render(self, script: Script, soul: str, job_id: str) -> RenderedAudio:
        artifact_stem(job_id)  # validate job_id is filename-safe, as before
        if not script.turns:
            # Explicit failure, not a silent empty render: grounding dropped
            # every turn, or this was called on a non-dialogue script by
            # mistake. stage_audio treats this as non-fatal (audio_url stays
            # None, digest/script still stand) — see chorus/pipeline.py.
            raise ValueError(f"job {job_id}: dialogue script has no turns to render")

        voice_for = {"host": self.host_voice_id, "cohost": self.cohost_voice_id}
        chunks = _chunk_turns(script.turns, DIALOGUE_MAX_CHARS)
        parts: list[bytes] = []
        for chunk in chunks:
            inputs = [{"text": t.text, "voice_id": voice_for[t.speaker]} for t in chunk]
            resp = httpx.post(
                ELEVEN_DIALOGUE_URL,
                headers={
                    "xi-api-key": self.api_key,
                    "accept": "audio/mpeg",
                    "content-type": "application/json",
                },
                params={"output_format": OUTPUT_FORMAT},
                json={"inputs": inputs, "model_id": self.model_id},
                timeout=RENDER_TIMEOUT_S,
            )
            resp.raise_for_status()
            parts.append(resp.content)

        data = b"".join(parts)
        log.info(
            "audio(elevenlabs-dialogue): rendered %d bytes for job %s (%d turns, %d request(s))",
            len(data), job_id, len(script.turns), len(chunks),
        )
        return RenderedAudio(data=data, media_type="audio/mpeg", extension="mp3")


class ProfileAwareRenderer:
    """Dispatches on `script.format` — monologue to `monologue_renderer`,
    dialogue to `dialogue_renderer`. `AudioRenderer.render`'s signature is
    unchanged; the format lives on the script (chorus/models.py Script.format),
    not on a request/profile argument threaded through render()."""

    def __init__(self, monologue_renderer: AudioRenderer, dialogue_renderer: AudioRenderer) -> None:
        self.monologue_renderer = monologue_renderer
        self.dialogue_renderer = dialogue_renderer

    def render(self, script: Script, soul: str, job_id: str) -> RenderedAudio:
        renderer = self.dialogue_renderer if script.format == "dialogue" else self.monologue_renderer
        return renderer.render(script, soul, job_id)


def get_audio_renderer() -> AudioRenderer:
    key = os.environ.get("ELEVENLABS_API_KEY")
    if key:
        return ProfileAwareRenderer(ElevenLabsRenderer(key), ElevenLabsDialogueRenderer(key))
    log.warning("audio: ELEVENLABS_API_KEY absent — using MockAudioRenderer")
    return ProfileAwareRenderer(MockAudioRenderer(), MockAudioRenderer())
