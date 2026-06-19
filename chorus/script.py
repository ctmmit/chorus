"""Script synthesis: compose the agent's opinionated script from the digest.

Every take must be traceable to a surfaced highlight (no ungrounded opinions) —
ENGINEERING_REVIEW §9.2. The mock guarantees traceability by construction; the
real Sonnet composer is instructed to tag each take with its highlight and we
keep only takes that reference a real one.
"""
from __future__ import annotations

import logging
import os
import re
from typing import Protocol, runtime_checkable

from chorus.models import TAKE_TYPES, Digest, Script, Take

log = logging.getLogger("chorus.script")


def _ts(seconds: float) -> str:
    return f"{int(seconds) // 60}:{int(seconds) % 60:02d}"


@runtime_checkable
class ScriptComposer(Protocol):
    def write_script(self, digest: Digest, soul: str, context: str) -> Script: ...


class MockScriptComposer:
    """Deterministic: one take per highlight, take_type cycled, monologue built
    from the takes. Traceable by construction."""

    def write_script(self, digest: Digest, soul: str, context: str) -> Script:
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
        monologue = (
            "\n\n".join(t.text for t in takes)
            if takes
            else "Nothing cleared the bar this week."
        )
        return Script(soul_version=digest.soul_version, takes=takes, monologue=monologue)


class AnthropicScriptComposer:
    """Real opinionated script (Claude Sonnet). Activated when a key is present."""

    MODEL = "claude-sonnet-4-6"

    def __init__(self, api_key: str) -> None:
        import anthropic  # type: ignore[import-not-found]  # optional dep

        self._client = anthropic.Anthropic(api_key=api_key)

    def write_script(self, digest: Digest, soul: str, context: str) -> Script:
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
        msg = self._client.messages.create(
            model=self.MODEL,
            max_tokens=1500,
            messages=[{"role": "user", "content": prompt}],
        )
        body = "".join(b.text for b in msg.content if b.type == "text")
        valid = {(h.episode_id, round(h.segment_timestamp)) for h in digest.highlights}
        takes: list[Take] = []
        for line in body.splitlines():
            m = re.match(r"TAKE\|([^|]+)\|([^|]+)\|([0-9.]+)\|(.+)", line.strip())
            if not m:
                continue
            tt, eid, ts, text = m.group(1).strip(), m.group(2).strip(), float(m.group(3)), m.group(4)
            if tt in TAKE_TYPES and (eid, round(ts)) in valid:  # drop ungrounded takes
                takes.append(Take(text=text, take_type=tt, episode_id=eid, segment_timestamp=ts))
        monologue = "\n\n".join(t.text for t in takes) if takes else "Nothing cleared the bar."
        return Script(soul_version=digest.soul_version, takes=takes, monologue=monologue)


def get_script_composer() -> ScriptComposer:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if key:
        return AnthropicScriptComposer(key)
    log.warning("script: ANTHROPIC_API_KEY absent — using MockScriptComposer")
    return MockScriptComposer()
