"""LIVE full digest over all 5 clean episodes, through the API with real
providers. Spends Anthropic (curation + script) and ElevenLabs (audio). Run:

    .venv/Scripts/python scripts/full_digest_live.py
"""
from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from chorus.config import load_env  # noqa: E402

load_env()

from fastapi.testclient import TestClient  # noqa: E402

from chorus.app import create_app  # noqa: E402
from chorus.audio import ARTIFACT_DIR  # noqa: E402
from chorus.jobs import SqliteJobStore  # noqa: E402
from chorus.pipeline import default_deps  # noqa: E402

FIX = Path(__file__).resolve().parent.parent / "fixtures"
CLEAN = ["gs39QFYIbBY", "c4tvVKDhpiY", "wAnDWfEIwoE", "xKZ_8ULR91Y", "2Ryr95iiYNk"]


def main() -> None:
    payload = {
        "soul": (FIX / "souls" / "soul_investor.md").read_text(encoding="utf-8"),
        "context": (FIX / "context.md").read_text(encoding="utf-8"),
        "episodes": [{"video_id": v} for v in CLEAN],
        "highlight_count": 4,
    }
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        store = SqliteJobStore(Path(d) / "jobs.db")
        client = TestClient(create_app(store, default_deps()))
        print(f"LIVE: full digest over {len(CLEAN)} episodes (real Haiku/Sonnet/ElevenLabs)...")
        t = time.perf_counter()
        job_id = client.post("/digest", json=payload).json()["job_id"]
        body = client.get(f"/digest/{job_id}").json()
        elapsed = time.perf_counter() - t

        d_ = body.get("digest") or {}
        print(f"\nstatus={body['status']}  in {elapsed:.0f}s  soul_origin={d_.get('soul_origin')}")
        for ep in d_.get("episodes", []):
            tag = "REFUSED" if ep["refused"] else f"{len(ep['highlights'])} highlights"
            print(f"  {ep['episode_id']}: {tag}")
        total = sum(len(ep["highlights"]) for ep in d_.get("episodes", []))
        print(f"total highlights: {total}")
        print(f"audio_url: {body.get('audio_url')}")
        if body.get("audio_url"):
            mp3 = ARTIFACT_DIR / Path(body["audio_url"]).name
            if mp3.exists():
                print(f"audio file: {mp3} ({mp3.stat().st_size} bytes)")
        client.close()
        store.close()
    print("\nFULL DIGEST OK")


if __name__ == "__main__":
    main()
