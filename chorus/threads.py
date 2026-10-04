"""Cross-source threads: when several sources speak to the same question.

Curation scores each episode alone, so a digest could not say that three
guests disagreed about the same thing this week. That conversation is the
most valuable thing a week of listening can surface, and the product is
named for it. After curation, one pass over the surfaced highlights (quotes
and reasons only, which is cheap) groups them into `Thread`s: a question in
plain words, and the highlights that answer it, each with a stance.

`validate_threads` is what keeps a thread honest regardless of who wrote
it: a member must be a real highlight (its episode is taken from the
highlight, never from the writer), and a thread must span at least two
sources. A thread is therefore always grounded and always cross-source.

The outline stage receives the threads as candidate segments that put
sources in conversation (chorus/outline.py); the writer still decides
whether to use them. Disagreements are offered first.
"""
from __future__ import annotations

import logging
import re
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from pydantic import ValidationError

from chorus.models import Digest, Highlight, Thread, ThreadMember

log = logging.getLogger("chorus.threads")

MIN_SOURCES = 2
MAX_THREADS = 5
MAX_QUESTION_CHARS = 200
# The mock links two highlights from different sources when they share at
# least this many content words.
MIN_SHARED_WORDS = 3
MOCK_QUESTION_WORDS = 3
MIN_WORD_CHARS = 4

_WORD_RE = re.compile(r"[a-z][a-z'-]+")
_STOPWORDS = frozenset(
    ["about", "above", "after", "again", "against", "also", "because", "been", "before", "being", "below", "between", "both", "could", "does", "doing", "down", "during", "each", "from", "further", "have", "having", "here", "into", "itself", "just", "more", "most", "much", "must", "only", "other", "over", "same", "should", "some", "such", "than", "that", "their", "them", "then", "there", "these", "they", "this", "those", "through", "under", "until", "very", "want", "were", "what", "when", "where", "which", "while", "will", "with", "would", "your", "yours", "every", "last", "week", "next", "today", "going", "really", "thing", "things", "think", "said", "says", "like", "make", "made", "know"]
)
# A member whose quote carries one of these reads as pushing back.
_DISAGREE_MARKERS = frozenset(
    {"disagree", "wrong", "not", "never", "opposite", "isn't", "doesn't", "won't", "don't"}
)


@runtime_checkable
class ThreadWriter(Protocol):
    def write(self, highlights: list[Highlight]) -> list[Thread]: ...


@dataclass(frozen=True)
class Remembered:
    """A claim surfaced in an earlier digest (chorus/memory.py), offered to
    the thread writer next to this week's highlights."""

    highlight: Highlight
    surfaced_at: datetime


def content_words(text: str) -> set[str]:
    """Lower-cased content words: no stopwords, at least MIN_WORD_CHARS long."""
    return {
        w for w in _WORD_RE.findall(text.lower())
        if len(w) >= MIN_WORD_CHARS and w not in _STOPWORDS
    }


def _highlight_text(highlight: Highlight) -> str:
    return f"{highlight.quote} {highlight.excerpt}"


def validate_threads(
    threads: list[Thread], digest: Digest, remembered: Sequence[Remembered] = ()
) -> list[Thread]:
    """Keep only grounded, cross-source threads: unknown highlight ids are
    dropped, each member's episode comes from its highlight, a highlight
    appears once per thread, and a thread needs `MIN_SOURCES` sources and at
    least one of this week's highlights. A remembered member carries its
    date, quote and source. Disagreements first, then by size; at most
    `MAX_THREADS`."""
    current = {h.highlight_id: h for h in digest.highlights}
    past = {
        r.highlight.highlight_id: r for r in remembered if r.highlight.highlight_id not in current
    }
    kept: list[Thread] = []
    for thread in threads:
        question = " ".join(thread.question.split())[:MAX_QUESTION_CHARS]
        members: list[ThreadMember] = []
        seen: set[str] = set()
        for member in thread.members:
            if member.highlight_id in seen:
                continue
            if member.highlight_id in current:
                highlight = current[member.highlight_id]
                update: dict[str, object] = {
                    "episode_id": highlight.episode_id,
                    "remembered_at": None,
                    "quote": None,
                    "source": None,
                }
            elif member.highlight_id in past:
                old = past[member.highlight_id]
                update = {
                    "episode_id": old.highlight.episode_id,
                    "remembered_at": old.surfaced_at,
                    "quote": old.highlight.quote,
                    "source": old.highlight.show or old.highlight.episode_title,
                }
            else:
                continue
            seen.add(member.highlight_id)
            members.append(member.model_copy(update=update))
        this_week = [m for m in members if m.remembered_at is None]
        if question and this_week and len({m.episode_id for m in members}) >= MIN_SOURCES:
            kept.append(Thread(question=question, members=members))
    kept.sort(key=lambda t: (not t.disagreement, -len(t.members)))
    return kept[:MAX_THREADS]


