"""Goal 3.1 — script synthesis: opinionated takes, every one traceable."""
from __future__ import annotations

from pathlib import Path

from chorus.curation import build_digest
from chorus.ingest import ingest
from chorus.llm import MockLLMClient
from chorus.models import TAKE_TYPES, EpisodeInput
from chorus.script import MockScriptComposer
from chorus.transcripts import FixtureTranscriptProvider

FIX = Path(__file__).resolve().parent.parent / "fixtures"
ANDREESSEN = "c4tvVKDhpiY"
EGGS = "IAgmW_gTxls"


def _digest(video_id: str, soul_name: str):
    ig = ingest([EpisodeInput(video_id=video_id)], FixtureTranscriptProvider())
    soul = (FIX / "souls" / soul_name).read_text(encoding="utf-8")
    context = (FIX / "context.md").read_text(encoding="utf-8")
    return build_digest(ig, soul, context, MockLLMClient()), soul, context


def test_script_has_traceable_takes() -> None:
    digest, soul, context = _digest(ANDREESSEN, "soul_investor.md")
    script = MockScriptComposer().write_script(digest, soul, context)

    assert script.takes, "clean episode should yield takes"
    valid = {(h.episode_id, round(h.segment_timestamp)) for h in digest.highlights}
    for t in script.takes:
        assert t.take_type in TAKE_TYPES
        assert (t.episode_id, round(t.segment_timestamp)) in valid, "take not traceable to a highlight"
        assert t.text in script.monologue


def test_script_matches_soul_provenance() -> None:
    digest, soul, context = _digest(ANDREESSEN, "soul_investor.md")
    script = MockScriptComposer().write_script(digest, soul, context)
    assert script.soul_version == digest.soul_version


def test_refused_episode_yields_no_takes() -> None:
    digest, soul, context = _digest(EGGS, "soul_investor.md")  # ungrounded -> refused
    script = MockScriptComposer().write_script(digest, soul, context)
    assert script.takes == []
    assert "Nothing cleared the bar" in script.monologue
