"""Script synthesis: compose the agent's opinionated script from the digest.

Every take must be traceable to a surfaced highlight (no ungrounded opinions) —
ENGINEERING_REVIEW §9.2. The mock guarantees traceability by construction; the
real Sonnet composer is instructed to tag each take with its highlight and we
keep only takes that reference a real one.

Phase E (docs/DEVELOPMENT_PLAN.md §4, two-host episodes): outline-then-dialogue.
Pass 1 (unchanged) produces the beats (`takes`) from the digest. Pass 2, only
when `profile.format == "dialogue"`, turns those beats into `turns` — the
agent still writes every line; the profile only supplies who's speaking and
how (personas, tone, engagement techniques), never an auto-writer. Grounding
is per-turn: a turn that does not resolve to a surfaced highlight is dropped,
exactly as an ungrounded take is dropped today. Single-voice (no profile, or
`profile.format == "monologue"`) is unchanged from before this phase.
"""
from __future__ import annotations

import logging
import os
import re
from typing import Any, Literal, Protocol, runtime_checkable

from chorus.models import (
    HOST_PERSONA_IS_SOUL,
    TAKE_TYPES,
    ConversationStyle,
    Digest,
    EpisodeProfile,
    MONOLOGUE_PROFILE,
    Script,
    SpeakerProfile,
    Take,
    Turn,
)

log = logging.getLogger("chorus.script")

# Turn cap sizing (spec: ~150 spoken words/minute, ~35 words/turn) so a long
# target_minutes can't make pass 2 write an unbounded dialogue.
SPOKEN_WORDS_PER_MINUTE = 150
WORDS_PER_TURN = 35


def _ts(seconds: float) -> str:
    return f"{int(seconds) // 60}:{int(seconds) % 60:02d}"


def _max_turns(target_minutes: int) -> int:
    """~150 wpm / ~35 words-per-turn, so a 5-minute episode caps at ~21 turns."""
    return max(1, round(target_minutes * SPOKEN_WORDS_PER_MINUTE / WORDS_PER_TURN))


def _speaker(profile: EpisodeProfile, role: str) -> SpeakerProfile:
    for s in profile.speakers:
        if s.role == role:
            return s
    raise ValueError(f"profile {profile.name!r} has no {role!r} speaker")


def _speaker_persona(speaker: SpeakerProfile, soul: str) -> str:
    """The host's persona defaults to the sentinel "the soul" (see
    chorus/models.py): resolve it against the request's actual soul text."""
    return soul if speaker.persona == HOST_PERSONA_IS_SOUL else speaker.persona


def _style_block(style: ConversationStyle) -> str:
    engagement = ", ".join(style.engagement) if style.engagement else "none specified"
    return (
        f"Tone: {style.tone or 'unspecified'}\n"
        f"Engagement techniques to use: {engagement}\n"
        f"Target length: ~{style.target_minutes} minute(s)"
    )


def _turns_transcript(turns: list[Turn]) -> str:
    """The readable transcript for a dialogue script — what `monologue` holds
    in dialogue format (spec: "HOST: ...\\n\\nCOHOST: ...")."""
    if not turns:
        return "Nothing cleared the bar this week."
    return "\n\n".join(f"{t.speaker.upper()}: {t.text}" for t in turns)


@runtime_checkable
class ScriptComposer(Protocol):
    def write_script(
        self, digest: Digest, soul: str, context: str, profile: EpisodeProfile | None = None
    ) -> Script: ...


