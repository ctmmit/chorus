"""Soul bootstrap — source-agnostic (DESIGN_DOC ladder, ENGINEERING_REVIEW Q6).

The service only ever consumes a `soul.md`; HOW the caller builds one is a
pluggable, caller-side concern. This module provides the two non-trivial tiers
as recipes the caller (agent) can run:

  Tier 1  derive_from_corpus(texts)   — summarize "what the principal reads/saves"
          into the soul schema. Readwise is ONE adapter (just pass its text);
          Obsidian / notes / a raw dump are others. None is privileged.
  Tier 2  build_from_interview(answers) — no corpus needed; the universal fallback.

Both emit the same schema (Identity, Core Interests, Attention Triggers,
Anti-interests, Taste & Sensibility, Curation Guidance) so curation treats them
identically. Mock impl is deterministic; the Anthropic impl writes a richer soul
when a key is present.
"""
from __future__ import annotations

import logging
import os
from collections import Counter
from typing import Protocol, runtime_checkable

from chorus.llm import _keywords  # reuse the same tokenizer the scorer uses

log = logging.getLogger("chorus.bootstrap")

_TEMPLATE = """# Soul ({origin})

## Identity & Role
{identity}

## Core Interests
{interests}

## Attention Triggers
{triggers}

## Anti-interests
{anti}

## Taste & Sensibility
{taste}

## Curation Guidance
{guidance}
"""


def _render(
    origin: str,
    identity: str,
    interests: list[str],
    triggers: list[str],
    anti: list[str],
    taste: str,
    guidance: str,
) -> str:
    def bullets(items: list[str]) -> str:
        return "\n".join(f"- {i}" for i in items) if items else "- (none inferred)"

    return _TEMPLATE.format(
        origin=origin,
        identity=identity,
        interests=bullets(interests),
        triggers=bullets(triggers),
        anti=bullets(anti),
        taste=taste,
        guidance=guidance,
    )


@runtime_checkable
class SoulBuilder(Protocol):
    def derive_from_corpus(self, texts: list[str]) -> str: ...
    def build_from_interview(self, answers: dict[str, str]) -> str: ...


class MockSoulBuilder:
    """Deterministic. Tier 1 extracts the most frequent corpus keywords into the
    Attention Triggers / Core Interests; Tier 2 fills the template from answers."""

    def derive_from_corpus(self, texts: list[str], top: int = 12) -> str:
        counter: Counter[str] = Counter()
        for t in texts:
            counter.update(_keywords(t))
        top_terms = [w for w, _ in counter.most_common(top)]
        return _render(
            origin="derived:corpus",
            identity="Derived from the principal's recent reading/listening corpus.",
            interests=top_terms[:6],
            triggers=top_terms,
            anti=[],
            taste="Inferred from what the principal actually saves.",
            guidance=(
                "Surface segments matching the Core Interests / Attention Triggers above. "
                "The bar is high for material outside them; refuse rather than pad."
            ),
        )

    def build_from_interview(self, answers: dict[str, str]) -> str:
        def split(key: str) -> list[str]:
            return [s.strip() for s in answers.get(key, "").split(",") if s.strip()]

        return _render(
            origin="interview",
            identity=answers.get("identity", "Stated by the principal."),
            interests=split("interests"),
            triggers=split("triggers") or split("interests"),
            anti=split("ignore"),
            taste=answers.get("style", "As stated."),
            guidance=answers.get(
                "guidance",
                "Surface segments matching the triggers; high bar for everything else.",
            ),
        )


class AnthropicSoulBuilder:
    """Real soul writer (Claude). Activated when a key is present; not exercised
    offline. Falls back to the mock's structure if the model output is unusable."""

    MODEL = "claude-sonnet-4-6"

    def __init__(self, api_key: str) -> None:
        import anthropic  # type: ignore[import-not-found]  # optional dep

        self._client = anthropic.Anthropic(api_key=api_key)
        self._fallback = MockSoulBuilder()

    def _write(self, source_label: str, material: str) -> str:
        prompt = (
            "Write a `soul.md` (a reader/listener lens) from the material below. "
            "Use these exact sections: Identity & Role, Core Interests, Attention "
            "Triggers, Anti-interests, Taste & Sensibility, Curation Guidance. "
            "Curation Guidance must give explicit scoring calibration.\n\n"
            f"SOURCE ({source_label}):\n{material}"
        )
        msg = self._client.messages.create(
            model=self.MODEL, max_tokens=900, messages=[{"role": "user", "content": prompt}]
        )
        body = "".join(b.text for b in msg.content if b.type == "text")
        return body if "Curation Guidance" in body else ""

    def derive_from_corpus(self, texts: list[str]) -> str:
        out = self._write("corpus", "\n\n".join(texts)[:8000])
        return out or self._fallback.derive_from_corpus(texts)

    def build_from_interview(self, answers: dict[str, str]) -> str:
        material = "\n".join(f"{k}: {v}" for k, v in answers.items())
        out = self._write("interview", material)
        return out or self._fallback.build_from_interview(answers)


def get_soul_builder() -> SoulBuilder:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if key:
        return AnthropicSoulBuilder(key)
    log.warning("bootstrap: ANTHROPIC_API_KEY absent — using MockSoulBuilder")
    return MockSoulBuilder()
