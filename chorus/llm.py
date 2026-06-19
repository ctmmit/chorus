"""LLM client interface + a deterministic mock + the real Anthropic client.

The scorer is `f(segment, soul, context) -> (score, reason)` (ENGINEERING_REVIEW
§9.1). Production uses Claude Haiku; with no ANTHROPIC_API_KEY we fall back to a
deterministic keyword-overlap MockLLMClient so the whole pipeline + path_test are
buildable and testable offline. The mock honors the soul's Attention Triggers /
Curation Guidance (positive signal) and Ignore / Anti-interests (negative),
which is what produces the validated two-soul divergence in tests.
"""
from __future__ import annotations

import logging
import os
import re
from functools import lru_cache
from typing import Protocol, runtime_checkable

log = logging.getLogger("chorus.llm")

_WORD_RE = re.compile(r"[a-z][a-z'\-]{2,}")
_HEADER_RE = re.compile(r"^#{1,6}\s*(.+?)\s*$", re.MULTILINE)
_STOP = frozenset(
    """the a an and or of to in for on with that this it is are be as at by from into
    you your they their our we i not but if then so what when which who whom how why
    only over under more most less than thing things something someone when where
    just like about across very real own kind sort one two get got make made go
    its his her them he she him do does did has have had will would can could should""".split()
)


def _keywords(text: str) -> set[str]:
    return {w for w in _WORD_RE.findall(text.lower()) if w not in _STOP}


def _section(markdown: str, *needles: str) -> str:
    """Return the text under any header whose title contains a needle, up to the
    next header. Case-insensitive."""
    out: list[str] = []
    headers = list(_HEADER_RE.finditer(markdown))
    for i, h in enumerate(headers):
        title = h.group(1).lower()
        if any(n in title for n in needles):
            start = h.end()
            end = headers[i + 1].start() if i + 1 < len(headers) else len(markdown)
            out.append(markdown[start:end])
    return "\n".join(out)


@runtime_checkable
class LLMClient(Protocol):
    def score_segment(self, text: str, soul: str, context: str) -> tuple[float, str]: ...


# Tuned so on-topic windows clear curation's threshold and off-topic ones do not.
_POS_WEIGHT = 0.16
_NEG_WEIGHT = 0.22


@lru_cache(maxsize=16)
def _signal(soul: str, context: str) -> tuple[frozenset[str], frozenset[str]]:
    positive = _keywords(
        _section(soul, "attention", "curation guidance", "core interests", "identity")
    ) | _keywords(context)
    negative = _keywords(_section(soul, "ignore", "anti-interest", "anti interest", "penalize"))
    positive -= negative
    return frozenset(positive), frozenset(negative)


class MockLLMClient:
    """Deterministic stand-in for Haiku. Same interface, no network."""

    def score_segment(self, text: str, soul: str, context: str) -> tuple[float, str]:
        positive, negative = _signal(soul, context)
        toks = _keywords(text)
        hits = len(toks & positive)
        anti = len(toks & negative)
        score = max(0.0, min(1.0, _POS_WEIGHT * hits - _NEG_WEIGHT * anti))
        reason = f"{hits} attention-trigger match(es)" + (f"; {anti} ignore-signal" if anti else "")
        return score, reason


class AnthropicLLMClient:
    """Real per-segment scorer (Claude Haiku). Activated when a key is present.
    Not exercised offline; the mock covers tests until a key is provided."""

    MODEL = "claude-haiku-4-5-20251001"

    def __init__(self, api_key: str) -> None:
        import anthropic  # type: ignore[import-not-found]  # optional dep; only when a key is used

        self._client = anthropic.Anthropic(api_key=api_key)

    def score_segment(self, text: str, soul: str, context: str) -> tuple[float, str]:
        prompt = (
            "You score how strongly a podcast segment matches a listener's lens.\n"
            f"LENS (soul):\n{soul}\n\nPRINCIPAL CONTEXT:\n{context}\n\n"
            f"SEGMENT:\n{text}\n\n"
            "Reply with a line 'SCORE: <0.0-1.0>' then 'REASON: <one line>'. "
            "Honor the lens's Curation Guidance; be strict."
        )
        msg = self._client.messages.create(
            model=self.MODEL,
            max_tokens=120,
            messages=[{"role": "user", "content": prompt}],
        )
        body = "".join(block.text for block in msg.content if block.type == "text")
        score_match = re.search(r"SCORE:\s*([01](?:\.\d+)?)", body)
        reason_match = re.search(r"REASON:\s*(.+)", body)
        score = float(score_match.group(1)) if score_match else 0.0
        reason = reason_match.group(1).strip() if reason_match else "no reason"
        return max(0.0, min(1.0, score)), reason


def get_llm_client() -> LLMClient:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if key:
        log.info("llm: using AnthropicLLMClient")
        return AnthropicLLMClient(key)
    log.warning("llm: ANTHROPIC_API_KEY absent — using MockLLMClient (offline, deterministic)")
    return MockLLMClient()
