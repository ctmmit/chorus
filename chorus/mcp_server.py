"""MCP distribution surface for local agents and the hosted FastAPI app.

R9 (docs/REVIEW_WAVE1.md #9): submission goes through the SAME `JobRunner`
the HTTP API uses (`chorus.runners`), not a direct, inline `await run_job()`
— a hosted (Inngest) deployment gets the same durable-step semantics an HTTP
`POST /digest` gets, and a long transcript/LLM/audio run can no longer be cut
short by the MCP request's own lifetime. `BackgroundRunner.submit` runs on a
daemon thread when there is no per-request `BackgroundTasks` to hand off to
(there never is one here), so every submit tool returns `{"job_id"}`
immediately either way — callers poll `get_digest`.

R4/R3/R8 ride along: submissions are tagged with the caller's owner (read off
the mounted HTTP request's `request.state.owner`, set by chorus.app's guards
middleware — see `_owner_from_context`), `get_digest` 404s a foreign job
exactly like the HTTP route, a job is quota-checked before creation, and a
runner dispatch failure marks the job failed and raises a tool error that
still carries the job id.
"""
from __future__ import annotations

import functools
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any, Literal, cast

import anyio.to_thread
from fastapi import FastAPI
from mcp.server.fastmcp import Context, FastMCP  # type: ignore[import-not-found,import-untyped]

from chorus import catalog
from chorus.bootstrap import get_soul_builder
from chorus.feeds import SubscriptionPreview
from chorus.jobs import MASTER_OWNER, JobStore, SqliteJobStore
from chorus.library import LibraryImport, LibraryItem, ResolutionStatus, SavedItem
from chorus.library_api import (
    LIST_DEFAULT_LIMIT,
    FileImportResult,
    ImportFileRequest,
    ImportPreview,
    LibraryService,
    LibrarySoul,
    ShareRequest,
    ShareResult,
)
from chorus.library_inputs import FileFormat
from chorus.models import (
    DigestRequest,
    EpisodeInput,
    EpisodeProfile,
    Job,
    JobStatus,
    SelectionRequest,
)
from chorus.pipeline import Deps
from chorus.podcasts_api import OpmlImport, PodcastDirectory, PodcastSearchResult, parse_opml
from chorus.quotas import QuotaExceeded, enforce_job_quota
from chorus.runners import BackgroundRunner, JobRunner
from chorus.subscriptions import (
    DEFAULT_LOOKBACK_DAYS,
    DEFAULT_MAX_EPISODES_PER_RUN,
    Source,
    Subscription,
    SubscriptionCreate,
    SubscriptionStore,
    SubscriptionUpdate,
)
from chorus.subscriptions_api import (
    PreviewRequest,
    apply_update,
    get_owned,
    new_subscription,
    preview_for,
)

MCP_NAME = "Chorus"
MCP_INSTRUCTIONS = (
    "Build persona-conditioned podcast digests. List shows, submit episodes or a catalog "
    "selection, then poll the returned job id (get_digest) until done or failed — submission "
    "returns immediately and does not itself wait for the pipeline to finish. For a recurring "
    "weekly email digest, find shows with search_podcasts (or resolve_podcast for a pasted "
    "URL), check preview_subscription, then subscribe; the service checks each feed for new "
    "episodes on every run and never repeats one. To start from what the principal already "
    "listens to: pass any Apple Podcasts, Spotify or YouTube episode or show link they share "
    "with you to share_links (Chorus becomes their save-for-later queue); import a YouTube "
    "Takeout subscriptions.csv or a podcast-app OPML with import_file; or, if you have your "
    "own connector to their library (e.g. a Readwise MCP), push its items with "
    "import_library. Each returns ranked shows to subscribe to and a saved-queue source that "
    "makes each run draw from unheard saves."
)

