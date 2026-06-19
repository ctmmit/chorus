"""Audio rendering: voice the script into a downloadable episode.

Spine floor (ENGINEERING_REVIEW Q1): a single-voice opinionated monologue.
Real rendering is podcast-creator + esperanto -> ElevenLabs; offline (no key) a
MockAudioRenderer writes a placeholder artifact so the lifecycle + path_test
work end-to-end. Two-host dialogue is a layer, not the spine.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Protocol, runtime_checkable

from chorus.models import Script

log = logging.getLogger("chorus.audio")

ARTIFACT_DIR = Path(__file__).resolve().parent.parent / "artifacts"


@runtime_checkable
class AudioRenderer(Protocol):
    def render(self, script: Script, soul: str, out_dir: Path) -> Path: ...


class MockAudioRenderer:
    """Writes the monologue as a text artifact and returns its path. Stands in
    for the TTS engine; lets the job reach status=done offline."""

    def render(self, script: Script, soul: str, out_dir: Path = ARTIFACT_DIR) -> Path:
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"episode_{script.soul_version}.txt"
        path.write_text(script.monologue, encoding="utf-8")
        log.info("audio(mock): wrote %s", path)
        return path


class PodcastCreatorRenderer:
    """Real renderer: podcast-creator (LangGraph) + esperanto -> ElevenLabs.
    Single-voice profile is the spine floor. Activated when a key is present."""

    def __init__(self, api_key: str) -> None:
        self.api_key = api_key

    def render(self, script: Script, soul: str, out_dir: Path = ARTIFACT_DIR) -> Path:
        out_dir.mkdir(parents=True, exist_ok=True)
        # Wired when ELEVENLABS_API_KEY is provided:
        #   from podcast_creator import create_podcast
        #   create_podcast(content=script.monologue, episode_profile="single_voice",
        #                  output_dir=str(out_dir), ...)  # esperanto -> ElevenLabs
        raise NotImplementedError(
            "PodcastCreatorRenderer requires ELEVENLABS_API_KEY and the podcast-creator dep"
        )


def get_audio_renderer() -> AudioRenderer:
    if os.environ.get("ELEVENLABS_API_KEY"):
        return PodcastCreatorRenderer(os.environ["ELEVENLABS_API_KEY"])
    log.warning("audio: ELEVENLABS_API_KEY absent — using MockAudioRenderer")
    return MockAudioRenderer()
