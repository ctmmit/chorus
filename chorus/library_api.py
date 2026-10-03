"""Library import routes: bring in what the principal follows and has saved.

    POST /library/import     LibraryImport -> ImportPreview
    GET  /library/items      the owner's stored items (?status=&limit=)
    POST /library/soul       soul.md derived from the owner's library

The agent-push path: an agent that already has a connector for the
principal's library (a Readwise MCP, a Spotify MCP) fetches the items itself
and posts them here, so Chorus never holds that provider's token. SKILL.md
documents the field mapping per provider.

An import never subscribes anyone or rewrites a soul. It stores the items,
resolves what it can (chorus.library_resolve), and returns an ImportPreview:
ranked `suggested_sources` to offer the principal, a `saved_queue_source` to
add to a subscription so runs draw from the saved queue, and what could not
be resolved and why. The agent applies the suggestions with the existing
subscription tools.

`LibraryService` is shared by these routes and the MCP tools
(chorus.mcp_server) so both behave identically.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from chorus.bootstrap import get_soul_builder
from chorus.jobs import MASTER_OWNER
from chorus.library import (
    CORPUS_MAX_TEXTS,
    LibraryImport,
    LibraryItem,
    ResolutionStatus,
    SavedItem,
    ShowSuggestion,
    corpus_texts,
    merge_item,
    rank_show_suggestions,
    saved_queue_episodes,
    show_key,
)
from chorus.library_resolve import LibraryResolver
from chorus.podcasts_api import PodcastDirectory
from chorus.saved_items import SavedItemStore
from chorus.subscriptions import RssSource, SavedQueueSource, SubscriptionStore

log = logging.getLogger("chorus.library_api")

# Pending items resolved per import call (each new show costs one throttled
# Apple lookup, so a big library finishes over a few calls).
MAX_RESOLVE_PER_IMPORT = 300
MAX_UNRESOLVED_LISTED = 50
LIST_DEFAULT_LIMIT = 50
LIST_MAX_LIMIT = 500


class UnresolvedItem(BaseModel):
    """An item that is not digestible yet, and why."""

    title: str = Field(description="Item title.")
    show_title: str | None = Field(default=None, description="Its show, if known.")
    status: ResolutionStatus = Field(description='"pending" (will retry) or "unresolved".')
    reason: str | None = Field(default=None, description="Why.")


class ImportPreview(BaseModel):
    """What an import stored and what the principal can do with it."""

    received: int = Field(description="Items in this request (after de-duplication).")
    new: int = Field(description="Items not seen in an earlier import.")
    queued_episodes: int = Field(
        description="Resolved, unheard saved episodes now in the owner's saved queue."
    )
    pending: int = Field(description="Items still waiting on a lookup; import again to finish.")
    unresolved_count: int = Field(description="Items that could not be matched to a feed episode.")
    unresolved: list[UnresolvedItem] = Field(
        description=f"Up to {MAX_UNRESOLVED_LISTED} pending or unresolved items, newest first."
    )
    suggested_sources: list[ShowSuggestion] = Field(
        description="Shows ranked by recency-weighted saves; offer these as subscriptions."
    )
    saved_queue_source: SavedQueueSource = Field(
        description="Add this to a subscription's sources so each run draws from the saved queue."
    )
    corpus_items: int = Field(description="Items available to POST /library/soul.")
    next_steps: str = Field(description="What to do with this preview.")


class LibrarySoul(BaseModel):
    """A soul derived from the owner's library."""

    soul: str = Field(description="The soul.md markdown; a proposal, not saved anywhere.")
    based_on: int = Field(description="Library items the soul was derived from.")