def _in_thread[T](fn: Callable[..., T]) -> Callable[..., Any]:
    """Register a blocking-network tool as async: FastMCP runs a plain `def`
    tool on the event loop, and a feed fetch (up to 20 s per source) would
    stall every other request on the app. The wrapper keeps `fn`'s signature
    and docstring (functools.wraps), so the tool schema is unchanged."""

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> T:
        return await anyio.to_thread.run_sync(functools.partial(fn, *args, **kwargs))

    return wrapper


class MCPSubmitError(RuntimeError):
    """Raised by a submit tool when the runner failed to dispatch a job
    (R8) — the job was already created and marked failed, so `job_id` is
    included in the message: the caller can still poll `get_digest` for the
    recorded error instead of losing track of the job entirely."""

    def __init__(self, job_id: str, detail: str) -> None:
        self.job_id = job_id
        super().__init__(f"job {job_id}: dispatch failed: {detail}")


def _owner_from_context(ctx: Context | None) -> str:
    """The caller's owner (R4), read off the underlying HTTP request's
    `request.state.owner` — set by chorus.app's guards middleware before the
    request ever reaches the mounted MCP transport. Verified 21 Sep 2026
    against the installed `mcp` SDK (mcp/server/streamable_http.py,
    mcp/server/lowlevel/server.py): a streamable-HTTP tool call's
    `ctx.request_context.request` is the raw Starlette `Request`. Falls back
    to MASTER_OWNER for the local stdio transport (`main()` below), which has
    no HTTP request at all — that is local/dev-only tooling, equivalent to
    holding the master token."""
    if ctx is None:
        return MASTER_OWNER
    try:
        request = ctx.request_context.request
    except ValueError:
        return MASTER_OWNER
    if request is None:
        return MASTER_OWNER
    owner = getattr(getattr(request, "state", None), "owner", None)
    return owner if isinstance(owner, str) and owner else MASTER_OWNER


