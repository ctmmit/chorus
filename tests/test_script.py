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
    profile — single-voice stays the default."""
    digest, soul, context = _digest(ANDREESSEN, "soul_investor.md")
    default_script = MockScriptComposer().write_script(digest, soul, context)
    explicit_script = MockScriptComposer().write_script(digest, soul, context, MONOLOGUE_PROFILE)
    assert default_script == explicit_script
    assert explicit_script.format == "monologue"
    assert {t.speaker for t in explicit_script.turns} == {"host"}


def test_dialogue_mock_has_both_speakers_and_cohost_follows_host() -> None:
    digest, soul, context = _digest(ANDREESSEN, "soul_investor.md")
    script = MockScriptComposer().write_script(digest, soul, context, TWO_HOST_PROFILE)

    assert script.format == "dialogue"
    assert {t.speaker for t in script.turns} == {"host", "cohost"}
    for prev, turn in zip(script.turns, script.turns[1:], strict=False):
        if turn.speaker == "cohost":
            # The cohost reacts to the moment the host just raised.
            assert prev.speaker == "host"
            assert prev.citations == turn.citations


def test_every_citation_resolves_to_a_highlight_or_a_featured_brief() -> None:
    digest, soul, context = _digest(ANDREESSEN, "soul_investor.md")
    for profile in (MONOLOGUE_PROFILE, TWO_HOST_PROFILE):
        script = MockScriptComposer().write_script(digest, soul, context, profile)
        valid = {(h.episode_id, round(h.segment_timestamp)) for h in digest.highlights}
        briefed = {b.episode_id for b in script.briefs}
        assert script.turns
        for turn in script.turns:
            for cite in turn.citations:
                if cite.segment_timestamp is None:
                    assert cite.episode_id in briefed
                else:
                    assert (cite.episode_id, round(cite.segment_timestamp)) in valid


def test_source_is_set_up_before_any_highlight_is_discussed() -> None:
    """Context before commentary: a source's first line cites its brief, not a moment."""
    digest, soul, context = _digest(ANDREESSEN, "soul_investor.md")
    script = MockScriptComposer().write_script(digest, soul, context, TWO_HOST_PROFILE)

    first_highlight = next(i for i, t in enumerate(script.turns) if t.segment_timestamp is not None)
    setup = [t for t in script.turns[:first_highlight] if t.move == "setup"]
    assert any(t.citations and t.citations[0].segment_timestamp is None for t in setup)
    assert script.outline is not None
    assert [s.kind for s in script.outline.segments][0] == "intro"
    assert [s.kind for s in script.outline.segments][-1] == "close"


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


def test_no_profile_is_a_single_voice_monologue() -> None:
    digest, soul, context = _digest(ANDREESSEN, "soul_investor.md")
    script = MockScriptComposer().write_script(digest, soul, context)
    assert script.format == "monologue"
    assert script.monologue == "\n\n".join(t.text for t in script.turns)
