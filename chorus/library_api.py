"""Library import routes: bring in what the principal follows and has saved.

    POST /library/share        links or text holding links -> ShareResult
    POST /library/import-file  YouTube Takeout CSV or OPML -> FileImportResult
    POST /library/import       LibraryImport -> ImportPreview
    GET  /library/items        the owner's stored items (?status=&limit=)
    POST /library/soul         soul.md derived from the owner's library

None of these needs the principal to sign in to anything:

- Share: any Apple Podcasts, Spotify or YouTube episode or show link, from a
  phone's share sheet (an iOS Shortcut that POSTs here), a pasted message,
  or an agent relaying what the principal sent it. Chorus is the save-for-
  later queue; no read-later app is involved.
- Files: a Google Takeout YouTube subscriptions export, or an OPML export
  from a podcast app.
- Agent push: an agent that already has a connector for the principal's
  library (a Readwise MCP, a Spotify MCP) fetches the items itself and posts
  them to /library/import, so Chorus never holds that provider's token.

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
from collections.abc import Callable
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field, model_validator

from chorus.bootstrap import MockSoulBuilder, SoulBuilder, get_soul_builder
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
from chorus.library_inputs import (
    MAX_SHARE_LINKS,
    MAX_SHARE_TEXT_CHARS,
    FileFormat,
    ParsedLinks,
    SkippedLink,
    links_from_text,
    opml_show_items,
    parse_links,
    parse_youtube_takeout,
)
from chorus.library_resolve import LibraryResolver
from chorus.podcasts_api import PodcastDirectory, PodcastError, parse_opml
from chorus.saved_items import SavedItemStore
from chorus.subscriptions import RssSource, SavedQueueSource, SubscriptionStore, YoutubeSource

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
    corpus: list[str] = Field(
        default_factory=list,
        description=(
            "The library texts, returned only when the caller's own agent is the brain: "
            "the agent writes the soul from them instead of Chorus calling a model."
        ),
    )
    agent_notes: list[str] = Field(
        default_factory=list, description="What the calling agent should do with this draft."
    )


# Returns the builder for a soul, or None when the caller's own agent writes it.
SoulBuilderFactory = Callable[[], SoulBuilder | None]

HOST_SOUL_NOTES = [
    "Chorus called no model: `soul` is a plain template. Write the real soul yourself from "
    "`corpus` (what the principal saved), keeping the six section headings.",
    "Validate your draft with onboarding_soul_draft(source='write', markdown=...), show it to "
    "the principal in full, and save it only after they approve.",
]


class ShareRequest(BaseModel):
    """POST /library/share request body: links, text containing links, or both."""

    links: list[str] = Field(
        default_factory=list,
        max_length=MAX_SHARE_LINKS,
        description="Apple Podcasts, Spotify or YouTube episode/show links.",
    )
    text: str | None = Field(
        default=None,
        max_length=MAX_SHARE_TEXT_CHARS,
        description="Free text (a share-sheet payload, a forwarded message); its links are used.",
    )

    @model_validator(mode="after")
    def _something(self) -> ShareRequest:
        if not self.links and not (self.text and self.text.strip()):
            raise ValueError("provide links, or text that contains links")
        return self

    def all_links(self) -> list[str]:
        found = list(dict.fromkeys(link.strip() for link in self.links if link.strip()))
        for link in links_from_text(self.text or ""):
            if link not in found:
                found.append(link)
        return found


class SharedItemStatus(BaseModel):
    """What became of one shared link."""

    link: str = Field(description="The link as shared.")
    title: str = Field(description="Episode or show title (filled in by resolution).")
    show_title: str | None = Field(default=None, description="The show, once known.")
    item_kind: str = Field(description='"episode" or "show".')
    status: ResolutionStatus = Field(description="resolved, pending or unresolved.")
    reason: str | None = Field(default=None, description="Why it is pending or unresolved.")


class ShareResult(BaseModel):
    """POST /library/share response."""

    summary: str = Field(
        description="One line for a phone notification, e.g. what was saved and the queue size."
    )
    items: list[SharedItemStatus] = Field(description="One entry per recognized link.")
    skipped: list[SkippedLink] = Field(description="Links that were not recognized, and why.")
    preview: ImportPreview = Field(description="The owner's library after this share.")


def share_summary(
    items: list[SharedItemStatus], skipped: list[SkippedLink], queued: int
) -> str:
    """The one-line answer a share-sheet Shortcut shows."""
    if not items:
        return f"Nothing saved: {skipped[0].reason}" if skipped else "Nothing saved: no links found"
    queue = f"{queued} episode{'s' if queued != 1 else ''} in your queue."
    if len(items) > 1:
        saved = sum(1 for i in items if i.status != "unresolved")
        return f"Saved {saved} of {len(items) + len(skipped)} links. {queue}"
    item = items[0]
    name = f"“{item.title}”" + (f" ({item.show_title})" if item.show_title else "")
    if item.status == "unresolved":
        return f"Could not match {name}: {item.reason}"
    if item.status == "pending":
        return f"Saved {name}; still looking it up. {queue}"
    if item.item_kind == "show":
        return f"Added {name} to your suggested shows."
    return f"Saved {name}. {queue}"


class ImportFileRequest(BaseModel):
    """POST /library/import-file request body."""

    format: FileFormat = Field(
        description='"youtube_takeout" (Takeout subscriptions.csv) or "opml" (podcast app export).'
    )
    content: str = Field(min_length=1, description="The file's text (max 1 MiB).")


class FileImportResult(BaseModel):
    """POST /library/import-file response."""

    imported: int = Field(description="Followed shows or channels read from the file.")
    skipped: list[SkippedLink] = Field(description="Rows or outlines that were not usable.")
    preview: ImportPreview = Field(description="The owner's library after this import.")


class LibraryService:
    def __init__(
        self,
        store: SavedItemStore,
        directory: PodcastDirectory,
        subscriptions: SubscriptionStore | None = None,
        soul_builder: SoulBuilderFactory = get_soul_builder,
    ) -> None:
        self.store = store
        self._soul_builder = soul_builder
        self._resolver = LibraryResolver(directory)
        self._subscriptions = subscriptions

    def import_items(
        self, owner: str, payload: LibraryImport, now: datetime | None = None
    ) -> ImportPreview:
        now = now or datetime.now(UTC)
        keys, new = self._ingest(owner, payload.items, now)
        return self._preview(owner, now, received=len(keys), new=new)

    def share(self, owner: str, request: ShareRequest, now: datetime | None = None) -> ShareResult:
        """Save shared links as `provider="shared"` items, dated now."""
        now = now or datetime.now(UTC)
        parsed = parse_links(request.all_links(), saved_at=now)
        keys, new = self._ingest(owner, parsed.items, now)
        stored = self.store.get_many(owner, keys)
        statuses = [
            SharedItemStatus(
                link=item.url or "",
                title=entry.item.title or item.url or "",
                show_title=entry.item.show_title,
                item_kind=entry.item.item_kind,
                status=entry.status,
                reason=entry.reason,
            )
            for item in parsed.items
            if (entry := stored.get(item.item_key())) is not None
        ]
        preview = self._preview(owner, now, received=len(keys), new=new)
        return ShareResult(
            summary=share_summary(statuses, parsed.skipped, preview.queued_episodes),
            items=statuses,
            skipped=parsed.skipped,
            preview=preview,
        )

    def import_file(
        self, owner: str, request: ImportFileRequest, now: datetime | None = None
    ) -> FileImportResult:
        """Followed shows from an export file; raises ValueError on a bad file."""
        now = now or datetime.now(UTC)
        if request.format == "youtube_takeout":
            parsed = parse_youtube_takeout(request.content)
        else:
            try:
                opml = parse_opml(request.content)
            except PodcastError as err:
                raise ValueError(str(err)) from err
            parsed = ParsedLinks(
                items=opml_show_items(opml),
                skipped=[SkippedLink(link=s.line, reason=s.reason) for s in opml.skipped],
            )
        keys, new = self._ingest(owner, parsed.items, now)
        return FileImportResult(
            imported=len(keys),
            skipped=parsed.skipped,
            preview=self._preview(owner, now, received=len(keys), new=new),
        )

    def _ingest(self, owner: str, items: list[LibraryItem], now: datetime) -> tuple[list[str], int]:
        """Merge, persist, then resolve what is pending. Returns the distinct
        incoming keys and how many of them were new."""
        incoming: dict[str, LibraryItem] = {}
        for item in items:
            incoming[item.item_key()] = item  # last occurrence wins
        if not incoming:
            return [], 0
        existing = self.store.get_many(owner, list(incoming))
        merged = [merge_item(existing.get(key), item, owner, now) for key, item in incoming.items()]
        # Persist before any lookup, so a failure mid-resolution loses nothing.
        self.store.put_many(merged)

        pending = self.store.list(owner, status="pending", limit=MAX_RESOLVE_PER_IMPORT)
        if pending:
            self.store.put_many(self._resolver.resolve(pending))
        return list(incoming), len(incoming) - len(existing)

    def list_items(
        self, owner: str, status: ResolutionStatus | None = None, limit: int = LIST_DEFAULT_LIMIT
    ) -> list[SavedItem]:
        return self.store.list(owner, status=status, limit=max(1, min(limit, LIST_MAX_LIMIT)))

    def soul(self, owner: str) -> LibrarySoul:
        items = [s.item for s in self.store.list(owner, limit=CORPUS_MAX_TEXTS)]
        if not items:
            raise ValueError("this owner has no imported library items yet; import some first")
        texts = corpus_texts(items)
        builder = self._soul_builder()
        if builder is None:
            return LibrarySoul(
                soul=MockSoulBuilder().derive_from_corpus(texts),
                based_on=len(texts),
                corpus=texts,
                agent_notes=HOST_SOUL_NOTES,
            )
        return LibrarySoul(soul=builder.derive_from_corpus(texts), based_on=len(texts))

    def _subscribed(self, owner: str) -> list[RssSource | YoutubeSource]:
        if self._subscriptions is None:
            return []
        return [
            source
            for sub in self._subscriptions.list(owner=owner)
            for source in sub.sources or []
            if isinstance(source, (RssSource, YoutubeSource))
        ]

    def _preview(self, owner: str, now: datetime, *, received: int, new: int) -> ImportPreview:
        everything = self.store.list(owner)
        sources: dict[str, RssSource | YoutubeSource] = {}
        for entry in everything:
            if entry.show_source is not None:
                sources.setdefault(show_key(entry.item), entry.show_source)
        suggestions = rank_show_suggestions(
            [e.item for e in everything],
            now,
            sources=sources,
            subscribed=self._subscribed(owner),
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
                    title=e.item.title or e.item.url or "(untitled)",
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

    @router.post("/library/share")
    def share_links(payload: ShareRequest, request: Request) -> ShareResult:
        return service.share(_owner(request), payload)

    @router.post("/library/import-file")
    def import_file(payload: ImportFileRequest, request: Request) -> FileImportResult:
        try:
            return service.import_file(_owner(request), payload)
        except ValueError as err:
            raise HTTPException(status_code=422, detail=str(err)) from err

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
