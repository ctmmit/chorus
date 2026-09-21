"""Shared test configuration.

The real show transcripts (fixtures/transcripts/<video_id>.json) are NOT in the
public repo — they are captions of commercial podcasts and live in the private
fixtures repo (see fixtures/README.md). Tests that depend on them are skipped
when the files are absent; the synthetic `sample_public` transcript keeps the
whole spine covered in every environment.
"""
from __future__ import annotations

from pathlib import Path

import pytest

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
TRANSCRIPTS = FIXTURES / "transcripts"

# One representative real fixture; if it is present, assume the private set is.
PRIVATE_SENTINEL = TRANSCRIPTS / "c4tvVKDhpiY.json"
HAVE_PRIVATE_FIXTURES = PRIVATE_SENTINEL.exists()

# Modules whose tests reference real show transcripts.
PRIVATE_FIXTURE_MODULES = {
    "test_api",
    "test_bootstrap",
    "test_curation",
    "test_ingest",
    "test_script",
    "test_select",
}

requires_private_fixtures = pytest.mark.skipif(
    not HAVE_PRIVATE_FIXTURES,
    reason="private show transcripts not present (run scripts/sync_private_fixtures)",
)


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if HAVE_PRIVATE_FIXTURES:
        return
    for item in items:
        if item.module.__name__ in PRIVATE_FIXTURE_MODULES:  # type: ignore[attr-defined]
            item.add_marker(requires_private_fixtures)
