"""LLM client interface + a deterministic mock + the real Anthropic client.

The scorer is `f(windows, soul, context) -> [(score, reason), ...]` — one call
per EPISODE, not one per window (ENGINEERING_REVIEW §9.1 revised). The soul +
context go in a cached system block, so a five-episode digest costs a handful
of calls instead of hundreds and returns in seconds instead of minutes.
`score_segment` remains for single-window callers.

A window's result may carry a third element: a short verbatim excerpt naming
the span that earned the score. It is a pointer, not a quote: curation uses it
only if it occurs verbatim in that window's text, and otherwise cuts the quote
itself (chorus.curation._cite).

Production uses Claude Haiku; with no ANTHROPIC_API_KEY we fall back to a
deterministic keyword-overlap MockLLMClient so the whole pipeline + path_test
are buildable and testable offline. The mock honors the soul's Attention
Triggers / Core Interests (positive signal) and Ignore / Anti-interests
(negative), which is what produces the validated two-soul divergence in tests.
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Protocol, runtime_checkable

log = logging.getLogger("chorus.llm")

_WORD_RE = re.compile(r"[a-z][a-z'\-]{2,}")
_HEADER_RE = re.compile(r"^#{1,6}\s*(.+?)\s*$", re.MULTILINE)
_STOP = frozenset(
    ["the", "a", "an", "and", "or", "of", "to", "in", "for", "on", "with", "that", "this", "it", "is", "are", "be", "as", "at", "by", "from", "into", "you", "your", "they", "their", "our", "we", "i", "not", "but", "if", "then", "so", "what", "when", "which", "who", "whom", "how", "why", "only", "over", "under", "more", "most", "less", "than", "thing", "things", "something", "someone", "when", "where", "just", "like", "about", "across", "very", "real", "own", "kind", "sort", "one", "two", "get", "got", "make", "made", "go", "its", "his", "her", "them", "he", "she", "him", "do", "does", "did", "has", "have", "had", "will", "would", "can", "could", "should"]
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


Scored = tuple[float, str]
# (score, reason) or (score, reason, excerpt). The excerpt is optional so
# scorers that cannot point at a span (and older test doubles) still fit.
ScoredWindow = tuple[float, str] | tuple[float, str, str]


class LLMError(Exception):
    """The model returned something we could not turn into scores. Explicit,
    so the job ends `failed` with this reason rather than a silent zero digest."""


@runtime_checkable
class LLMClient(Protocol):
    def score_segment(self, text: str, soul: str, context: str) -> Scored: ...

    def score_windows(
        self, windows: list[str], soul: str, context: str, meter: TokenUsage | None = None
    ) -> list[ScoredWindow]:
        """`meter`, when given, accumulates this call's token spend (R14) —
        callers that need PER-JOB usage (chorus.pipeline.stage_curate_episode)
        pass a fresh TokenUsage() per episode rather than reading a
        client-level counter shared across concurrent jobs."""
        ...


@dataclass
class TokenUsage:
    """Accumulated over a client's lifetime; the pipeline snapshots it per job."""

    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    def add(self, usage: Any) -> None:
        self.calls += 1
        self.input_tokens += int(getattr(usage, "input_tokens", 0) or 0)
        self.output_tokens += int(getattr(usage, "output_tokens", 0) or 0)
        self.cache_read_tokens += int(getattr(usage, "cache_read_input_tokens", 0) or 0)
        self.cache_write_tokens += int(getattr(usage, "cache_creation_input_tokens", 0) or 0)


# Tuned so on-topic windows clear curation's threshold and off-topic ones do not.
# Negative weight is high so each soul's Ignore list actively demotes the other
# lens's territory — that is what drives two-soul divergence.
_POS_WEIGHT = 0.16
_NEG_WEIGHT = 0.34
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")


@lru_cache(maxsize=16)
def _signal(soul: str, context: str) -> tuple[frozenset[str], frozenset[str]]:
    # Topic signal only — Attention Triggers + Core Interests. Identity / Curation
    # Guidance are prose (boilerplate in derived souls) and would pollute scoring.
    positive = _keywords(_section(soul, "attention", "core interests")) | _keywords(context)
    negative = _keywords(_section(soul, "ignore", "anti-interest", "anti interest", "penalize"))
    positive -= negative
    return frozenset(positive), frozenset(negative)


def _net_hits(text: str, positive: frozenset[str], negative: frozenset[str]) -> tuple[int, int]:
    toks = _keywords(text)
    return len(toks & positive), len(toks & negative)