class ChorusTools:
    """Directly callable tool implementation with injected storage, providers,
    and job runner."""

    def __init__(
        self,
        store: JobStore,
        deps: Deps,
        runner: JobRunner | None = None,
        subscription_store: SubscriptionStore | None = None,
        podcasts: PodcastDirectory | None = None,
        library: LibraryService | None = None,
    ) -> None:
        self._store = store
        self._deps = deps
        # Defaults to a BackgroundRunner over `store`/`deps` so direct callers
        # (tests, the local stdio transport) work with zero extra wiring;
        # chorus.app.create_app always passes the app's own selected runner.
        self._runner = runner or BackgroundRunner(store, deps)
        self._subscription_store = subscription_store
        self._podcasts = podcasts or PodcastDirectory()
        self._library = library
        self._owns_library = library is None

    def _subscriptions(self) -> SubscriptionStore:
        """The subscription store: the app's own when injected, else selected
        the same way chorus.app does (Postgres when DATABASE_URL is set, else
        SQLite beside the job store). Created lazily so a server that never
        touches subscriptions opens no extra connection."""
        if self._subscription_store is None:
            from chorus import config_env

            self._subscription_store = config_env.select_subscription_store(self._store)
        return self._subscription_store

    def _library_service(self) -> LibraryService:
        """The library service: the app's own when injected, else built
        lazily over the environment-selected saved-item store."""
        if self._library is None:
            from chorus import config_env

            self._library = LibraryService(
                config_env.select_saved_item_store(self._store),
                self._podcasts,
                self._subscriptions(),
            )
        return self._library

    def close(self) -> None:
        """Close stores this instance opened itself (never injected ones)."""
        if self._owns_library and self._library is not None:
            self._library.store.close()

    def list_shows(self) -> list[dict[str, object]]:
        """List catalog shows and their currently selectable episodes."""
        return cast(list[dict[str, object]], catalog.list_shows())

    def submit_digest(
        self,
        soul: str,
        context: str,
        episodes: list[EpisodeInput],
        highlight_count: int = 4,
        profile: EpisodeProfile | None = None,
        ctx: Context | None = None,
    ) -> dict[str, str]:
        """Submit explicit episodes and return {"job_id"} immediately — poll
        get_digest(job_id) for status. `profile` is optional; omit it for the
        single-voice monologue default (R17)."""
        request = DigestRequest(
            soul=soul,
            context=context,
            episodes=episodes,
            highlight_count=highlight_count,
            profile=profile,
        )
        return self._submit(request, ctx)

    def submit_selection(
        self,
        soul: str,
        context: str,
        shows: list[str] | None = None,
        video_ids: list[str] | None = None,
        highlight_count: int = 4,
        profile: EpisodeProfile | None = None,
        ctx: Context | None = None,
    ) -> dict[str, str]:
        """Resolve catalog shows or video ids, submit them, and return
        {"job_id"} immediately — poll get_digest(job_id) for status."""
        selection = SelectionRequest(
            soul=soul,
            context=context,
            shows=shows,
            video_ids=video_ids,
            highlight_count=highlight_count,
            profile=profile,
        )
        episodes = catalog.resolve(shows=selection.shows, video_ids=selection.video_ids)
        if not episodes:
            raise ValueError("selection resolved to no episodes")
        request = DigestRequest(
            soul=selection.soul,
            context=selection.context,
            episodes=episodes,
            highlight_count=selection.highlight_count,
            soul_origin=selection.soul_origin,
            profile=selection.profile,  # R17: previously dropped
        )
        return self._submit(request, ctx)

    def get_digest(self, job_id: str, ctx: Context | None = None) -> Job:
        """Return the current job, including digest, audio URL, warnings, or
        error. Unknown OR foreign (belongs to a different owner) job ids
        raise the same ValueError (R4) — no signal to distinguish them."""
        job = self._store.get(job_id)
        owner = _owner_from_context(ctx)
        if job is None or (owner != MASTER_OWNER and job.owner != owner):
            raise ValueError("unknown job_id")
        return job

    def build_soul_from_interview(self, answers: dict[str, str]) -> str:
        """Build soul.md from the interview answer keys documented in the skill."""
        return get_soul_builder().build_from_interview(answers)

    # --- Recurring digests: search or resolve -> preview -> subscribe ----------

    def search_podcasts(self, query: str, limit: int = 10) -> list[PodcastSearchResult]:
        """Step 1 of subscribing: find podcasts by name in Apple's directory.
        Returns up to `limit` shows with title, author, feed_url, artwork_url
        and apple_id. To subscribe to one, pass {"kind": "rss", "feed_url":
        <feed_url>, "title": <title>} as a source to preview_subscription and
        then subscribe. Results are cached for an hour, so repeat searches
        are free."""
        return self._podcasts.search(query, limit)

    def resolve_podcast(self, url: str) -> Source:
        """Step 1 of subscribing, when you already have a link: turn an RSS
        feed URL, an Apple Podcasts show URL (podcasts.apple.com/.../id123) or
        a YouTube channel URL (/channel/UC...) into a source object ready for
        preview_subscription and subscribe. YouTube @handle URLs resolve only
        when the server has a YouTube API key; otherwise paste the
        /channel/UC... URL."""
        return self._podcasts.resolve(url)

    def preview_subscription(
        self,
        sources: list[Source],
        lookback_days: int = DEFAULT_LOOKBACK_DAYS,
        max_episodes_per_run: int = DEFAULT_MAX_EPISODES_PER_RUN,
        ctx: Context | None = None,
    ) -> SubscriptionPreview:
        """Step 2 of subscribing: show exactly which episodes the first run
        would digest for these sources (published within `lookback_days`,
        capped at `max_episodes_per_run` round-robin across sources), plus any
        source that could not be read. Saves nothing. Show the principal this
        list, drop sources with errors, then call subscribe."""
        return preview_for(
            PreviewRequest(
                sources=sources,
                lookback_days=lookback_days,
                max_episodes_per_run=max_episodes_per_run,
            ),
            _owner_from_context(ctx),
            self._library_service().store,
        )

    def subscribe(
        self,
        email: str,
        soul: str,
        context: str,
        sources: list[Source],
        cadence: Literal["weekly", "daily"] = "weekly",
        highlight_count: int = 4,
        profile: EpisodeProfile | None = None,
        max_episodes_per_run: int = DEFAULT_MAX_EPISODES_PER_RUN,
        ctx: Context | None = None,
    ) -> Subscription:
        """Step 3: create the recurring digest. On every run (weekly: Friday
        13:00 UTC; daily: 13:00 UTC) the service checks each source for
        episodes published since the last run, digests only ones it has not
        sent before, and emails `email`; a week with nothing new gets a short
        "nothing new" note. `sources` are objects from search_podcasts /
        resolve_podcast, e.g. {"kind": "rss", "feed_url": "...", "title": "..."},
        {"kind": "youtube", "channel_id": "UC..."}. Returns the stored
        subscription including its subscription_id."""
        payload = SubscriptionCreate(
            email=email,
            soul=soul,
            context=context,
            sources=sources,
            cadence=cadence,
            highlight_count=highlight_count,
            profile=profile,
            max_episodes_per_run=max_episodes_per_run,
        )
        subscription = new_subscription(payload, _owner_from_context(ctx), datetime.now(UTC))
        self._subscriptions().create(subscription)
        return subscription

    def list_subscriptions(self, ctx: Context | None = None) -> list[Subscription]:
        """List your subscriptions with their sources, schedule, last_run_summary
        and the episode ids already sent. (The master token lists everyone's.)"""
        owner = _owner_from_context(ctx)
        return self._subscriptions().list(owner=None if owner == MASTER_OWNER else owner)

    def update_subscription(
        self,
        subscription_id: str,
        context: str | None = None,
        active: bool | None = None,
        cadence: Literal["weekly", "daily"] | None = None,
        sources: list[Source] | None = None,
        highlight_count: int | None = None,
        max_episodes_per_run: int | None = None,
        notify_when_empty: bool | None = None,
        ctx: Context | None = None,
    ) -> Subscription:
        """Change a subscription; only the fields you pass are changed (leave
        the rest out). Typical uses: refresh `context` before the next run,
        pause or resume with `active`, replace `sources` (episodes already
        sent stay remembered, so nothing repeats), change `cadence`, or turn
        the "nothing new" email off with `notify_when_empty=False`."""
        supplied = {
            "context": context,
            "active": active,
            "cadence": cadence,
            "sources": sources,
            "highlight_count": highlight_count,
            "max_episodes_per_run": max_episodes_per_run,
            "notify_when_empty": notify_when_empty,
        }
        payload = SubscriptionUpdate.model_validate(
            {k: v for k, v in supplied.items() if v is not None}
        )
        store = self._subscriptions()
        current = get_owned(store, subscription_id, _owner_from_context(ctx))
        updated = apply_update(current, payload)
        store.save(updated)
        return updated

    def unsubscribe(self, subscription_id: str, ctx: Context | None = None) -> dict[str, str]:
        """Stop and delete a subscription (it is removed, not paused; use
        update_subscription(active=False) to pause instead)."""
        store = self._subscriptions()
        get_owned(store, subscription_id, _owner_from_context(ctx))
        store.delete(subscription_id)
        return {"unsubscribed": subscription_id}

    # --- Library import: what the principal follows and has saved ------------

    def share_links(
        self,
        links: list[str] | None = None,
        text: str | None = None,
        ctx: Context | None = None,
    ) -> ShareResult:
        """Save episodes or shows the principal shared: Apple Podcasts,
        Spotify or YouTube links (open.spotify.com/episode/..., youtu.be/...,
        podcasts.apple.com/...?i=...), passed as `links` or inside free
        `text`. No sign-in anywhere: titles come from the links and Spotify
        episodes are matched to the show's public feed. Saved episodes join
        the saved queue; shared shows become suggestions. Returns each link's
        status, unrecognized links with reasons, and the library preview."""
        request = ShareRequest(links=links or [], text=text)
        return self._library_service().share(_owner_from_context(ctx), request)

    def import_file(
        self, format: FileFormat, content: str, ctx: Context | None = None
    ) -> FileImportResult:
        """Import followed shows from an export file's text: "youtube_takeout"
        (Google Takeout > YouTube > subscriptions/subscriptions.csv) or "opml"
        (Overcast, Pocket Casts, Castro, an Apple Podcasts Shortcut). Every
        followed show or channel becomes an explicit suggestion in the
        returned preview."""
        request = ImportFileRequest(format=format, content=content)
        return self._library_service().import_file(_owner_from_context(ctx), request)

    def import_library(self, items: list[LibraryItem], ctx: Context | None = None) -> ImportPreview:
        """Import shows and saved episodes the principal already has in other
        apps, fetched with YOUR OWN connectors (Chorus holds no token for
        them). Each item: {"provider": "readwise"|"spotify"|"apple"|
        "instapaper"|"opml"|"pushed", "item_kind": "episode"|"show"|"document",
        "title", optional "show_title", "url", "feed_url", "guid", "saved_at"
        (ISO 8601 with offset), "consumed", "tags", "highlights", "notes",
        "external_id"}. For a Readwise Reader podcast document: title=title,
        show_title=the show name, url=source_url, external_id=id,
        saved_at=saved_at, tags=tag names, consumed=(location == "archive" or
        reading_progress >= 0.9). Apple Podcasts links are resolved to the
        show's feed and the exact episode. Up to 500 items per call;
        re-importing is idempotent. Returns ranked suggested_sources, a
        saved_queue_source to add to a subscription's sources, and anything
        still pending (call again to finish)."""
        return self._library_service().import_items(
            _owner_from_context(ctx), LibraryImport(items=items)
        )

    def import_opml(self, opml: str) -> OpmlImport:
        """Turn an OPML subscription export (Overcast, Pocket Casts, Castro,
        an Apple Podcasts Shortcut) into RSS sources ready for
        preview_subscription and subscribe, plus the outlines it skipped."""
        return parse_opml(opml)

    def list_library_items(
        self,
        status: ResolutionStatus | None = None,
        limit: int = LIST_DEFAULT_LIMIT,
        ctx: Context | None = None,
    ) -> list[SavedItem]:
        """List imported library items newest first, optionally only one
        status: "resolved", "pending", "unresolved" or "corpus"."""
        return self._library_service().list_items(_owner_from_context(ctx), status, limit)

    def soul_from_library(self, ctx: Context | None = None) -> LibrarySoul:
        """Propose a soul.md from the imported library (titles, tags, notes,
        highlights). Nothing is saved: show it to the principal, merge it with
        their current soul if they have one, then use it in subscribe."""
        return self._library_service().soul(_owner_from_context(ctx))

    def _submit(self, request: DigestRequest, ctx: Context | None) -> dict[str, str]:
        owner = _owner_from_context(ctx)
        try:
            enforce_job_quota(self._store, owner)
        except QuotaExceeded as err:
            raise ValueError(str(err)) from err
        job_id = self._store.create(owner=owner)
        try:
            self._runner.submit(job_id, request, background=None)
        except Exception as err:
            job = self._store.get(job_id)
            if job is not None:
                job.status = JobStatus.failed
                job.error = f"dispatch failed: {type(err).__name__}: {err}"
                self._store.save(job)
            raise MCPSubmitError(job_id, f"{type(err).__name__}: {err}") from err
        return {"job_id": job_id}


