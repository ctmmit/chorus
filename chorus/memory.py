"""Running memory across weeks: what Chorus already surfaced for a principal.

An analyst remembers last month. Each finished job writes the claims it
surfaced (one per highlight, with its thread question when it had one) to a
`ClaimStore`. The next run uses them two ways:

- **Repeat penalty.** A window whose content closely matches a claim
  surfaced in the last `REPEAT_WINDOW_WEEKS` loses `REPEAT_PENALTY` from its
  relevance score, and its reason says so. The same take heard on three
  shows stops filling three highlights. The eval harness
  (scripts/eval_curation.py) is where the constant gets tuned.
- **Threads with the past.** Claims from the last `MEMORY_LOOKBACK_WEEKS`
  that share content with this week's highlights are offered to the thread
  writer (chorus/threads.py) as dated members, so a thread can say that a
  guest three weeks ago predicted what this week's guest disputes.
  Remembered members are shown with their date and are never citable by the
  script, which keeps every spoken claim grounded in this week's sources.

Retrieval is content-word overlap, no vector database: the claims are short
quotes, and the store holds one owner's few hundred at most.

The principal controls it: `remember=False` on a request or subscription
skips both reading and writing, and `clear_memory` forgets everything.
"""
from __future__ import annotations

import sqlite3
import threading
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from mcp.server.fastmcp import Context, FastMCP  # type: ignore[import-not-found,import-untyped]
from pydantic import BaseModel, Field

from chorus.jobs import DEFAULT_DB
from chorus.models import Highlight, Job
from chorus.threads import Remembered, content_words

REPEAT_PENALTY = 0.25
REPEAT_WINDOW_WEEKS = 4
# Share of a remembered claim's content words a window must contain to count
# as repeating it.
REPEAT_MIN_OVERLAP = 0.6
MEMORY_LOOKBACK_WEEKS = 12
MAX_REMEMBERED_CLAIMS = 20
# Share of a claim's content words some current highlight must share for the
# claim to be offered to the thread writer.
RECALL_MIN_OVERLAP = 0.2
CLAIMS_TABLE = "remembered_claims"


class Claim(BaseModel):
    owner: str
    highlight_id: str
    job_id: str
    episode_id: str
    show: str | None = None
    episode_title: str | None = None
    segment_timestamp: float = 0.0
    quote: str
    why_surface: str = ""
    thread_question: str | None = None
    surfaced_at: datetime


# --- Pure logic -----------------------------------------------------------------


def claims_from(job: Job, surfaced_at: datetime) -> list[Claim]:
    """One claim per surfaced highlight of a finished job."""
    if job.digest is None:
        return []
    questions: dict[str, str] = {}
    for thread in job.digest.threads:
        for member in thread.members:
            if member.remembered_at is None:
                questions.setdefault(member.highlight_id, thread.question)
    return [
        Claim(
            owner=job.owner,
            highlight_id=h.highlight_id,
            job_id=job.job_id,
            episode_id=h.episode_id,
            show=h.show,
            episode_title=h.episode_title,
            segment_timestamp=h.segment_timestamp,
            quote=h.quote,
            why_surface=h.why_surface,
            thread_question=questions.get(h.highlight_id),
            surfaced_at=surfaced_at,
        )
        for h in job.digest.highlights
    ]


def _coverage(claim_words: set[str], text_words: set[str]) -> float:
    """Share of the claim's content words found in the text."""
    return len(claim_words & text_words) / len(claim_words) if claim_words else 0.0


def repeated_claim(window_text: str, claims: Sequence[Claim]) -> Claim | None:
    """The remembered claim this window repeats, if any: the one whose
    content words the window covers most, at or above REPEAT_MIN_OVERLAP."""
    words = content_words(window_text)
    best: tuple[float, Claim] | None = None
    for claim in claims:
        coverage = _coverage(content_words(claim.quote), words)
        if coverage >= REPEAT_MIN_OVERLAP and (best is None or coverage > best[0]):
            best = (coverage, claim)
    return best[1] if best else None


def penalize(score: float, reason: str, claim: Claim) -> tuple[float, str]:
    when = claim.surfaced_at.strftime("%d %b %Y")
    source = claim.show or claim.episode_title or claim.episode_id
    return (
        max(0.0, score - REPEAT_PENALTY),
        f"{reason}; repeats a point surfaced {when} from {source}",
    )


def related_claims(
    highlights: Sequence[Highlight], claims: Sequence[Claim], limit: int = MAX_REMEMBERED_CLAIMS
) -> list[Claim]:
    """Remembered claims worth offering to the thread writer: from a job
    other than this week's, sharing at least RECALL_MIN_OVERLAP of their
    content words with some current highlight. Best first."""
    current_ids = {h.highlight_id for h in highlights}
    texts = [content_words(f"{h.quote} {h.excerpt}") for h in highlights]
    scored: list[tuple[float, Claim]] = []
    for claim in claims:
        if claim.highlight_id in current_ids:
            continue
        words = content_words(claim.quote)
        best = max((_coverage(words, t) for t in texts), default=0.0)
        if best >= RECALL_MIN_OVERLAP:
            scored.append((best, claim))
    scored.sort(key=lambda pair: (-pair[0], -pair[1].surfaced_at.timestamp()))
    return [claim for _, claim in scored[:limit]]


