"""AnthropicScriptComposer: request/parse behavior exercised against a fake
Anthropic client (same injectable-client pattern as tests/test_llm_batch.py),
so no key and no network are needed.

Covers: TAKE parsing (pass 1, unchanged), TURN parsing (pass 2, dialogue),
ungrounded turns dropped, malformed timestamps skipped (never raised), the
turn cap, and that pass-1 takes still parse correctly ahead of pass 2.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from chorus.models import (
    ConversationStyle,
    Digest,
    EpisodeDigest,
    EpisodeProfile,
    Highlight,
    MONOLOGUE_PROFILE,
    SpeakerProfile,
    TWO_HOST_PROFILE,
)
import pytest

from chorus.errors import TerminalError
from chorus.script import AnthropicScriptComposer, ScriptError, _max_turns


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


def _digest(*highlights: Highlight) -> Digest:
    return Digest(
        soul_version="deadbeef",
        episodes=[EpisodeDigest(episode_id="ep1", episode_title="Ep One", highlights=list(highlights))],
    )


HL = Highlight(
    episode_id="ep1",
    episode_title="Ep One",
    segment_timestamp=120.0,
    quote="margins are expanding",
    relevance_score=0.9,
    why_surface="on-thesis",
)
HL2 = Highlight(
    episode_id="ep1",
    episode_title="Ep One",
    segment_timestamp=300.0,
    quote="the moat is real",
    relevance_score=0.8,
    why_surface="on-thesis",
)


# --- pass 1: takes (unchanged behavior, still exercised ahead of pass 2) ---


def test_take_parsing_still_works_for_monologue() -> None:
    digest = _digest(HL)
    fake = _FakeClient(["TAKE|idea|ep1|120|Margins are expanding, and that matters."])
    composer = AnthropicScriptComposer(client=fake)

    script = composer.write_script(digest, "SOUL", "CTX", MONOLOGUE_PROFILE)

    assert script.format == "monologue"
    assert len(script.takes) == 1
    assert script.takes[0].episode_id == "ep1"
    assert script.takes[0].segment_timestamp == 120.0
    assert script.turns == []
    assert len(fake.messages.requests) == 1  # pass 2 never runs for monologue


def test_no_profile_defaults_to_monologue_like_before() -> None:
    digest = _digest(HL)
    fake = _FakeClient(["TAKE|idea|ep1|120|Margins are expanding."])
    script = AnthropicScriptComposer(client=fake).write_script(digest, "SOUL", "CTX")
    assert script.format == "monologue"
    assert script.turns == []


# --- pass 2: dialogue turns -------------------------------------------------


def test_dialogue_turn_parsing() -> None:
    digest = _digest(HL, HL2)
    take_reply = "\n".join(
        [
            "TAKE|idea|ep1|120|Margins are expanding.",
            "TAKE|pushback|ep1|300|The moat is real.",
        ]
    )
    turn_reply = "\n".join(
        [
            "TURN|host|ep1|120|Margins are expanding, and that's the whole thesis.",
            "TURN|cohost|ep1|120|Sure, but where's the number?",
            "TURN|host|ep1|300|The moat is real too.",
            "TURN|cohost|ep1|300|Fine, I'll grant that one.",
        ]
    )
    fake = _FakeClient([take_reply, turn_reply])
    composer = AnthropicScriptComposer(client=fake)

    script = composer.write_script(digest, "SOUL", "CTX", TWO_HOST_PROFILE)

    assert script.format == "dialogue"
    assert len(script.turns) == 4
    assert [t.speaker for t in script.turns] == ["host", "cohost", "host", "cohost"]
    assert script.turns[0].episode_id == "ep1"
    assert script.turns[0].segment_timestamp == 120.0
    # readable transcript built from turns
    assert "HOST: Margins are expanding, and that's the whole thesis." in script.monologue
    assert "COHOST: Sure, but where's the number?" in script.monologue
    # pass-2 prompt carries both personas and the style block
    turn_prompt = fake.messages.requests[1]["messages"][0]["content"]
    assert "skeptical foil" in turn_prompt
    assert "interruptions" in turn_prompt


def test_ungrounded_turns_are_dropped() -> None:
    digest = _digest(HL)  # only one real highlight
    take_reply = "TAKE|idea|ep1|120|Margins are expanding."
    turn_reply = "\n".join(
        [
            "TURN|host|ep1|120|Margins are expanding.",
            "TURN|cohost|ep1|999|This timestamp does not exist as a highlight.",
            "TURN|host|other-episode|120|Wrong episode id entirely.",
        ]
    )
    fake = _FakeClient([take_reply, turn_reply])
    script = AnthropicScriptComposer(client=fake).write_script(digest, "SOUL", "CTX", TWO_HOST_PROFILE)

    assert len(script.turns) == 1
    assert script.turns[0].text == "Margins are expanding."


def test_malformed_timestamp_is_skipped_not_raised() -> None:
    digest = _digest(HL)
    take_reply = "TAKE|idea|ep1|120|Margins are expanding."
    turn_reply = "\n".join(
        [
            "TURN|host|ep1|not-a-number|This line has a bad timestamp.",
            "TURN|cohost|ep1|120|But this one is fine.",
        ]
    )
    fake = _FakeClient([take_reply, turn_reply])

    script = AnthropicScriptComposer(client=fake).write_script(digest, "SOUL", "CTX", TWO_HOST_PROFILE)

    assert len(script.turns) == 1
    assert script.turns[0].text == "But this one is fine."


def test_turn_cap_enforced() -> None:
    digest = _digest(HL)
    take_reply = "TAKE|idea|ep1|120|Margins are expanding."
    style = ConversationStyle(tone="fast", engagement=[], target_minutes=1)
    profile = EpisodeProfile(
        name="capped",
        format="dialogue",
        speakers=[
            SpeakerProfile(role="host", name="Host"),
            SpeakerProfile(role="cohost", name="Cohost", persona="foil"),
        ],
        style=style,
    )
    cap = _max_turns(1)
    assert cap < 20  # sanity: 1 minute really does cap well below "a lot"
    # Offer far more grounded turns than the cap allows.
    turn_reply = "\n".join(f"TURN|host|ep1|120|Turn number {i}." for i in range(cap + 10))
    fake = _FakeClient([take_reply, turn_reply])

    script = AnthropicScriptComposer(client=fake).write_script(digest, "SOUL", "CTX", profile)

    assert len(script.turns) == cap


# --- R20: zero grounded takes with highlights present is an error ---------


def test_zero_grounded_takes_with_highlights_raises_script_error() -> None:
    # The digest HAS grounded material (HL), but the model's reply is empty/
    # unparseable/ungrounded -> zero takes survive. That must be an explicit
    # failure (ScriptError), not a false "Nothing cleared the bar." digest.
    digest = _digest(HL)
    fake = _FakeClient(["the model said something that doesn't match the TAKE format at all"])
    composer = AnthropicScriptComposer(client=fake)

    with pytest.raises(ScriptError):
        composer.write_script(digest, "SOUL", "CTX", MONOLOGUE_PROFILE)


def test_script_error_is_terminal() -> None:
    assert issubclass(ScriptError, TerminalError)


def test_pass_two_never_runs_when_pass_one_yields_no_takes() -> None:
    digest = _digest()  # no highlights at all -> refused, no takes
    fake = _FakeClient(["Nothing to say here."])
    script = AnthropicScriptComposer(client=fake).write_script(digest, "SOUL", "CTX", TWO_HOST_PROFILE)

    assert script.takes == []
    assert script.turns == []
    assert len(fake.messages.requests) == 1  # only the pass-1 call happened
