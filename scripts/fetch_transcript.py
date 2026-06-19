"""Pull a YouTube transcript (captions) with timestamps, for fixtures.

Local fixture tool only (run from a residential IP). Production transcript
sourcing is a separate concern — see docs/ENGINEERING_REVIEW.md Q2.

Usage:
    python scripts/fetch_transcript.py <youtube_url_or_id> [--out DIR]

Writes fixtures/transcripts/<video_id>.json:
    {"video_id", "segments": [{"start": float, "text": str}, ...]}
Segments keep start times so citation resolution has timestamps to resolve to.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from youtube_transcript_api import YouTubeTranscriptApi

ID_RE = re.compile(r"(?:v=|/shorts/|youtu\.be/|/embed/)([A-Za-z0-9_-]{11})")


def video_id(s: str) -> str:
    m = ID_RE.search(s)
    if m:
        return m.group(1)
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", s):
        return s
    raise SystemExit(f"could not parse a video id from: {s}")


def fetch(vid: str) -> list[dict]:
    """Handle both youtube-transcript-api APIs (v1 instance vs legacy static)."""
    try:  # v1.x: instance .fetch() -> FetchedTranscript of snippet objects
        raw = YouTubeTranscriptApi().fetch(vid)
        return [{"start": round(float(s.start), 2), "text": s.text} for s in raw]
    except (TypeError, AttributeError):  # legacy: static get_transcript -> list[dict]
        raw = YouTubeTranscriptApi.get_transcript(vid)
        return [{"start": round(float(s["start"]), 2), "text": s["text"]} for s in raw]


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    out_dir = Path("fixtures/transcripts")
    if "--out" in sys.argv:
        out_dir = Path(sys.argv[sys.argv.index("--out") + 1])
    out_dir.mkdir(parents=True, exist_ok=True)

    vid = video_id(sys.argv[1])
    segments = fetch(vid)
    payload = {"video_id": vid, "segments": segments}
    dest = out_dir / f"{vid}.json"
    dest.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    words = sum(len(s["text"].split()) for s in segments)
    print(f"OK {vid}: {len(segments)} segments, ~{words} words -> {dest}")


if __name__ == "__main__":
    main()