class LibraryService:
    def __init__(
        self,
        store: SavedItemStore,
        directory: PodcastDirectory,
        subscriptions: SubscriptionStore | None = None,
    ) -> None:
        self.store = store
        self._resolver = LibraryResolver(directory)
        self._subscriptions = subscriptions

    def import_items(
        self, owner: str, payload: LibraryImport, now: datetime | None = None
    ) -> ImportPreview:
        now = now or datetime.now(UTC)
        incoming: dict[str, LibraryItem] = {}
        for item in payload.items:
            incoming[item.item_key()] = item  # last occurrence wins
        existing = self.store.get_many(owner, list(incoming))
        merged = [merge_item(existing.get(key), item, owner, now) for key, item in incoming.items()]
        # Persist before any lookup, so a failure mid-resolution loses nothing.
        self.store.put_many(merged)

        pending = self.store.list(owner, status="pending", limit=MAX_RESOLVE_PER_IMPORT)
        if pending:
            self.store.put_many(self._resolver.resolve(pending))
        return self._preview(owner, now, received=len(incoming), new=len(incoming) - len(existing))

    def list_items(
        self, owner: str, status: ResolutionStatus | None = None, limit: int = LIST_DEFAULT_LIMIT
    ) -> list[SavedItem]:
        return self.store.list(owner, status=status, limit=max(1, min(limit, LIST_MAX_LIMIT)))

    def soul(self, owner: str) -> LibrarySoul:
        items = [s.item for s in self.store.list(owner, limit=CORPUS_MAX_TEXTS)]
        if not items:
            raise ValueError("this owner has no imported library items yet; import some first")
        texts = corpus_texts(items)
        return LibrarySoul(soul=get_soul_builder().derive_from_corpus(texts), based_on=len(texts))

    def _subscribed_feeds(self, owner: str) -> list[str]:
        if self._subscriptions is None:
            return []
        return [
            source.feed_url
            for sub in self._subscriptions.list(owner=owner)
            for source in sub.sources or []
            if isinstance(source, RssSource)
        ]

    def _preview(self, owner: str, now: datetime, *, received: int, new: int) -> ImportPreview:
        everything = self.store.list(owner)
        sources: dict[str, RssSource] = {}
        for entry in everything:
            if entry.show_source is not None:
                sources.setdefault(show_key(entry.item), entry.show_source)
        suggestions = rank_show_suggestions(
            [e.item for e in everything],
            now,
            sources=sources,
            subscribed_feeds=self._subscribed_feeds(owner),
        )
        queue = SavedQueueSource(kind="saved")
        queued = saved_queue_episodes(everything, queue, now, limit=max(1, len(everything)))
        not_ready = [e for e in everything if e.status in ("pending", "unresolved")]
        pending = sum(1 for e in not_ready if e.status == "pending")

        steps = [
            "Show the principal suggested_sources and subscribe (or update_subscription) "
            "with the ones they approve; each has a ready `source` when it resolved to a feed.",
        ]
        if queued:
            steps.append(
                f"Add saved_queue_source to a subscription's sources so runs draw from the "
                f"{len(queued)} saved episode(s) not yet heard."
            )
        if pending:
            steps.append(
                f"{pending} item(s) are still pending; import again in a minute to finish."
            )
        steps.append(
            "POST /library/soul (MCP: soul_from_library) proposes a soul from this library."
        )

        return ImportPreview(
            received=received,
            new=new,
            queued_episodes=len(queued),
            pending=pending,
            unresolved_count=len(not_ready) - pending,
            unresolved=[
                UnresolvedItem(
                    title=e.item.title,
                    show_title=e.item.show_title,
                    status=e.status,
                    reason=e.reason,
                )
                for e in not_ready[:MAX_UNRESOLVED_LISTED]
            ],
            suggested_sources=suggestions,
            saved_queue_source=queue,
            corpus_items=min(len(everything), CORPUS_MAX_TEXTS),
            next_steps=" ".join(steps),
        )


def _owner(request: Request) -> str:
    return getattr(request.state, "owner", MASTER_OWNER)


def build_library_router(service: LibraryService) -> APIRouter:
    router = APIRouter()

    @router.post("/library/import")
    def import_library(payload: LibraryImport, request: Request) -> ImportPreview:
        return service.import_items(_owner(request), payload)

    @router.get("/library/items")
    def list_library_items(
        request: Request,
        status: ResolutionStatus | None = None,
        limit: int = Query(default=LIST_DEFAULT_LIMIT, ge=1, le=LIST_MAX_LIMIT),
    ) -> list[SavedItem]:
        return service.list_items(_owner(request), status, limit)

    @router.post("/library/soul")
    def soul_from_library(request: Request) -> LibrarySoul:
        try:
            return service.soul(_owner(request))
        except ValueError as err:
            raise HTTPException(status_code=422, detail=str(err)) from err
        except Exception as err:
            log.exception("library/soul: soul builder failed")
            raise HTTPException(
                status_code=502, detail="the soul builder failed; try again"
            ) from err

    return router