class MockLLMClient:
    """Deterministic stand-in for Haiku. Same interface, no network."""

    def score_segment(self, text: str, soul: str, context: str) -> Scored:
        hits, anti = _net_hits(text, *_signal(soul, context))
        score = max(0.0, min(1.0, _POS_WEIGHT * hits - _NEG_WEIGHT * anti))
        reason = f"{hits} attention-trigger match(es)" + (f"; {anti} ignore-signal" if anti else "")
        return score, reason

    def excerpt(self, text: str, soul: str, context: str) -> str | None:
        """The window's sentence with the most net attention-trigger matches
        (earliest wins a tie), or None when no sentence nets a match. Verbatim
        by construction; curation still verifies it like any model's excerpt."""
        positive, negative = _signal(soul, context)
        best: tuple[int, str] | None = None
        for sentence in _SENTENCE_RE.split(text.strip()):
            hits, anti = _net_hits(sentence, positive, negative)
            net = hits - anti
            if net > 0 and (best is None or net > best[0]):
                best = (net, sentence)
        return best[1] if best else None

    def score_windows(
        self, windows: list[str], soul: str, context: str, meter: TokenUsage | None = None
    ) -> list[ScoredWindow]:
        # The mock makes no real model calls, so there is nothing to meter;
        # `meter` is accepted (and left untouched) purely for Protocol parity.
        out: list[ScoredWindow] = []
        for w in windows:
            score, reason = self.score_segment(w, soul, context)
            excerpt = self.excerpt(w, soul, context)
            out.append((score, reason, excerpt) if excerpt else (score, reason))
        return out


# Batch sizing: ~40 windows is an hour of audio; keeps the JSON reply well under
# max_tokens and the input well under Haiku's context.
BATCH_WINDOWS = 40
BATCH_MAX_TOKENS = 4_000
SINGLE_MAX_TOKENS = 120
# Excerpts are requested only for windows at or above this score, which keeps
# the batch reply inside BATCH_MAX_TOKENS. Mirrors
# chorus.curation.RELEVANCE_THRESHOLD (a test pins the two together; curation
# imports this module, so it cannot import that constant here).
EXCERPT_SCORE_FLOOR = 0.35
UNSCORED_REASON = "not scored by model"
_JSON_ARRAY_RE = re.compile(r"\[.*\]", re.DOTALL)


