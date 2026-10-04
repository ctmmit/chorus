"""Context: what the principal is working on and reading this week.

`DigestRequest.context` has always been a free-text blob the calling agent
assembles. It is the most underused input: most principals have a reading
queue, notes and a calendar that say what they care about right now. This
module gives that input structure without making Chorus hold anyone's
credentials.

- `ContextBlock` (chorus/models.py) is one source's contribution: where it
  came from, its items, and when it was gathered. An agent may send
  `context_blocks` next to (or instead of) the `context` string.
  `render_context` turns them into the text curation reads, within the
  same `MAX_CONTEXT_CHARS` budget, and the job records which sources
  contributed (`JobUsage.context_sources`).
- The `chorus-context` skill teaches the host agent what to pull from the
  sources it can already reach (Readwise, an Obsidian vault, a calendar,
  a task list), how much, and what never to include. That is the primary
  path: the agent already holds those connections.
- `ContextProvider` is the seam for Chorus to pull a source itself later.
  `ReadwiseContextProvider` implements it against Readwise's export API with
  a principal's own token; it is not wired into the hosted service yet.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

import httpx

from chorus.models import MAX_CONTEXT_CHARS, ContextBlock, ContextItem

log = logging.getLogger("chorus.context")

# Each block gets at most this share of the budget when several compete, so
# one long reading list cannot crowd out a short list of active projects.
MIN_BLOCK_SHARE = 0.2
ITEM_MAX_CHARS = 400
ELLIPSIS = "..."

READWISE_EXPORT_URL = "https://readwise.io/api/v2/export/"
READWISE_TIMEOUT_S = 30.0
READWISE_MAX_ITEMS = 40


def _item_line(item: ContextItem) -> str:
    text = " ".join(item.text.split())
    if len(text) > ITEM_MAX_CHARS:
        text = text[: ITEM_MAX_CHARS - len(ELLIPSIS)].rstrip() + ELLIPSIS
    label = f" ({item.label})" if item.label else ""
    return f"- {text}{label}"


def _block_text(block: ContextBlock, budget: int) -> str:
    when = f", as of {block.as_of.strftime('%d %b %Y')}" if block.as_of else ""
    lines = [f"## {block.source}{when}"]
    used = len(lines[0])
    for item in block.items:
        line = _item_line(item)
        if used + 1 + len(line) > budget:
            break
        lines.append(line)
        used += 1 + len(line)
    return "\n".join(lines) if len(lines) > 1 else ""


def render_context(
    context: str, blocks: list[ContextBlock], budget: int = MAX_CONTEXT_CHARS
) -> tuple[str, list[str]]:
    """The context string curation reads, and the sources that made it in.
    The caller's free text comes first and is kept whole (it is already
    bounded); the blocks share what is left, each guaranteed at least
    MIN_BLOCK_SHARE of it, newest items first as the caller ordered them."""
    parts = [context.strip()] if context.strip() else []
    remaining = budget - sum(len(p) + 2 for p in parts)
    sources: list[str] = []
    live = [b for b in blocks if b.items]
    for index, block in enumerate(live):
        share = max(int(remaining * MIN_BLOCK_SHARE), remaining // (len(live) - index))
        text = _block_text(block, min(share, remaining))
        if not text:
            continue
        parts.append(text)
        sources.append(block.source)
        remaining -= len(text) + 2
        if remaining <= 0:
            break
    return "\n\n".join(parts), sources


@runtime_checkable
class ContextProvider(Protocol):
    def fetch(self, since: datetime) -> ContextBlock:
        """What the source holds that is new since `since`."""
        ...


class MockContextProvider:
    """Deterministic stand-in: returns the items it was built with."""

    def __init__(self, source: str, items: list[ContextItem]) -> None:
        self.source = source
        self.items = items

    def fetch(self, since: datetime) -> ContextBlock:
        return ContextBlock(source=self.source, items=self.items, as_of=since)


class ReadwiseContextProvider:
    """Highlights the principal saved in Readwise since `since`, via the
    export API with their own token. `transport` is injectable for tests."""

    def __init__(self, token: str, transport: httpx.BaseTransport | None = None) -> None:
        self._client = httpx.Client(
            headers={"Authorization": f"Token {token}"},
            timeout=READWISE_TIMEOUT_S,
            transport=transport,
        )

    def fetch(self, since: datetime) -> ContextBlock:
        resp = self._client.get(READWISE_EXPORT_URL, params={"updatedAfter": since.isoformat()})
        resp.raise_for_status()
        items: list[ContextItem] = []
        for book in resp.json().get("results", []):
            title = str(book.get("title") or "")
            for highlight in book.get("highlights", []):
                text = str(highlight.get("text") or "").strip()
                if text:
                    items.append(ContextItem(text=text, label=title or None))
                if len(items) >= READWISE_MAX_ITEMS:
                    break
            if len(items) >= READWISE_MAX_ITEMS:
                break
        return ContextBlock(source="Readwise highlights", items=items, as_of=since)

    def close(self) -> None:
        self._client.close()


def block_summary(blocks: list[ContextBlock]) -> list[dict[str, Any]]:
    """What arrived, for logs: source and item count, never the content."""
    return [{"source": b.source, "items": len(b.items)} for b in blocks]
