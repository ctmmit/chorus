"""Golden-path executable eval — the v1 definition of done (ENG-REVIEW Q3/§6).

Drives the REAL API over the fixture model (POST /digest -> poll GET) with
offline deps, and asserts:
  1. clean run reaches `done` within the latency bound; every highlight's
     citation resolves to a real transcript timestamp; audio_url present.
  2. a missing-transcript episode mixed in is skipped, the run still completes,
     and it never leaks into highlights.
  3. the ungrounded fixture refuses ("nothing cleared the relevance bar").
  4. all-transcripts-fail returns `failed` (never an empty `done`).

Prints PATH_TEST GREEN and exits 0, or PATH_TEST FAIL: <reason> and exits 1.
`path_test.ps1` / `path_test.sh` delegate here.
"""
from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

# Make `chorus` importable when run as a standalone script (not via pytest).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from chorus.app import create_app  # noqa: E402
from chorus.artifacts import LocalArtifactStore  # noqa: E402
from chorus.audio import MockAudioRenderer  # noqa: E402
from chorus.curation import REFUSAL, citation_resolves  # noqa: E402
from chorus.jobs import SqliteJobStore  # noqa: E402
from chorus.llm import MockLLMClient  # noqa: E402
from chorus.models import EpisodeInput, Job  # noqa: E402
from chorus.pipeline import Deps  # noqa: E402
from chorus.script import MockScriptComposer  # noqa: E402
from chorus.transcripts import FixtureTranscriptProvider  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
FIX = ROOT / "fixtures"
LATENCY_BOUND_S = 90.0
CLEAN = ["gs39QFYIbBY", "c4tvVKDhpiY", "wAnDWfEIwoE", "xKZ_8ULR91Y", "2Ryr95iiYNk"]
MISSING = "KhZfxZ-C-2g"
EGGS = "IAgmW_gTxls"


def fail(msg: str) -> None:
    print(f"PATH_TEST FAIL: {msg}")
    sys.exit(1)


def _soul(name: str) -> str:
    return (FIX / "souls" / name).read_text(encoding="utf-8")


def _context() -> str:
    return (FIX / "context.md").read_text(encoding="utf-8")


def _payload(soul_name: str, ids: list[str]) -> dict:
    return {
        "soul": _soul(soul_name),
        "context": _context(),
        "episodes": [{"video_id": i} for i in ids],
        "highlight_count": 4,
    }


def _run(client: TestClient, soul_name: str, ids: list[str]) -> tuple[Job, float]:
    start = time.perf_counter()
    job_id = client.post("/digest", json=_payload(soul_name, ids)).json()["job_id"]
    body = client.get(f"/digest/{job_id}").json()
    return Job.model_validate(body), time.perf_counter() - start


def main() -> None:
    provider = FixtureTranscriptProvider()
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        tmp = Path(d)
        store = SqliteJobStore(tmp / "jobs.db")
        deps = Deps(
            provider,
            MockLLMClient(),
            MockScriptComposer(),
            MockAudioRenderer(out_dir=tmp),
            LocalArtifactStore(tmp),
        )
        client = TestClient(create_app(store, deps))

        # 1. clean run: done, within latency, citations resolve, audio present
        job, elapsed = _run(client, "soul_investor.md", CLEAN)
        if job.status.value != "done":
            fail(f"clean run status={job.status.value} (expected done)")
        if elapsed > LATENCY_BOUND_S:
            fail(f"clean run exceeded {LATENCY_BOUND_S}s ({elapsed:.1f}s)")
        if not job.digest or not job.digest.highlights:
            fail("clean run produced no highlights")
        for h in job.digest.highlights:
            transcript = provider.get(EpisodeInput(video_id=h.episode_id))
            if not citation_resolves(transcript, h.segment_timestamp, h.quote):
                fail(f"citation did not resolve at {h.segment_timestamp}s in {h.episode_id}")
        if not job.audio_url:
            fail("clean run produced no audio_url")

        # 2. missing-transcript mixed in: still done, never leaks into highlights
        mixed, _ = _run(client, "soul_investor.md", CLEAN + [MISSING])
        if mixed.status.value != "done":
            fail(f"mixed-with-missing status={mixed.status.value} (expected done)")
        leaked = [h for h in (mixed.digest.highlights if mixed.digest else []) if h.episode_id == MISSING]
        if leaked:
            fail("missing-transcript episode leaked into highlights")

        # 3. ungrounded: explicit refusal
        ung, _ = _run(client, "soul_investor.md", [EGGS])
        eps = ung.digest.episodes if ung.digest else []
        if not eps or not eps[0].refused or eps[0].refusal_reason != REFUSAL:
            fail("ungrounded episode did not refuse")

        # 4. all-transcripts-fail: failed, not empty done
        allfail, _ = _run(client, "soul_investor.md", [MISSING])
        if allfail.status.value != "failed" or not allfail.error:
            fail(f"all-missing status={allfail.status.value} (expected failed with error)")

        # 5. Layer-1 thesis: two souls diverge on the SAME episode
        inv, _ = _run(client, "soul_investor.md", ["c4tvVKDhpiY"])
        pop, _ = _run(client, "soul_popculture.md", ["c4tvVKDhpiY"])
        inv_ts = {round(h.segment_timestamp) for h in (inv.digest.highlights if inv.digest else [])}
        pop_ts = {round(h.segment_timestamp) for h in (pop.digest.highlights if pop.digest else [])}
        if not inv_ts or not pop_ts:
            fail("two-soul divergence: a soul produced no highlights")
        overlap = len(inv_ts & pop_ts) / max(len(inv_ts), len(pop_ts))
        if overlap >= 0.5:
            fail(f"two-soul divergence too weak (overlap {overlap:.0%})")

        # 6. Layer-2 selection: /shows lists shows; selecting one runs the spine green
        shows = client.get("/shows").json()
        if not shows:
            fail("catalog /shows returned nothing")
        pick = next(
            (s["show"] for s in shows if any(e["video_id"] == "c4tvVKDhpiY" for e in s["episodes"])),
            None,
        )
        if pick is None:
            fail("no show in the catalog contains the Andreessen episode")
        sel = client.post(
            "/digest/select",
            json={"soul": _soul("soul_investor.md"), "context": _context(), "shows": [pick]},
        )
        sel_job = Job.model_validate(client.get(f"/digest/{sel.json()['job_id']}").json())
        if sel_job.status.value != "done" or not (sel_job.digest and sel_job.digest.highlights):
            fail(f"selection run not green (status={sel_job.status.value})")

        client.close()
        store.close()  # release the SQLite handle so the temp dir can be removed

    print("PATH_TEST GREEN")
    sys.exit(0)


if __name__ == "__main__":
    main()
