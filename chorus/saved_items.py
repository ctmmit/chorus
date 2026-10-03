"""Storage for imported library items (chorus.library.SavedItem), one row per
(owner, item key) so re-importing the same library is an idempotent upsert.

Same split as chorus.subscriptions: a `SavedItemStore` Protocol, a SQLite
implementation for local/dev/test, and `chorus.stores.postgres.
PostgresSavedItemStore` for Vercel, selected by
`chorus.config_env.select_saved_item_store`.

`saved_queue_lister` binds a store to one owner and returns the callable
chorus.feeds.gather_episodes uses for `SavedQueueSource` entries.
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Protocol, runtime_checkable

from chorus.feeds import FeedEpisode, SavedQueueLister
from chorus.jobs import DEFAULT_DB
from chorus.library import ResolutionStatus, SavedItem, saved_queue_episodes
from chorus.subscriptions import SavedQueueSource

SAVED_ITEMS_TABLE = "saved_items"
# The saved queue reads at most this many of an owner's newest items per run.
SAVED_QUEUE_SCAN_LIMIT = 1_000

# See chorus.subscriptions.SubscriptionList: a class with a method named
# `list` shadows the builtin inside its own deferred annotations.
SavedItemList = list[SavedItem]


def saved_at_column(item: SavedItem) -> str:
    """Sortable text: ISO-8601 UTC, or "" for undated items (sorted last)."""
    return item.item.saved_at.isoformat() if item.item.saved_at else ""


@runtime_checkable
class SavedItemStore(Protocol):
    def get_many(self, owner: str, keys: list[str]) -> dict[str, SavedItem]: ...

    def put_many(self, items: list[SavedItem]) -> None: ...

    def list(
        self, owner: str, *, status: ResolutionStatus | None = None, limit: int | None = None
    ) -> SavedItemList: ...

    def close(self) -> None: ...


class SqliteSavedItemStore:
    """`saved_items(owner, item_key, status, saved_at, payload)` with
    PRIMARY KEY (owner, item_key) and an (owner, saved_at) index, in the local
    `chorus.db` by default."""

    def __init__(self, db_path: Path | str = DEFAULT_DB) -> None:
        self.db_path = str(db_path)
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock:
            self._conn.execute(
                f"CREATE TABLE IF NOT EXISTS {SAVED_ITEMS_TABLE} ("
                "owner TEXT NOT NULL, item_key TEXT NOT NULL, status TEXT NOT NULL, "
                "saved_at TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY (owner, item_key))"
            )
            self._conn.execute(
                f"CREATE INDEX IF NOT EXISTS idx_saved_items_owner_saved "
                f"ON {SAVED_ITEMS_TABLE} (owner, saved_at)"
            )
            self._conn.commit()

    def get_many(self, owner: str, keys: list[str]) -> dict[str, SavedItem]:
        if not keys:
            return {}
        found: dict[str, SavedItem] = {}
        with self._lock:
            # Chunked to stay under SQLite's bound-parameter limit.
            for start in range(0, len(keys), 500):
                chunk = keys[start : start + 500]
                marks = ",".join("?" for _ in chunk)
                rows = self._conn.execute(
                    f"SELECT payload FROM {SAVED_ITEMS_TABLE} "
                    f"WHERE owner = ? AND item_key IN ({marks})",
                    (owner, *chunk),
                ).fetchall()
                for (payload,) in rows:
                    item = SavedItem.model_validate_json(payload)
                    found[item.key] = item
        return found

    def put_many(self, items: list[SavedItem]) -> None:
        with self._lock:
            self._conn.executemany(
                f"INSERT OR REPLACE INTO {SAVED_ITEMS_TABLE} "
                "(owner, item_key, status, saved_at, payload) VALUES (?, ?, ?, ?, ?)",
                [
                    (i.owner, i.key, i.status, saved_at_column(i), i.model_dump_json())
                    for i in items
                ],
            )
            self._conn.commit()

    def list(
        self, owner: str, *, status: ResolutionStatus | None = None, limit: int | None = None
    ) -> SavedItemList:
        query = f"SELECT payload FROM {SAVED_ITEMS_TABLE} WHERE owner = ?"
        params: list[object] = [owner]
        if status is not None:
            query += " AND status = ?"
            params.append(status)
        query += " ORDER BY saved_at DESC, item_key"
        if limit is not None:
            query += " LIMIT ?"
            params.append(limit)
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [SavedItem.model_validate_json(r[0]) for r in rows]

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def saved_queue_lister(store: SavedItemStore, owner: str, now: datetime) -> SavedQueueLister:
    """The saved-queue reader for one owner at one moment."""

    def list_saved(
        source: SavedQueueSource, limit: int, exclude_ids: frozenset[str]
    ) -> list[FeedEpisode]:
        resolved = store.list(owner, status="resolved", limit=SAVED_QUEUE_SCAN_LIMIT)
        return saved_queue_episodes(resolved, source, now, limit, exclude_ids)

    return list_saved