class AnthropicLLMClient:
    """Real scorer (Claude Haiku). Activated when a key is present.

    `client` is injectable so the parsing paths are unit-testable without a key.
    The system block (soul + context) is marked for prompt caching: it is
    identical across every batch in a job, so every call after the first reads
    it from cache at a fraction of the price.
    """

    MODEL = "claude-haiku-4-5-20251001"

    def __init__(self, api_key: str | None = None, client: Any | None = None) -> None:
        if client is None:
            import anthropic  # type: ignore[import-not-found]  # optional dep; only when a key is used

            client = anthropic.Anthropic(api_key=api_key)
        self._client: Any = client
        self.usage = TokenUsage()

    # -- prompt pieces -------------------------------------------------------

    @staticmethod
    def _system(soul: str, context: str) -> list[dict[str, Any]]:
        text = (
            "You score how strongly podcast transcript windows match a listener's lens. "
            "Honor the lens's Curation Guidance; be strict; a score of 1.0 means the window "
            "is exactly what this listener wants surfaced, 0.0 means irrelevant.\n\n"
            f"LENS (soul):\n{soul}\n\nPRINCIPAL CONTEXT:\n{context}"
        )
        return [{"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}]

    def _create(
        self, system: list[dict[str, Any]], user: str, max_tokens: int, meter: TokenUsage | None = None
    ) -> str:
        msg = self._client.messages.create(
            model=self.MODEL,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        usage = getattr(msg, "usage", None)
        # Client-level self.usage is kept for backward compatibility (existing
        # callers/tests read it); `meter`, when given, gets the SAME call's
        # usage added so a caller can isolate one job's spend (R14) instead of
        # snapshotting/deltaing this shared, cross-job counter.
        self.usage.add(usage)
        if meter is not None:
            meter.add(usage)
        return "".join(block.text for block in msg.content if block.type == "text")

    # -- single window (kept for callers that score one span) ----------------

    def score_segment(self, text: str, soul: str, context: str) -> Scored:
        body = self._create(
            self._system(soul, context),
            f"SEGMENT:\n{text}\n\nReply with a line 'SCORE: <0.0-1.0>' then 'REASON: <one line>'.",
            SINGLE_MAX_TOKENS,
        )
        score_match = re.search(r"SCORE:\s*([01](?:\.\d+)?)", body)
        reason_match = re.search(r"REASON:\s*(.+)", body)
        score = float(score_match.group(1)) if score_match else 0.0
        reason = reason_match.group(1).strip() if reason_match else "no reason"
        return max(0.0, min(1.0, score)), reason

    # -- batch: one call per ~40 windows ------------------------------------

    def score_windows(
        self, windows: list[str], soul: str, context: str, meter: TokenUsage | None = None
    ) -> list[ScoredWindow]:
        system = self._system(soul, context)
        out: list[ScoredWindow] = []
        for start in range(0, len(windows), BATCH_WINDOWS):
            chunk = windows[start : start + BATCH_WINDOWS]
            out.extend(self._score_batch(system, chunk, meter))
        return out

    def _score_batch(
        self, system: list[dict[str, Any]], chunk: list[str], meter: TokenUsage | None = None
    ) -> list[ScoredWindow]:
        listing = "\n\n".join(f"[{i}]\n{text}" for i, text in enumerate(chunk))
        user = (
            f"Score each of the {len(chunk)} windows below. Reply with ONLY a JSON array, one "
            'object per window, in order: [{"i": <index>, "score": <0.0-1.0>, "reason": '
            '"<one line>", "excerpt": "<see below>"}, ...]. Include every index exactly once.\n'
            f"For each window you score {EXCERPT_SCORE_FLOOR} or higher, set \"excerpt\" to the "
            "single most relevant span of that window: one sentence or clause, 6 to 28 words, "
            "copied character for character from the window text. Do not paraphrase, fix or "
            "join separate passages; an excerpt not found verbatim in the window is discarded. "
            "Omit \"excerpt\" for every other window.\n\n"
            f"WINDOWS:\n{listing}"
        )
        body = self._create(system, user, BATCH_MAX_TOKENS, meter)
        try:
            return _parse_batch(body, len(chunk))
        except LLMError as first:
            log.warning("llm: unparseable batch reply, retrying once (%s)", first)
            body = self._create(system, user, BATCH_MAX_TOKENS, meter)
            return _parse_batch(body, len(chunk))


def _reject_non_finite_constant(token: str) -> float:
    """`json.loads`'s `parse_constant` hook (R25): Python's decoder otherwise
    happily accepts the non-standard `NaN`/`Infinity`/`-Infinity` tokens and
    hands back a non-finite float, which a naive min/max clamp can then
    promote to a boundary score (0.0 or 1.0) instead of rejecting."""
    raise ValueError(f"non-finite JSON constant in model reply: {token}")


def _parse_batch(body: str, n: int) -> list[ScoredWindow]:
    match = _JSON_ARRAY_RE.search(body)
    if not match:
        raise LLMError("no JSON array in model reply")
    try:
        items = json.loads(match.group(0), parse_constant=_reject_non_finite_constant)
    except (json.JSONDecodeError, ValueError) as err:
        raise LLMError(f"invalid JSON in model reply: {err}") from err
    if not isinstance(items, list):
        raise LLMError("model reply JSON is not an array")

    scored: list[ScoredWindow | None] = [None] * n
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            i = int(item["i"])
            score = float(item["score"])
        except (KeyError, TypeError, ValueError):
            continue
        # A JSON number literal (not the special NaN/Infinity constants
        # parse_constant already rejects above) can still overflow float()
        # to +/-inf (e.g. "1e400"). Treated exactly like any other malformed
        # item: skipped here, folded into the "missing" count below, and
        # subject to the same retry-once path as any other parse failure.
        if not math.isfinite(score):
            continue
        if 0 <= i < n and scored[i] is None:
            reason = str(item.get("reason") or "no reason").strip()
            clamped = max(0.0, min(1.0, score))
            # Only a candidate: curation decides whether it is verbatim.
            excerpt = item.get("excerpt")
            if isinstance(excerpt, str) and excerpt.strip():
                scored[i] = (clamped, reason, excerpt)
            else:
                scored[i] = (clamped, reason)

    missing = [i for i, s in enumerate(scored) if s is None]
    if len(missing) > n // 2:
        raise LLMError(f"model scored only {n - len(missing)} of {n} windows")
    if missing:
        log.warning("llm: %d of %d windows unscored; treated as 0.0", len(missing), n)
    return [s if s is not None else (0.0, UNSCORED_REASON) for s in scored]


def get_llm_client() -> LLMClient:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if key:
        log.info("llm: using AnthropicLLMClient")
        return AnthropicLLMClient(key)
    log.warning("llm: ANTHROPIC_API_KEY absent — using MockLLMClient (offline, deterministic)")
    return MockLLMClient()


__all__ = [
    "AnthropicLLMClient",
    "LLMClient",
    "LLMError",
    "MockLLMClient",
    "Scored",
    "ScoredWindow",
    "TokenUsage",
    "get_llm_client",
]
