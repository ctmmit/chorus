"""Regenerate web/mocks/network.json from the real /network endpoint.

Registers a handful of personas against an offline API (fixture transcripts,
mock LLM/TTS — same pattern as scripts/golden_path.py) so the network graph
mock is a real GET /network response, not hand-authored JSON. Run whenever
the shape of Persona or the /network payload changes:

    python scripts/generate_network_mock.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from chorus.app import create_app  # noqa: E402
from chorus.artifacts import LocalArtifactStore  # noqa: E402
from chorus.audio import MockAudioRenderer  # noqa: E402
from chorus.jobs import SqliteJobStore  # noqa: E402
from chorus.llm import MockLLMClient  # noqa: E402
from chorus.pipeline import Deps  # noqa: E402
from chorus.script import MockScriptComposer  # noqa: E402
from chorus.transcripts import FixtureTranscriptProvider  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
NETWORK_OUT_FILE = ROOT / "web" / "mocks" / "network.json"
PERSONAS_OUT_FILE = ROOT / "web" / "mocks" / "personas.json"

# A handful of personas spanning overlapping and non-overlapping shows, so
# the mocked graph has personas of different sizes and at least one show
# nobody but one persona listens to (exercises the small-node case in
# web/app/network/page.tsx).
SEED_PERSONAS = [
    {
        "name": "Value Investor",
        "description": "Skeptical of hype, allergic to adjectives.",
        "soul": (ROOT / "fixtures" / "souls" / "soul_investor.md").read_text(encoding="utf-8"),
        "shows": ["20VC with Harry Stebbings", "Sohn Conference Foundation", "Jane Street"],
        "cadence": "weekly",
    },
    {
        "name": "Pop-Culture Critic",
        "description": "Reads every recap so you don't have to.",
        "soul": (ROOT / "fixtures" / "souls" / "soul_popculture.md").read_text(encoding="utf-8"),
        "shows": ["Joshua Weissman"],
        "cadence": "daily",
    },
    {
        "name": "Founder Operator",
        "description": "Building in public, allergic to vanity metrics.",
        "soul": "Care about durable moats, unit economics, and founder track record.",
        "shows": ["20VC with Harry Stebbings", "Jane Street", "Huberman Lab"],
        "cadence": "adhoc",
    },
]


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        deps = Deps(
            provider=FixtureTranscriptProvider(),
            llm=MockLLMClient(),
            composer=MockScriptComposer(),
            renderer=MockAudioRenderer(out_dir=tmp_path / "artifacts"),
            artifacts=LocalArtifactStore(tmp_path / "artifacts"),
        )
        store = SqliteJobStore(tmp_path / "jobs.db")
        # Context-manager form so FastAPI's lifespan shutdown runs (closes
        # the sqlite connections) before the TemporaryDirectory tries to
        # delete the underlying files — required on Windows, where an open
        # handle blocks unlink.
        with TestClient(create_app(store, deps)) as client:
            personas_json = []
            for payload in SEED_PERSONAS:
                r = client.post("/personas", json=payload)
                r.raise_for_status()
                personas_json.append(r.json())

            network = client.get("/network")
            network.raise_for_status()
            network_json = network.json()

    NETWORK_OUT_FILE.write_text(json.dumps(network_json, indent=2) + "\n", encoding="utf-8")
    PERSONAS_OUT_FILE.write_text(json.dumps(personas_json, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {NETWORK_OUT_FILE}")
    print(f"wrote {PERSONAS_OUT_FILE}")


if __name__ == "__main__":
    main()