def as_remembered(claim: Claim) -> Remembered:
    return Remembered(
        highlight=Highlight(
            episode_id=claim.episode_id,
            episode_title=claim.episode_title,
            segment_timestamp=claim.segment_timestamp,
            quote=claim.quote,
            relevance_score=0.0,
            why_surface=claim.why_surface,
            show=claim.show,
            highlight_id=claim.highlight_id,
        ),
        surfaced_at=claim.surfaced_at,
    )


# --- Storage ----------------------------------------------------------------------


@runtime_checkable
class ClaimStore(Protocol):
    def remember(self, claims: list[Claim]) -> None:
        """Upsert by (owner, highlight_id): a rerun refreshes the date."""
        ...

    def recent(self, owner: str, since: datetime) -> list[Claim]:
        """Claims surfaced at or after `since`, newest first."""
        ...

    def clear(self, owner: str) -> int:
        """Forget every claim for `owner`; returns how many."""
        ...

    def close(self) -> None: ...


class SqliteClaimStore:
    def __init__(self, db_path: Path | str = DEFAULT_DB) -> None:
        self.db_path = str(db_path)
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock:
            self._conn.execute(
                f"CREATE TABLE IF NOT EXISTS {CLAIMS_TABLE} (owner TEXT NOT NULL, "
                "highlight_id TEXT NOT NULL, surfaced_at TEXT NOT NULL, payload TEXT NOT NULL, "
                "PRIMARY KEY (owner, highlight_id))"
            )
            self._conn.execute(
                f"CREATE INDEX IF NOT EXISTS idx_claims_owner_surfaced "
                f"ON {CLAIMS_TABLE} (owner, surfaced_at)"
            )
            self._conn.commit()

    def remember(self, claims: list[Claim]) -> None:
        with self._lock:
            self._conn.executemany(
                f"INSERT OR REPLACE INTO {CLAIMS_TABLE} "
                "(owner, highlight_id, surfaced_at, payload) VALUES (?, ?, ?, ?)",
                [(c.owner, c.highlight_id, c.surfaced_at.isoformat(), c.model_dump_json())
                 for c in claims],
            )
            self._conn.commit()

    def recent(self, owner: str, since: datetime) -> list[Claim]:
        with self._lock:
            rows = self._conn.execute(
                f"SELECT payload FROM {CLAIMS_TABLE} WHERE owner = ? AND surfaced_at >= ? "
                "ORDER BY surfaced_at DESC",
                (owner, since.isoformat()),
            ).fetchall()
        return [Claim.model_validate_json(r[0]) for r in rows]

    def clear(self, owner: str) -> int:
        with self._lock:
            cur = self._conn.execute(f"DELETE FROM {CLAIMS_TABLE} WHERE owner = ?", (owner,))
            self._conn.commit()
        return cur.rowcount

    def close(self) -> None:
        with self._lock:
            self._conn.close()


# --- What a run reads ----------------------------------------------------------------


class Recall(BaseModel):
    """What one run remembers: claims for the repeat penalty, and claims to
    offer the thread writer."""

    repeats: list[Claim] = Field(default_factory=list)
    lookback: list[Claim] = Field(default_factory=list)


def recall(store: ClaimStore | None, owner: str, now: datetime) -> Recall:
    if store is None:
        return Recall()
    lookback = store.recent(owner, now - timedelta(weeks=MEMORY_LOOKBACK_WEEKS))
    cutoff = now - timedelta(weeks=REPEAT_WINDOW_WEEKS)
    return Recall(repeats=[c for c in lookback if c.surfaced_at >= cutoff], lookback=lookback)


# --- MCP -------------------------------------------------------------------------------


def register_memory_tools(server: FastMCP, get_store: Callable[[], ClaimStore | None]) -> None:
    """clear_memory: forget what Chorus surfaced for the caller."""
    from chorus.mcp_server import _owner_from_context

    def clear_memory(ctx: Context | None = None) -> dict[str, Any]:
        """Forget every claim Chorus has remembered from your past digests.
        Later digests stop penalizing repeats and stop referring back until
        new digests are remembered. To stop remembering for one
        subscription, set its `remember` to false instead."""
        store = get_store()
        if store is None:
            return {"forgotten": 0, "note": "this deployment keeps no memory"}
        return {"forgotten": store.clear(_owner_from_context(ctx))}

    server.add_tool(clear_memory)
