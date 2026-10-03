"""AnthropicScriptComposer against a fake Anthropic client (same injectable-
client pattern as tests/test_llm_batch.py): no key, no network.

Covers the brief -> outline -> per-segment loop: what each prompt carries
(source context, the full outline, the transcript so far, the final-segment
flag), the one feedback retry for briefs, outlines and segments, the
fallbacks when a retry fails too, and the line grounding rules.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pytest

from chorus.errors import TerminalError
from chorus.models import (
    MONOLOGUE_PROFILE,
    TWO_HOST_PROFILE,
    ConversationStyle,
    Digest,
    EpisodeDigest,
    EpisodeProfile,
    Highlight,
    SpeakerProfile,
)
from chorus.script import (
    AnthropicScriptComposer,
    DraftCite,
    DraftLine,
    LineContext,
    ScriptError,
    json_object,
    plan_episode,
    validate_line,
    vocabulary_of,
)


@dataclass
class _Block:
    type: str
    text: str


@dataclass
class _Msg:
    content: list[_Block]


class _FakeMessages:
    """Replays canned reply bodies in order and records every request."""

    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.requests: list[dict[str, Any]] = []

    def create(self, **kw: Any) -> _Msg:
        self.requests.append(kw)
        return _Msg(content=[_Block("text", self.replies.pop(0))])


class _FakeClient:
    def __init__(self, replies: list[str]) -> None:
        self.messages = _FakeMessages(replies)


def _user(request: dict[str, Any], index: int = 0) -> str:
    return str(request["messages"][index]["content"])


def _system(request: dict[str, Any]) -> str:
    return str(request["system"][0]["text"])


AIRED = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)


def _hl(episode_id: str, ts: float, excerpt: str, score: float = 0.9) -> Highlight:
    return Highlight(
        episode_id=episode_id,
        episode_title="Ep",
        segment_timestamp=ts,
        quote=excerpt[:40],
        relevance_score=score,
        why_surface="on-thesis",
        excerpt=excerpt,
    )


EP1 = EpisodeDigest(
    episode_id="ep1",
    episode_title="Marc Andreessen on the Future of Venture",
    show="20VC",
    published_at=AIRED,
    description="Marc Andreessen, co-founder of a16z, joins Harry Stebbings.",
    intro_excerpt="[Harry Stebbings] Welcome back. Today Marc Andreessen is here.",
    highlights=[
        _hl("ep1", 120.0, "[Marc Andreessen] Margins are expanding across the portfolio."),
        _hl("ep1", 300.0, "[Marc Andreessen] The moat is distribution, not models.", 0.8),
    ],
)
EP2 = EpisodeDigest(
    episode_id="ep2",
    episode_title="Why AI Capex Breaks",
    show="Odd Lots",
    published_at=AIRED,
    highlights=[_hl("ep2", 60.0, "[Speaker A] Capex is outrunning revenue by a wide margin.")],
)


def _digest(*episodes: EpisodeDigest) -> Digest:
    return Digest(soul_version="deadbeef", episodes=list(episodes))


def _brief_reply(points: list[float], people: list[dict[str, str]] | None = None) -> str:
    return json.dumps(
        {
            "people": people
            if people is not None
            else [{"name": "Marc Andreessen", "role": "guest", "credential": "co-founder of a16z"}],
            "context": "A new fund and a public debate about venture returns.",
            "thesis": "Software margins keep expanding and distribution is the moat.",
            "key_points": [{"text": f"Point at {p}", "timestamp": p} for p in points],
        }
    )


def _outline_reply(*source_ids: str) -> str:
    segments: list[dict[str, Any]] = [
        {"name": "Open", "kind": "intro", "source_ids": list(source_ids), "description": "d", "size": "short"}
    ]
    segments += [
        {"name": f"S {sid}", "kind": "source", "source_ids": [sid], "description": "d", "size": "long"}
        for sid in source_ids
    ]
    if len(source_ids) >= 2:
        segments.append(
            {"name": "Link", "kind": "connection", "source_ids": list(source_ids), "description": "d"}
        )
    segments.append({"name": "Close", "kind": "close", "source_ids": [], "description": "d", "size": "short"})
    return json.dumps({"segments": segments})


def _lines(*lines: dict[str, Any]) -> str:
    return json.dumps({"lines": list(lines)})


def _line(text: str, *cites: tuple[str, float | None], speaker: str = "host", move: str = "idea") -> dict[str, Any]:
    return {
        "speaker": speaker,
        "text": text,
        "move": move,
        "cites": [{"episode_id": e, "timestamp": t} for e, t in cites],
    }


INTRO = _lines(_line("Today we look at one conversation about venture.", ("ep1", None), move="setup"))
SOURCE = _lines(
    _line("On 20VC, Marc Andreessen sat down with Harry Stebbings.", ("ep1", None), move="setup"),
    _line("His first claim is that margins are expanding.", ("ep1", 120.0)),
)
CLOSE = _lines(_line("That's the episode.", move="setup"))


def _monologue_replies() -> list[str]:
    return [_brief_reply([120.0, 300.0]), _outline_reply("ep1"), INTRO, SOURCE, CLOSE]


# --- the full loop ----------------------------------------------------------------


def test_monologue_runs_brief_outline_then_one_call_per_segment() -> None:
    fake = _FakeClient(_monologue_replies())
    script = AnthropicScriptComposer(client=fake).write_script(_digest(EP1), "SOUL", "CTX", MONOLOGUE_PROFILE)

    assert len(fake.messages.requests) == 5  # brief, outline, intro, source, close
    assert script.format == "monologue"
    assert [t.speaker for t in script.turns] == ["host"] * 4
    assert script.monologue == "\n\n".join(t.text for t in script.turns)
    assert script.briefs[0].people[0].name == "Marc Andreessen"
    assert script.outline is not None and [s.kind for s in script.outline.segments] == ["intro", "source", "close"]
    # Only the highlight-anchored line becomes a take (API/email compatibility).
    assert [(t.episode_id, t.segment_timestamp) for t in script.takes] == [("ep1", 120.0)]


def test_brief_prompt_carries_who_what_when_and_the_excerpts() -> None:
    fake = _FakeClient(_monologue_replies())
    AnthropicScriptComposer(client=fake).write_script(_digest(EP1), "SOUL", "CTX")

    prompt = _user(fake.messages.requests[0])
    for expected in ("20VC", "Marc Andreessen on the Future of Venture", "30 Sep 2026",
                     "co-founder of a16z", "[Harry Stebbings] Welcome back",
                     "The moat is distribution, not models."):
        assert expected in prompt
    assert "Never invent a name" in _system(fake.messages.requests[0])


def test_each_segment_sees_the_outline_and_everything_written_before_it() -> None:
    fake = _FakeClient(_monologue_replies())
    AnthropicScriptComposer(client=fake).write_script(_digest(EP1), "SOUL", "CTX")
    intro_req, source_req, close_req = fake.messages.requests[2:]

    system = _system(source_req)
    assert '"kind": "source"' in system  # full outline, as JSON
    assert "co-founder of a16z" in system  # the brief
    assert "Context before commentary" in system
    assert source_req["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert _system(intro_req) == _system(close_req)  # one cached block for every segment

    assert "nothing yet" in _user(intro_req)
    assert "HOST: Today we look at one conversation about venture." in _user(source_req)
    assert "has not been introduced yet" in _user(source_req)
    assert "HOST: His first claim is that margins are expanding." in _user(close_req)
    assert "final segment" in _user(close_req)
    assert "final segment" not in _user(source_req)
    # The old opaque beat format is gone.
    assert "id=ep1 ts=" not in _user(source_req) + system


def test_dialogue_uses_both_personas_and_both_speakers() -> None:
    source = _lines(
        _line("On 20VC, Marc Andreessen sat down with Harry Stebbings.", ("ep1", None), move="setup"),
        _line("Margins are expanding, he says.", ("ep1", 120.0)),
        _line("Expanding by how much?", ("ep1", 120.0), speaker="cohost", move="question"),
    )
    fake = _FakeClient([_brief_reply([120.0]), _outline_reply("ep1"), INTRO, source, CLOSE])
    script = AnthropicScriptComposer(client=fake).write_script(_digest(EP1), "SOUL", "CTX", TWO_HOST_PROFILE)

    assert script.format == "dialogue"
    assert [t.speaker for t in script.turns].count("cohost") == 1
    assert "COHOST: Expanding by how much?" in script.monologue
    assert "skeptical foil" in _system(fake.messages.requests[2])
    assert "interruptions" in _system(fake.messages.requests[2])


def test_two_sources_get_a_connection_after_both_are_introduced() -> None:
    ep2_source = _lines(_line("Over on Odd Lots, the argument is that capex outruns revenue.", ("ep2", 60.0)))
    link = _lines(_line("Put these together and the margin story looks fragile.", ("ep1", None), ("ep2", None)))
    fake = _FakeClient(
        [_brief_reply([120.0]), _brief_reply([60.0], people=[]), _outline_reply("ep1", "ep2"),
         INTRO, SOURCE, ep2_source, link, CLOSE]
    )
    script = AnthropicScriptComposer(client=fake).write_script(_digest(EP1, EP2), "SOUL", "CTX")

    assert [s.kind for s in script.outline.segments] == ["intro", "source", "source", "connection", "close"]  # type: ignore[union-attr]
    assert [b.episode_id for b in script.briefs] == ["ep1", "ep2"]
    assert len(fake.messages.requests) == 8


# --- retries with feedback and fallbacks -------------------------------------------


def test_invalid_segment_lines_are_re_asked_once_with_the_problems() -> None:
    bad = _lines(_line("Margins are expanding.", ("ep1", 999.0)))
    fixed = _lines(_line("Margins are expanding.", ("ep1", 120.0)))
    fake = _FakeClient([_brief_reply([120.0]), _outline_reply("ep1"), INTRO, bad, fixed, CLOSE])
    script = AnthropicScriptComposer(client=fake).write_script(_digest(EP1), "SOUL", "CTX")

    repair = fake.messages.requests[4]
    assert repair["messages"][1] == {"role": "assistant", "content": bad}
    assert "999" in _user(repair, 2) and "not a surfaced moment" in _user(repair, 2)
    assert any(t.segment_timestamp == 120.0 for t in script.turns)


def test_lines_still_invalid_after_repair_are_dropped_and_valid_ones_kept() -> None:
    reply = _lines(
        _line("Margins are expanding.", ("ep1", 120.0)),
        _line("Revenue grew 40 percent last year."),  # uncited number
    )
    fake = _FakeClient([_brief_reply([120.0]), _outline_reply("ep1"), INTRO, reply, reply, CLOSE])
    script = AnthropicScriptComposer(client=fake).write_script(_digest(EP1), "SOUL", "CTX")

    texts = [t.text for t in script.turns]
    assert "Margins are expanding." in texts
    assert "Revenue grew 40 percent last year." not in texts


def test_unparseable_repair_keeps_the_valid_first_draft() -> None:
    reply = _lines(_line("Margins are expanding.", ("ep1", 120.0)), _line("It grew 40 percent."))
    fake = _FakeClient([_brief_reply([120.0]), _outline_reply("ep1"), INTRO, reply, "not json", CLOSE])
    script = AnthropicScriptComposer(client=fake).write_script(_digest(EP1), "SOUL", "CTX")
    assert "Margins are expanding." in [t.text for t in script.turns]


def test_outline_with_bad_structure_is_retried_with_the_violations() -> None:
    bad = json.dumps({"segments": [{"name": "S", "kind": "source", "source_ids": ["ep1"], "description": "d"}]})
    fake = _FakeClient([_brief_reply([120.0]), bad, _outline_reply("ep1"), INTRO, SOURCE, CLOSE])
    script = AnthropicScriptComposer(client=fake).write_script(_digest(EP1), "SOUL", "CTX")

    retry = _user(fake.messages.requests[2], 2)
    assert "first segment must be the intro" in retry and "last segment must be the close" in retry
    assert script.outline is not None and script.outline.segments[0].kind == "intro"


def test_outline_failing_twice_falls_back_to_the_required_structure() -> None:
    fake = _FakeClient([_brief_reply([120.0]), "nope", "still nope", INTRO, SOURCE, CLOSE])
    script = AnthropicScriptComposer(client=fake).write_script(_digest(EP1), "SOUL", "CTX")
    assert script.outline is not None
    assert [s.kind for s in script.outline.segments] == ["intro", "source", "close"]


def test_brief_failing_twice_falls_back_to_metadata() -> None:
    no_thesis = json.dumps({"people": [], "context": "x", "thesis": "", "key_points": []})
    fake = _FakeClient([no_thesis, no_thesis, _outline_reply("ep1"), INTRO, SOURCE, CLOSE])
    script = AnthropicScriptComposer(client=fake).write_script(_digest(EP1), "SOUL", "CTX")

    assert "thesis" in _user(fake.messages.requests[1], 2)
    assert script.briefs[0].context.startswith("Marc Andreessen, co-founder of a16z")


# --- R20 and the empty digest ---------------------------------------------------------


def test_no_grounded_line_with_highlights_raises_script_error() -> None:
    ungrounded = _lines(_line("Something vague."))
    fake = _FakeClient(
        [_brief_reply([120.0]), _outline_reply("ep1"), ungrounded, ungrounded, ungrounded]
    )
    with pytest.raises(ScriptError):
        AnthropicScriptComposer(client=fake).write_script(_digest(EP1), "SOUL", "CTX")


def test_script_error_is_terminal() -> None:
    assert issubclass(ScriptError, TerminalError)


def test_empty_digest_makes_no_calls() -> None:
    fake = _FakeClient([])
    refused = EpisodeDigest(episode_id="ep9", episode_title=None, highlights=[], refused=True)
    script = AnthropicScriptComposer(client=fake).write_script(_digest(refused), "SOUL", "CTX", TWO_HOST_PROFILE)

    assert script.turns == [] and script.takes == []
    assert "Nothing cleared the bar" in script.monologue
    assert fake.messages.requests == []


def test_segment_lines_are_capped() -> None:
    many = _lines(*[_line(f"Point {chr(97 + i)}.", ("ep1", 120.0)) for i in range(26)])
    fake = _FakeClient([_brief_reply([120.0]), _outline_reply("ep1"), INTRO, many, CLOSE])
    profile = EpisodeProfile(
        name="short",
        format="monologue",
        speakers=[SpeakerProfile(role="host", name="Host")],
        style=ConversationStyle(target_minutes=2),
    )
    script = AnthropicScriptComposer(client=fake).write_script(_digest(EP1), "SOUL", "CTX", profile)
    assert sum(1 for t in script.turns if t.text.startswith("Point")) < 26


# --- plan ---------------------------------------------------------------------------


def _ep(eid: str, score: float) -> EpisodeDigest:
    return EpisodeDigest(episode_id=eid, episode_title=eid, highlights=[_hl(eid, 10.0, "x", score)])


def test_plan_budgets_length_from_sources_and_caps_at_three() -> None:
    digest = _digest(_ep("a", 0.5), _ep("b", 0.9), _ep("c", 0.7), _ep("d", 0.6))
    plan = plan_episode(digest, MONOLOGUE_PROFILE)
    assert [e.episode_id for e in plan.featured] == ["b", "c", "d"]
    assert [e.episode_id for e in plan.also_noted] == ["a"]
    assert plan.target_minutes == 14


def test_plan_with_explicit_short_length_features_one_source() -> None:
    digest = _digest(_ep("a", 0.5), _ep("b", 0.9))
    profile = EpisodeProfile(
        name="five",
        format="monologue",
        speakers=[SpeakerProfile(role="host", name="Host")],
        style=ConversationStyle(target_minutes=5),
    )
    plan = plan_episode(digest, profile)
    assert [e.episode_id for e in plan.featured] == ["b"]
    assert plan.target_minutes == 5


# --- line validation (pure) --------------------------------------------------------


CTX = LineContext(
    speakers=frozenset({"host", "cohost"}),
    citable_ids=frozenset({"ep1"}),
    highlights={("ep1", 120): 120.0},
    vocabulary=vocabulary_of("Marc Andreessen 20VC Harry Stebbings"),
)


def _draft(text: str, *cites: tuple[str, float | None], speaker: str = "host") -> DraftLine:
    return DraftLine(speaker=speaker, text=text, cites=[DraftCite(episode_id=e, timestamp=t) for e, t in cites])


def test_validate_line_accepts_brief_and_highlight_citations() -> None:
    assert validate_line(_draft("Marc Andreessen was on 20VC in 2026.", ("ep1", None)), CTX) == []
    assert validate_line(_draft("Margins grew 40 percent.", ("ep1", 120.4)), CTX) == []


def test_validate_line_rejects_unknown_timestamp_source_and_speaker() -> None:
    assert validate_line(_draft("x", ("ep1", 999.0)), CTX)
    assert validate_line(_draft("x", ("ep7", None)), CTX)
    assert validate_line(_draft("x", speaker="narrator"), CTX)


def test_uncited_line_may_frame_but_not_state_numbers_or_new_names() -> None:
    assert validate_line(_draft("Here's where it breaks. Andreessen's case rests on one thing."), CTX) == []
    assert validate_line(_draft("That grew 40 percent."), CTX)
    problems = validate_line(_draft("This is exactly what Sam Altman warned about."), CTX)
    assert problems and "Sam" in problems[0]


def test_json_object_tolerates_fences_and_prose() -> None:
    assert json_object('Here you go:\n```json\n{"lines": []}\n```') == {"lines": []}
    with pytest.raises(ValueError):
        json_object("no object here")
