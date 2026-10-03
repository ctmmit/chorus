"""LIVE smoke test — spends real Anthropic API. Loads .env.local and runs a few
fixture episodes through the REAL Haiku curation and the Sonnet script stage
(brief -> outline -> one call per segment), then writes the briefs, outline and
script to artifacts/script_smoke.json for review. Audio is opt-in because it
spends ElevenLabs credits. Run:

    .venv/Scripts/python scripts/smoke_live.py [--monologue] [--audio]
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # Windows console (cp1252)

from chorus.config import load_env

load_env()

from chorus import catalog
from chorus.audio import get_audio_renderer
from chorus.curation import build_digest
from chorus.ingest import ingest
from chorus.llm import get_llm_client
from chorus.models import MONOLOGUE_PROFILE, TWO_HOST_PROFILE, EpisodeInput
from chorus.script import get_script_composer
from chorus.transcripts import FixtureTranscriptProvider

ROOT = Path(__file__).resolve().parent.parent
FIX = ROOT / "fixtures"
OUT = ROOT / "artifacts" / "script_smoke.json"
# Gavin Baker (25 min), xKZ_8ULR91Y (30 min), Marc Andreessen on 20VC (76 min).
EPISODES = ["2Ryr95iiYNk", "xKZ_8ULR91Y", "c4tvVKDhpiY"]


def main() -> None:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("SMOKE FAIL: no ANTHROPIC_API_KEY (is .env.local filled?)")
        sys.exit(1)
    profile = MONOLOGUE_PROFILE if "--monologue" in sys.argv else TWO_HOST_PROFILE

    soul = (FIX / "souls" / "soul_investor.md").read_text(encoding="utf-8")
    context = (FIX / "context.md").read_text(encoding="utf-8")
    episodes = catalog.enrich([EpisodeInput(video_id=v) for v in EPISODES])
    ig = ingest(episodes, FixtureTranscriptProvider())
    print(f"LIVE: scoring {len(ig.resolved)} episodes with real Haiku...")
    digest = build_digest(ig, soul, context, get_llm_client(), highlight_count=4)
    for ep in digest.episodes:
        print(f"  {ep.show} — {ep.episode_title}: {len(ep.highlights)} highlight(s)")

    composer = get_script_composer()
    print("\nBRIEFS (real Sonnet)...")
    briefs = composer.write_briefs(digest, soul, context, profile)
    for b in briefs:
        people = ", ".join(f"{p.name} ({p.role})" for p in b.people) or "no names found"
        print(f"  {b.show} — {b.title}\n    people: {people}\n    thesis: {b.thesis}")

    print("\nOUTLINE...")
    outline = composer.write_outline(digest, briefs, soul, context, profile)
    for seg in outline.segments:
        print(f"  [{seg.kind}/{seg.size}] {seg.name}")

    print("\nSCRIPT (one call per segment)...")
    script = composer.write_script(digest, soul, context, profile, briefs=briefs, outline=outline)
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(script.model_dump_json(indent=2), encoding="utf-8")
    words = len(script.monologue.split())
    print(f"{len(script.turns)} lines, {words} words (~{words / 150:.1f} min). Full script: {OUT}\n")
    print(script.monologue[:3000])

    if "--audio" in sys.argv:
        renderer = get_audio_renderer()
        print(f"\nRendering audio via {type(renderer).__name__}...")
        rendered = renderer.render(script, soul, job_id="smoke")
        from chorus.artifacts import LocalArtifactStore

        url = LocalArtifactStore().put(f"smoke.{rendered.extension}", rendered.data, rendered.media_type)
        print(f"AUDIO: {url} ({len(rendered.data)} bytes)")
    print("\nSMOKE OK")


if __name__ == "__main__":
    main()