def create_mcp_server(
    store: JobStore,
    deps: Deps,
    runner: JobRunner | None = None,
    subscription_store: SubscriptionStore | None = None,
    podcasts: PodcastDirectory | None = None,
    library: LibraryService | None = None,
) -> tuple[FastMCP, ChorusTools]:
    """Create one MCP server and expose its direct-call implementation for tests."""
    server = FastMCP(
        MCP_NAME,
        instructions=MCP_INSTRUCTIONS,
        # This value does not bind a socket for a mounted ASGI app. It prevents
        # FastMCP from applying its localhost-only Host allowlist in production.
        host="0.0.0.0",
        json_response=True,
    )
    tools = ChorusTools(store, deps, runner, subscription_store, podcasts, library)
    server.add_tool(tools.list_shows)
    server.add_tool(tools.submit_digest)
    server.add_tool(tools.submit_selection)
    server.add_tool(tools.get_digest)
    server.add_tool(tools.build_soul_from_interview)
    # Tools that fetch feeds or call Apple/YouTube run off the event loop.
    server.add_tool(_in_thread(tools.search_podcasts))
    server.add_tool(_in_thread(tools.resolve_podcast))
    server.add_tool(_in_thread(tools.preview_subscription))
    server.add_tool(tools.subscribe)
    server.add_tool(tools.list_subscriptions)
    server.add_tool(tools.update_subscription)
    server.add_tool(tools.unsubscribe)
    server.add_tool(_in_thread(tools.share_links))
    server.add_tool(_in_thread(tools.import_file))
    server.add_tool(_in_thread(tools.import_library))
    server.add_tool(tools.import_opml)
    server.add_tool(tools.list_library_items)
    server.add_tool(_in_thread(tools.soul_from_library))
    return server, tools