class MockScriptComposer:
    """Deterministic: one take per highlight (pass 1), take_type cycled,
    traceable by construction. For dialogue profiles, pass 2 deterministically
    alternates host/cohost per beat: the host states the take, the cohost
    pushes back citing the SAME highlight (so every turn stays traceable)."""

    def write_script(
        self, digest: Digest, soul: str, context: str, profile: EpisodeProfile | None = None
    ) -> Script:
        profile = profile or MONOLOGUE_PROFILE
        takes = self._takes(digest)
        monologue_text = self._monologue(takes)

        if profile.format != "dialogue":
            return Script(
                soul_version=digest.soul_version,
                takes=takes,
                monologue=monologue_text,
                format=profile.format,
            )

        turns = self._turns(takes)
        return Script(
            soul_version=digest.soul_version,
            takes=takes,
            monologue=_turns_transcript(turns),
            turns=turns,
            format="dialogue",
        )

    @staticmethod
    def _takes(digest: Digest) -> list[Take]:
        takes: list[Take] = []
        for i, h in enumerate(digest.highlights):
            take_type = TAKE_TYPES[i % len(TAKE_TYPES)]
            label = h.episode_title or h.episode_id
            text = f"[{take_type}] On {label} at {_ts(h.segment_timestamp)} — {h.quote}"
            takes.append(
                Take(
                    text=text,
                    take_type=take_type,
                    episode_id=h.episode_id,
                    segment_timestamp=h.segment_timestamp,
                )
            )
        return takes

    @staticmethod
    def _monologue(takes: list[Take]) -> str:
        return (
            "\n\n".join(t.text for t in takes)
            if takes
            else "Nothing cleared the bar this week."
        )

    @staticmethod
    def _turns(takes: list[Take]) -> list[Turn]:
        turns: list[Turn] = []
        for t in takes:
            turns.append(
                Turn(speaker="host", text=t.text, episode_id=t.episode_id, segment_timestamp=t.segment_timestamp)
            )
            turns.append(
                Turn(
                    speaker="cohost",
                    text=f"Where's the number on that? [{t.take_type}] I'll push back: {t.text}",
                    episode_id=t.episode_id,
                    segment_timestamp=t.segment_timestamp,
                )
            )
        return turns