class MockThreadWriter:
    """Deterministic, offline. Links highlights from different sources that
    share `MIN_SHARED_WORDS` content words, clusters the links, and names each
    cluster's question after its most shared words. Within a cluster the
    first highlight `adds`; a later one `disagrees` when its quote pushes back
    ("not", "wrong", "disagree"...), else `agrees`."""

    def write(self, highlights: list[Highlight]) -> list[Thread]:
        words = [content_words(_highlight_text(h)) for h in highlights]
        parent = list(range(len(highlights)))

        def find(i: int) -> int:
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        for i in range(len(highlights)):
            for j in range(i + 1, len(highlights)):
                if highlights[i].episode_id == highlights[j].episode_id:
                    continue
                if len(words[i] & words[j]) >= MIN_SHARED_WORDS:
                    parent[find(j)] = find(i)

        clusters: dict[int, list[int]] = {}
        for i in range(len(highlights)):
            clusters.setdefault(find(i), []).append(i)

        threads: list[Thread] = []
        for indexes in clusters.values():
            if len({highlights[i].episode_id for i in indexes}) < MIN_SOURCES:
                continue
            counts = Counter(w for i in indexes for w in words[i])
            shared = [w for w, n in counts.most_common() if n >= 2][:MOCK_QUESTION_WORDS]
            topic = ", ".join(shared) if shared else "the same subject"
            members = [
                ThreadMember(
                    highlight_id=highlights[i].highlight_id,
                    episode_id=highlights[i].episode_id,
                    stance="adds" if rank == 0 else (
                        "disagrees"
                        if set(_WORD_RE.findall(highlights[i].quote.lower())) & _DISAGREE_MARKERS
                        else "agrees"
                    ),
                )
                for rank, i in enumerate(indexes)
            ]
            threads.append(Thread(question=f"What do the sources say about {topic}?", members=members))
        return threads


THREADS_SYSTEM = """You read the highlights a principal's podcast digest surfaced this week, \
from several different sources, and find the questions two or more sources speak to.

For each such question, list the highlights that answer it and each one's stance relative to \
the others: "agrees", "disagrees", or "adds" (new information without taking a side). A thread \
must include highlights from at least two different sources. Do not invent a connection the \
quotes do not support; no threads is a fine answer. Prefer disagreements.

Reply with ONLY a JSON object: {"threads": [{"question": "...", "members": \
[{"highlight_id": "...", "stance": "agrees"|"disagrees"|"adds"}]}]}"""


class AnthropicThreadWriter:
    MODEL = "claude-haiku-4-5-20251001"
    MAX_TOKENS = 1500

    def __init__(self, api_key: str | None = None, client: Any | None = None) -> None:
        if client is None:
            import anthropic  # type: ignore[import-not-found]  # optional dep; only with a key

            client = anthropic.Anthropic(api_key=api_key)
        self._client: Any = client

    def write(self, highlights: list[Highlight]) -> list[Thread]:
        from chorus.script import json_object

        lines = [
            f"- id {h.highlight_id} | source {h.show or ''}: {h.episode_title or h.episode_id} | "
            f'"{h.quote}" | why: {h.why_surface}'
            for h in highlights
        ]
        msg = self._client.messages.create(
            model=self.MODEL,
            max_tokens=self.MAX_TOKENS,
            system=THREADS_SYSTEM,
            messages=[{"role": "user", "content": "HIGHLIGHTS:\n" + "\n".join(lines)}],
        )
        body = "".join(b.text for b in msg.content if b.type == "text")
        threads: list[Thread] = []
        for raw in json_object(body).get("threads", []):
            members = [
                {**m, "episode_id": ""} for m in raw.get("members", []) if isinstance(m, dict)
            ]
            try:
                threads.append(Thread.model_validate({**raw, "members": members}))
            except ValidationError as err:
                log.info("threads: dropping unusable thread (%s)", err)
        return threads


def get_thread_writer() -> ThreadWriter:
    import os

    key = os.environ.get("ANTHROPIC_API_KEY")
    return AnthropicThreadWriter(key) if key else MockThreadWriter()


def thread_writer_for(llm: object) -> Callable[[], ThreadWriter]:
    """Follow the deployment's scorer, as chorus.feedback.proposal_writer_for does."""
    from chorus.llm import MockLLMClient

    if isinstance(llm, MockLLMClient):
        return MockThreadWriter
    return get_thread_writer


def build_threads(
    digest: Digest, writer: ThreadWriter, remembered: Sequence[Remembered] = ()
) -> list[Thread]:
    """Threads for a digest, with claims remembered from earlier digests
    offered alongside. None (and no model call) when this week surfaced
    nothing, or fewer than two sources are in play."""
    if not digest.highlights:
        return []
    current = {h.highlight_id for h in digest.highlights}
    past = [r for r in remembered if r.highlight.highlight_id not in current]
    sources = {h.episode_id for h in digest.highlights} | {r.highlight.episode_id for r in past}
    if len(sources) < MIN_SOURCES:
        return []
    candidates = list(digest.highlights) + [r.highlight for r in past]
    return validate_threads(writer.write(candidates), digest, past)


def threads_brief(digest: Digest) -> str:
    """The threads as the outline prompt shows them: question, then each
    member's source id, stance and quote."""
    by_id = {h.highlight_id: h for h in digest.highlights}
    blocks: list[str] = []
    for thread in digest.threads:
        lines = [f"- {thread.question}" + ("  (a disagreement)" if thread.disagreement else "")]
        for member in thread.members:
            if member.remembered_at is not None:
                when = member.remembered_at.strftime("%d %b %Y")
                lines.append(
                    f"    {member.episode_id} {member.stance} (surfaced {when}, not citable "
                    f'this week): "{member.quote or ""}"'
                )
                continue
            quote = by_id[member.highlight_id].quote if member.highlight_id in by_id else ""
            lines.append(f'    {member.episode_id} {member.stance}: "{quote}"')
        blocks.append("\n".join(lines))
    return "\n".join(blocks)