def mount_mcp(
    app: FastAPI,
    store: JobStore,
    deps: Deps,
    runner: JobRunner | None = None,
    subscription_store: SubscriptionStore | None = None,
    podcasts: PodcastDirectory | None = None,
    library: LibraryService | None = None,
) -> None:
    """Mount Streamable HTTP at /mcp and compose its lifespan into FastAPI."""
    server, tools = create_mcp_server(store, deps, runner, subscription_store, podcasts, library)
    mcp_app = server.streamable_http_app()
    parent_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(host_app: FastAPI) -> AsyncIterator[None]:
        async with parent_lifespan(host_app), server.session_manager.run():
            yield

    app.router.lifespan_context = lifespan
    # Keep FastMCP's internal /mcp route. Mounting at / avoids a /mcp/ redirect;
    # callers must add this catch-all mount after every application route.
    app.mount("/", mcp_app, name="mcp")
    app.state.chorus_mcp_server = server
    app.state.chorus_mcp_tools = tools


def local_stdio_deps() -> Deps:
    """`default_deps` with every on-disk store under `~/.chorus/`. Providers
    are still picked by key presence, as for the hosted API; the onboarding
    choices take over this selection when the MCP onboarding tools land."""
    from chorus import paths
    from chorus.artifacts import LocalArtifactStore
    from chorus.audio import get_audio_renderer
    from chorus.config_env import build_transcript_chain
    from chorus.llm import get_llm_client
    from chorus.script import get_script_composer
    from chorus.transcript_cache import SqliteTranscriptCache

    return Deps(
        provider=build_transcript_chain(SqliteTranscriptCache(paths.db_path())),
        llm=get_llm_client(),
        composer=get_script_composer(),
        renderer=get_audio_renderer(),
        artifacts=LocalArtifactStore(paths.artifacts_dir()),
    )


def main() -> None:
    """Run the local stdio transport used by desktop and CLI agents. No HTTP
    request exists on this transport, so every call runs as MASTER_OWNER
    (see `_owner_from_context`) — equivalent to holding the master token,
    appropriate for local/dev tooling.

    State (job and subscription database, transcript cache, artifacts) lives
    under `~/.chorus/` (`chorus.paths`), never beside the code, so upgrading
    an installed package or pulling a clone cannot touch it."""
    from chorus import paths
    from chorus.config import load_env

    paths.ensure_home()
    load_env()
    store = SqliteJobStore(paths.db_path())
    deps = local_stdio_deps()
    server, tools = create_mcp_server(store, deps, BackgroundRunner(store, deps))
    try:
        server.run(transport="stdio")
    finally:
        deps.close()
        store.close()
        tools.close()
        if tools._subscription_store is not None:
            tools._subscription_store.close()


if __name__ == "__main__":
    main()
