"""LIVE smoke test — spends real Anthropic API. Loads .env.local and runs ONE
short episode through the REAL Haiku curation + Sonnet script (audio left to the
mock; the ElevenLabs renderer is still a stub). Run:

    .venv/Scripts/python scripts/smoke_live.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # Windows console (cp1252)

from chorus.config import load_env  # noqa: E402

load_env()

from chorus.curation import build_digest, window_segments  # noqa: E402
from chorus.ingest import ingest  # noqa: E402
from chorus.llm import get_llm_client  # noqa: E402
from chorus.models import EpisodeInput  # noqa: E402
from chorus.audio import get_audio_renderer  # noqa: E402
from chorus.script import get_script_composer  # noqa: E402
from chorus.transcripts import FixtureTranscriptProvider  # noqa: E402

FIX = Path(__file__).resolve().parent.parent / "fixtures"
EPISODE = "2Ryr95iiYNk"  # Gavin Baker — shortest clean transcript (cheapest)


def main() -> None:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("SMOKE FAIL: no ANTHROPIC_API_KEY (is .env.local filled?)")
        sys.exit(1)

    soul = (FIX / "souls" / "soul_investor.md").read_text(encoding="utf-8")
    context = (FIX / "context.md").read_text(encoding="utf-8")
    ig = ingest([EpisodeInput(video_id=EPISODE)], FixtureTranscriptProvider())
    n_windows = len(window_segments(ig.resolved[0].transcript.segments))
    print(f"LIVE: scoring ~{n_windows} windows of {EPISODE} with real Haiku...")

    digest = build_digest(ig, soul, context, get_llm_client(), highlight_count=4)
    print(f"\nHIGHLIGHTS ({len(digest.highlights)}):")
    for h in digest.highlights:
        ts = f"{int(h.segment_timestamp) // 60}:{int(h.segment_timestamp) % 60:02d}"
        print(f"  [{ts}] score={h.relevance_score}  {h.why_surface}")
        print(f"        {h.quote[:90]}")

    print("\nSCRIPT (real Sonnet):")
    script = get_script_composer().write_script(digest, soul, context)
    print(script.monologue[:1200])

    renderer = get_audio_renderer()
    print(f"\nRendering audio via {type(renderer).__name__}...")
    rendered = renderer.render(script, soul, job_id="smoke")
    from chorus.artifacts import LocalArtifactStore

    url = LocalArtifactStore().put(f"smoke.{rendered.extension}", rendered.data, rendered.media_type)
    print(f"AUDIO: {url} ({len(rendered.data)} bytes)")
    print("\nSMOKE OK")


if __name__ == "__main__":
    main()