class AnthropicScriptComposer:
    """Real opinionated script (Claude Sonnet). Activated when a key is
    present. `client` is injectable so the parsing paths are unit-testable
    without a key (same pattern as chorus.llm.AnthropicLLMClient)."""

    MODEL = "claude-sonnet-4-6"
    TAKES_MAX_TOKENS = 1500
    TURNS_MAX_TOKENS = 2500

    def __init__(self, api_key: str | None = None, client: Any | None = None) -> None:
        if client is None:
            import anthropic  # type: ignore[import-not-found]  # optional dep; only when a key is used

            client = anthropic.Anthropic(api_key=api_key)
        self._client: Any = client

    def _create(self, prompt: str, max_tokens: int) -> str:
        msg = self._client.messages.create(
            model=self.MODEL,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        return "".join(b.text for b in msg.content if b.type == "text")

    def write_script(
        self, digest: Digest, soul: str, context: str, profile: EpisodeProfile | None = None
    ) -> Script:
        profile = profile or MONOLOGUE_PROFILE
        takes = self._write_takes(digest, soul, context)
        monologue_text = "\n\n".join(t.text for t in takes) if takes else "Nothing cleared the bar."

        if profile.format != "dialogue":
            return Script(
                soul_version=digest.soul_version,
                takes=takes,
                monologue=monologue_text,
                format=profile.format,
            )

        turns = self._write_turns(digest, soul, context, profile, takes)
        return Script(
            soul_version=digest.soul_version,
            takes=takes,
            monologue=_turns_transcript(turns),
            turns=turns,
            format="dialogue",
        )

    # -- pass 1: beats (unchanged from single-voice) ------------------------

    def _write_takes(self, digest: Digest, soul: str, context: str) -> list[Take]:
        hl_lines = "\n".join(
            f"- id={h.episode_id} ts={h.segment_timestamp} :: {h.quote}" for h in digest.highlights
        )
        prompt = (
            f"You are this lens:\n{soul}\n\nPRINCIPAL CONTEXT:\n{context}\n\n"
            "Write a short, opinionated single-voice podcast monologue. Take a "
            "position, push back, connect episodes. EVERY beat must be grounded in "
            "one of these highlights. Output one beat per line as "
            "'TAKE|<type>|<episode_id>|<ts>|<text>' where type is one of "
            f"{', '.join(TAKE_TYPES)}.\n\nHIGHLIGHTS:\n{hl_lines}"
        )
        body = self._create(prompt, self.TAKES_MAX_TOKENS)
        valid = {(h.episode_id, round(h.segment_timestamp)) for h in digest.highlights}
        takes: list[Take] = []
        for line in body.splitlines():
            m = re.match(r"TAKE\|([^|]+)\|([^|]+)\|([0-9.]+)\|(.+)", line.strip())
            if not m:
                continue
            tt, eid, ts_str, text = m.group(1).strip(), m.group(2).strip(), m.group(3), m.group(4)
            try:
                ts = float(ts_str)
            except ValueError:
                log.warning("script: take has unparseable timestamp %r; skipped", ts_str)
                continue
            if tt in TAKE_TYPES and (eid, round(ts)) in valid:  # drop ungrounded takes
                takes.append(Take(text=text, take_type=tt, episode_id=eid, segment_timestamp=ts))
            else:
                log.info("script: dropped ungrounded take (episode_id=%s ts=%s)", eid, ts)
        return takes

    # -- pass 2: beats -> dialogue turns (dialogue format only) -------------

    def _write_turns(
        self, digest: Digest, soul: str, context: str, profile: EpisodeProfile, takes: list[Take]
    ) -> list[Turn]:
        if not takes:
            return []
        max_turns = _max_turns(profile.style.target_minutes)
        prompt = self._turns_prompt(soul, context, profile, takes, max_turns)
        body = self._create(prompt, self.TURNS_MAX_TOKENS)
        valid = {(h.episode_id, round(h.segment_timestamp)) for h in digest.highlights}
        return _parse_turns(body, valid, max_turns)

    @staticmethod
    def _turns_prompt(
        soul: str, context: str, profile: EpisodeProfile, takes: list[Take], max_turns: int
    ) -> str:
        host = _speaker(profile, "host")
        cohost = _speaker(profile, "cohost")
        beats = "\n".join(
            f"- id={t.episode_id} ts={t.segment_timestamp} type={t.take_type} :: {t.text}" for t in takes
        )
        return (
            f"PRINCIPAL CONTEXT:\n{context}\n\n"
            f"HOST ({host.name}) persona:\n{_speaker_persona(host, soul)}\n\n"
            f"COHOST ({cohost.name}) persona:\n{_speaker_persona(cohost, soul)}\n\n"
            f"CONVERSATION STYLE:\n{_style_block(profile.style)}\n\n"
            "Turn these beats into a two-host dialogue: the host raises each beat in "
            "their own voice, the cohost reacts in theirs (push back, ask for the "
            "number, agree when it's warranted), honoring the conversation style "
            "above. EVERY turn must be grounded in one of the beats below — do not "
            f"introduce claims the beats don't support. Write at most {max_turns} "
            "turns total. Output one turn per line as "
            "'TURN|<host|cohost>|<episode_id>|<ts>|<text>'.\n\n"
            f"BEATS:\n{beats}"
        )


_TURN_RE = re.compile(r"TURN\|(host|cohost)\|([^|]+)\|([^|]+)\|(.+)")


def _parse_turns(body: str, valid: set[tuple[str, int]], max_turns: int) -> list[Turn]:
    """Parse 'TURN|<host|cohost>|<episode_id>|<ts>|<text>' lines. Ungrounded
    turns are dropped (logged); a malformed timestamp is skipped (logged),
    never raised — the whole reply must never fail the job over one bad line."""
    turns: list[Turn] = []
    for line in body.splitlines():
        m = _TURN_RE.match(line.strip())
        if not m:
            continue
        # _TURN_RE's first group only ever matches "host" or "cohost", but
        # that's not visible to mypy from a regex match — narrow explicitly.
        speaker: Literal["host", "cohost"] = "host" if m.group(1) == "host" else "cohost"
        eid, ts_str, text = m.group(2).strip(), m.group(3).strip(), m.group(4)
        try:
            ts = float(ts_str)
        except ValueError:
            log.warning("script: dialogue turn has unparseable timestamp %r; skipped", ts_str)
            continue
        if (eid, round(ts)) not in valid:
            log.info("script: dropped ungrounded dialogue turn (episode_id=%s ts=%s)", eid, ts)
            continue
        turns.append(Turn(speaker=speaker, text=text, episode_id=eid, segment_timestamp=ts))
        if len(turns) >= max_turns:
            break
    return turns


def get_script_composer() -> ScriptComposer:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if key:
        return AnthropicScriptComposer(key)
    log.warning("script: ANTHROPIC_API_KEY absent — using MockScriptComposer")
    return MockScriptComposer()
