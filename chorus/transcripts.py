"""Transcript resolution.

ENGINEERING_REVIEW Q2: production uses a managed transcript API, but dev and
`path_test` resolve against the pre-transcribed fixtures in fixtures/transcripts/
so nothing ever depends on a live third party. Both implement TranscriptProvider;
the managed-API provider is added in a later iteration behind the same interface.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Protocol, runtime_checkable

from chorus.models import Transcript

_ID_RE = re.compile(r"(?:v=|/shorts/|youtu\.be/|/embed/)([A-Za-z0-9_-]{11})")
_BARE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")

# fixtures/transcripts/ lives at the project root, two levels up from this file.
FIXTURE_TRANSCRIPTS_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "transcripts"


class TranscriptNotFound(Exception):
    """No transcript could be resolved for an episode (missing/removed/no captions)."""


def extract_video_id(url_or_id: str) -> str:
    match = _ID_RE.search(url_or_id)
    if match:
        return match.group(1)
    if _BARE_ID_RE.match(url_or_id):
        return url_or_id
    raise ValueError(f"cannot parse a video id from: {url_or_id!r}")


@runtime_checkable
class TranscriptProvider(Protocol):
    def get(self, video_id: str) -> Transcript: ...


class FixtureTranscriptProvider:
    """Reads `<video_id>.json` from the fixtures directory."""

    def __init__(self, directory: Path | None = None) -> None:
        self.directory = directory or FIXTURE_TRANSCRIPTS_DIR

    def get(self, video_id: str) -> Transcript:
        path = self.directory / f"{video_id}.json"
        if not path.exists():
            raise TranscriptNotFound(f"no transcript fixture for {video_id} at {path}")
        data = json.loads(path.read_text(encoding="utf-8"))
        return Transcript(video_id=data.get("video_id", video_id), segments=data["segments"])
