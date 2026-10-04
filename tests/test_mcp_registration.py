"""The MCP tool surface, pinned: which tools each server exposes, in what
order, and which run off the event loop (`_in_thread`).

Registration lives in per-feature `register_*_tools` functions; this test
fails if moving a tool between them changes what a client sees.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from chorus.audio import MockAudioRenderer
from chorus.jobs import SqliteJobStore
from chorus.llm import MockLLMClient
from chorus.mcp_server import create_mcp_server
from chorus.pipeline import Deps
from chorus.script import MockScriptComposer
from chorus.transcripts import FixtureTranscriptProvider

# (name, runs off the event loop)
HOSTED_TOOLS = [
    ("list_shows", False),
    ("submit_digest", False),
    ("submit_selection", False),
    ("get_digest", False),
    ("build_soul_from_interview", False),
    ("search_podcasts", True),
    ("resolve_podcast", True),
    ("preview_subscription", True),
    ("subscribe", False),
    ("list_subscriptions", False),
    ("update_subscription", False),
    ("unsubscribe", False),
    ("share_links", True),
    ("import_file", True),
    ("import_library", True),
    ("import_opml", False),
    ("list_library_items", False),
    ("soul_from_library", True),
    ("get_podcast_feed", False),
    ("rate_highlight", False),
    ("propose_soul_update", False),
    ("apply_soul_update", False),
    ("clear_memory", False),
]


def _tools(tmp_path: Path, local: bool) -> list[tuple[str, bool]]:
    store = SqliteJobStore(tmp_path / "jobs.db")
    deps = Deps(
        FixtureTranscriptProvider(), MockLLMClient(), MockScriptComposer(), MockAudioRenderer()
    )
    server, tools = create_mcp_server(store, deps, local=local)
    try:
        registered = server._tool_manager.list_tools()
        return [(t.name, t.is_async) for t in registered]
    finally:
        tools.close()
        store.close()


def test_hosted_server_exposes_exactly_the_hosted_tools(tmp_path: Path) -> None:
    assert _tools(tmp_path, local=False) == HOSTED_TOOLS


@pytest.mark.usefixtures("chorus_home")
def test_local_server_adds_setup_tools_after_the_hosted_ones(tmp_path: Path) -> None:
    local = _tools(tmp_path, local=True)
    assert local[: len(HOSTED_TOOLS)] == HOSTED_TOOLS
    added = {name for name, _ in local[len(HOSTED_TOOLS) :]}
    assert {"onboarding_status", "onboarding_voices", "run_my_digest", "host_next"} <= added
