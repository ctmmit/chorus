"""MCP distribution surface for local agents and the hosted FastAPI app."""
from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import cast

from fastapi import FastAPI
from mcp.server.fastmcp import FastMCP  # type: ignore[import-not-found,import-untyped]

from chorus import catalog
from chorus.bootstrap import get_soul_builder
from chorus.jobs import JobStore, SqliteJobStore
from chorus.models import DigestRequest, EpisodeInput, Job, SelectionRequest
from chorus.pipeline import Deps, default_deps, run_job

MCP_NAME = "Chorus"
MCP_INSTRUCTIONS = (
    "Build persona-conditioned podcast digests. List shows, submit episodes or a catalog "
    "selection, then poll the returned job id until done or failed."
)


class ChorusTools:
    """Directly callable tool implementation with injected storage and providers."""

    def __init__(self, store: JobStore, deps: Deps) -> None:
        self._store = store
        self._deps = deps

    def list_shows(self) -> list[dict[str, object]]:
        """List catalog shows and their currently selectable episodes."""
        return cast(list[dict[str, object]], catalog.list_shows())

    def submit_digest(
        self,
        soul: str,
        context: str,
        episodes: list[EpisodeInput],
        highlight_count: int = 4,
    ) -> dict[str, str]:
        """Submit explicit episodes and return the job id to poll."""
        request = DigestRequest(
            soul=soul,
            context=context,
            episodes=episodes,
            highlight_count=highlight_count,
        )
        return self._submit(request)

    def submit_selection(
        self,
        soul: str,
        context: str,
        shows: list[str] | None = None,
        video_ids: list[str] | None = None,
        highlight_count: int = 4,
    ) -> dict[str, str]:
        """Resolve catalog shows or video ids, submit them, and return a job id."""
        selection = SelectionRequest(
            soul=soul,
            context=context,
            shows=shows,
            video_ids=video_ids,
            highlight_count=highlight_count,
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
        )
        return self._submit(request)

    def get_digest(self, job_id: str) -> Job:
        """Return the current job, including digest, audio URL, warnings, or error."""
        job = self._store.get(job_id)
        if job is None:
            raise ValueError("unknown job_id")
        return job

    def build_soul_from_interview(self, answers: dict[str, str]) -> str:
        """Build soul.md from the interview answer keys documented in the skill."""
        return get_soul_builder().build_from_interview(answers)

    def _submit(self, request: DigestRequest) -> dict[str, str]:
        job_id = self._store.create()
        run_job(job_id, request, self._store, self._deps)
        return {"job_id": job_id}


def create_mcp_server(store: JobStore, deps: Deps) -> tuple[FastMCP, ChorusTools]:
    """Create one MCP server and expose its direct-call implementation for tests."""
    server = FastMCP(
        MCP_NAME,
        instructions=MCP_INSTRUCTIONS,
        # This value does not bind a socket for a mounted ASGI app. It prevents
        # FastMCP from applying its localhost-only Host allowlist in production.
        host="0.0.0.0",
        json_response=True,
    )
    tools = ChorusTools(store, deps)
    server.add_tool(tools.list_shows)
    server.add_tool(tools.submit_digest)
    server.add_tool(tools.submit_selection)
    server.add_tool(tools.get_digest)
    server.add_tool(tools.build_soul_from_interview)
    return server, tools


def mount_mcp(app: FastAPI, store: JobStore, deps: Deps) -> None:
    """Mount Streamable HTTP at /mcp and compose its lifespan into FastAPI."""
    server, tools = create_mcp_server(store, deps)
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
    """Run the local stdio transport used by desktop and CLI agents."""
    from chorus.config import load_env

    load_env()
    store = SqliteJobStore()
    server, _ = create_mcp_server(store, default_deps())
    try:
        server.run(transport="stdio")
    finally:
        store.close()


if __name__ == "__main__":
    main()
