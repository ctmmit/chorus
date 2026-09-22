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

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import cast

from fastapi import FastAPI
from mcp.server.fastmcp import Context, FastMCP  # type: ignore[import-not-found,import-untyped]

from chorus import catalog
from chorus.bootstrap import get_soul_builder
from chorus.jobs import JobStore, MASTER_OWNER, SqliteJobStore
from chorus.models import DigestRequest, EpisodeInput, EpisodeProfile, Job, JobStatus, SelectionRequest
from chorus.pipeline import Deps, default_deps
from chorus.quotas import QuotaExceeded, enforce_job_quota
from chorus.runners import BackgroundRunner, JobRunner

MCP_NAME = "Chorus"
MCP_INSTRUCTIONS = (
    "Build persona-conditioned podcast digests. List shows, submit episodes or a catalog "
    "selection, then poll the returned job id (get_digest) until done or failed — submission "
    "returns immediately and does not itself wait for the pipeline to finish."
)


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

    def __init__(self, store: JobStore, deps: Deps, runner: JobRunner | None = None) -> None:
        self._store = store
        self._deps = deps
        # Defaults to a BackgroundRunner over `store`/`deps` so direct callers
        # (tests, the local stdio transport) work with zero extra wiring;
        # chorus.app.create_app always passes the app's own selected runner.
        self._runner = runner or BackgroundRunner(store, deps)

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

    def _submit(self, request: DigestRequest, ctx: Context | None) -> dict[str, str]:
        owner = _owner_from_context(ctx)
        try:
            enforce_job_quota(self._store, owner)
        except QuotaExceeded as err:
            raise ValueError(str(err)) from err
        job_id = self._store.create(owner=owner)
        try:
            self._runner.submit(job_id, request, background=None)
        except Exception as err:  # noqa: BLE001 - R8: never strand a queued job
            job = self._store.get(job_id)
            if job is not None:
                job.status = JobStatus.failed
                job.error = f"dispatch failed: {type(err).__name__}: {err}"
                self._store.save(job)
            raise MCPSubmitError(job_id, f"{type(err).__name__}: {err}") from err
        return {"job_id": job_id}


def create_mcp_server(
    store: JobStore, deps: Deps, runner: JobRunner | None = None
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
    tools = ChorusTools(store, deps, runner)
    server.add_tool(tools.list_shows)
    server.add_tool(tools.submit_digest)
    server.add_tool(tools.submit_selection)
    server.add_tool(tools.get_digest)
    server.add_tool(tools.build_soul_from_interview)
    return server, tools


def mount_mcp(app: FastAPI, store: JobStore, deps: Deps, runner: JobRunner | None = None) -> None:
    """Mount Streamable HTTP at /mcp and compose its lifespan into FastAPI."""
    server, tools = create_mcp_server(store, deps, runner)
    mcp_app = server.streamable_http_app()
    parent_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(host_app: FastAPI) -> AsyncIterator[None]:
        async with parent_lifespan(host_app):
            async with server.session_manager.run():
                yield

    app.router.lifespan_context = lifespan
    # Keep FastMCP's internal /mcp route. Mounting at / avoids a /mcp/ redirect;
    # callers must add this catch-all mount after every application route.
    app.mount("/", mcp_app, name="mcp")
    app.state.chorus_mcp_server = server
    app.state.chorus_mcp_tools = tools


def main() -> None:
    """Run the local stdio transport used by desktop and CLI agents. No HTTP
    request exists on this transport, so every call runs as MASTER_OWNER
    (see `_owner_from_context`) — equivalent to holding the master token,
    appropriate for local/dev tooling."""
    from chorus.config import load_env

    load_env()
    store = SqliteJobStore()
    deps = default_deps()
    server, _ = create_mcp_server(store, deps, BackgroundRunner(store, deps))
    try:
        server.run(transport="stdio")
    finally:
        deps.close()
        store.close()


if __name__ == "__main__":
    main()
