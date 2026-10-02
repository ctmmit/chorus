"""Goal 3.1 — script synthesis: opinionated takes, every one traceable."""
from __future__ import annotations

from pathlib import Path

from chorus.curation import build_digest
from chorus.ingest import ingest
from chorus.llm import MockLLMClient
from chorus.models import MONOLOGUE_PROFILE, TAKE_TYPES, TWO_HOST_PROFILE, EpisodeInput
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


# --- Phase E: two-host dialogue (docs/DEVELOPMENT_PLAN.md §4) --------------


def test_monologue_profile_explicit_matches_default() -> None:
    """Passing MONOLOGUE_PROFILE explicitly must be identical to omitting a
    profile — single-voice stays the default and unchanged."""
    digest, soul, context = _digest(ANDREESSEN, "soul_investor.md")
    default_script = MockScriptComposer().write_script(digest, soul, context)
    explicit_script = MockScriptComposer().write_script(digest, soul, context, MONOLOGUE_PROFILE)
    assert default_script == explicit_script
    assert explicit_script.format == "monologue"
    assert explicit_script.turns == []


def test_dialogue_mock_alternates_speakers() -> None:
    digest, soul, context = _digest(ANDREESSEN, "soul_investor.md")
    script = MockScriptComposer().write_script(digest, soul, context, TWO_HOST_PROFILE)

    assert script.format == "dialogue"
    assert script.turns, "clean episode should yield dialogue turns"
    speakers = [t.speaker for t in script.turns]
    # Deterministic alternation: host states the take, cohost pushes back.
    assert speakers == ["host", "cohost"] * (len(speakers) // 2)


def test_dialogue_every_turn_traceable_to_a_highlight() -> None:
    digest, soul, context = _digest(ANDREESSEN, "soul_investor.md")
    script = MockScriptComposer().write_script(digest, soul, context, TWO_HOST_PROFILE)

    valid = {(h.episode_id, round(h.segment_timestamp)) for h in digest.highlights}
    assert script.turns
    for turn in script.turns:
        assert (turn.episode_id, round(turn.segment_timestamp)) in valid, "turn not traceable"


def test_dialogue_monologue_field_contains_readable_transcript_of_every_turn() -> None:
    digest, soul, context = _digest(ANDREESSEN, "soul_investor.md")
    script = MockScriptComposer().write_script(digest, soul, context, TWO_HOST_PROFILE)

    for turn in script.turns:
        assert f"{turn.speaker.upper()}: {turn.text}" in script.monologue


def test_dialogue_refused_episode_yields_no_turns() -> None:
    digest, soul, context = _digest(EGGS, "soul_investor.md")  # ungrounded -> refused
    script = MockScriptComposer().write_script(digest, soul, context, TWO_HOST_PROFILE)
    assert script.turns == []
    assert script.takes == []
    assert "Nothing cleared the bar" in script.monologue


def test_monologue_format_unchanged_from_before_phase_e() -> None:
    """No profile passed at all (the pre-Phase-E call shape) must still
    produce a monologue-format script with empty turns."""
    digest, soul, context = _digest(ANDREESSEN, "soul_investor.md")
    script = MockScriptComposer().write_script(digest, soul, context)
    assert script.format == "monologue"
    assert script.turns == []
