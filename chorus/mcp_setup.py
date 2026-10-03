"""Onboarding and configured-run tools for the LOCAL stdio MCP server.

Registered only by `chorus.mcp_server.main` (stdio, the principal's own
machine). They write API keys and souls under `~/.chorus/`, so they must
never be mounted on the hosted Streamable HTTP endpoint; `create_mcp_server`
only adds them when called with `local=True`.

Each tool is a thin wrapper over `chorus.agent_setup`, the same layer the
`chorus setup` JSON CLI uses for hosts without MCP.
"""
from __future__ import annotations

import threading
from typing import Any

from mcp.server.fastmcp import FastMCP  # type: ignore[import-not-found,import-untyped]

from chorus import agent_setup
from chorus.jobs import JobStore

LOCAL_INSTRUCTIONS = (
    "Chorus curates the podcasts a principal cannot get to and returns cited highlights plus a "
    "short voiced episode. Start every session with onboarding_status. If ready is false, walk "
    "the principal through `next`: ask them its `ask` text, follow its agent_notes, call the "
    "matching onboarding_* tool, and repeat until ready. The soul step is required. Once ready, "
    "run_my_digest starts this week's digest; poll get_digest(job_id) until done or failed."
)


def register_setup_tools(server: FastMCP, store: JobStore) -> None:
    def onboarding_status() -> dict[str, Any]:
        """Where setup stands and, if not ready, the next step to walk the
        principal through (what to ask, the options, and how to proceed)."""
        return agent_setup.agent_status().model_dump(mode="json")

    def onboarding_options(step: str) -> dict[str, Any]:
        """The prompt and options for any step, e.g. to revisit 'brain'.
        Steps: mode, brain, voice, transcripts, keys, soul, shows, updates, smoke_test."""
        return agent_setup.step_options(step).model_dump(mode="json")

    def onboarding_set(step: str, value: str) -> dict[str, Any]:
        """Record the principal's answer to a choice step (mode, brain, voice,
        transcripts, updates). `value` is an option's `value`."""
        return agent_setup.set_choice(step, value).model_dump(mode="json")

    def onboarding_set_key(env: str, value: str) -> dict[str, Any]:
        """Store one API key in ~/.chorus/.env on this machine. Never echo the
        value back to the principal."""
        return agent_setup.set_key(env, value).model_dump(mode="json")

    def onboarding_soul_draft(
        source: str,
        answers: dict[str, str] | None = None,
        texts: list[str] | None = None,
        preset: str | None = None,
        markdown: str | None = None,
    ) -> dict[str, Any]:
        """Draft (without saving) a soul. source: 'interview' + answers keyed
        identity/interests/triggers/ignore/style/guidance; 'corpus' + texts;
        'preset' + preset; 'write' or 'file' + markdown to validate."""
        return agent_setup.soul_draft(source, answers, texts, preset, markdown).model_dump(
            mode="json"
        )

    def onboarding_soul_save(name: str, markdown: str) -> dict[str, Any]:
        """Save the soul the principal approved, as ~/.chorus/souls/<name>.md."""
        return agent_setup.soul_save(name, markdown).model_dump(mode="json")

    def onboarding_soul_show() -> dict[str, Any]:
        """The principal's current soul, to review or revise."""
        return agent_setup.soul_show()

    def onboarding_set_shows(
        shows: list[str] | None = None, feeds: list[str] | None = None, weekly: bool = False
    ) -> dict[str, Any]:
        """Set followed catalog shows and RSS feed URLs, and whether to digest weekly."""
        return agent_setup.set_shows(shows or [], feeds or [], weekly).model_dump(mode="json")

    def onboarding_smoke_test(run: bool = True) -> dict[str, Any]:
        """Run (or skip) one test digest over a bundled sample transcript with
        the chosen brain and voice. May make billed calls; ask first."""
        return agent_setup.smoke_test(run)

    def run_my_digest(episode_ids: list[str] | None = None) -> dict[str, str]:
        """Start a digest with the principal's saved soul and choices, over this
        week's shows and feeds (or the given episode ids). Returns {"job_id"}
        immediately; poll get_digest. Refuses until onboarding is complete."""
        return submit_configured_digest(store, episode_ids)

    for tool in (
        onboarding_status,
        onboarding_options,
        onboarding_set,
        onboarding_set_key,
        onboarding_soul_draft,
        onboarding_soul_save,
        onboarding_soul_show,
        onboarding_set_shows,
        onboarding_smoke_test,
        run_my_digest,
    ):
        server.add_tool(tool)


def submit_configured_digest(store: JobStore, episode_ids: list[str] | None) -> dict[str, str]:
    from chorus.brains import BrainConfigError, build_local_deps
    from chorus.local_run import (
        HIGHLIGHTS_PER_EPISODE,
        read_context,
        recent_episodes,
    )
    from chorus.models import DigestRequest, EpisodeInput
    from chorus.onboarding import OnboardingError, load_config, status
    from chorus.pipeline import run_job
    from chorus.soul import load_soul

    config = load_config()
    current = status(config)
    if not current.ready:
        raise OnboardingError(
            f"setup is incomplete (next step: {current.next_step}); call onboarding_status"
        )
    episodes = (
        [EpisodeInput(video_id=i) for i in episode_ids] if episode_ids else recent_episodes(config)
    )
    if not episodes:
        raise OnboardingError("no new episodes in the past week from the followed shows and feeds")
    try:
        deps = build_local_deps(config)
    except BrainConfigError as err:
        raise OnboardingError(str(err)) from err
    request = DigestRequest(
        soul=load_soul(config.soul or ""),
        context=read_context(),
        episodes=episodes,
        highlight_count=HIGHLIGHTS_PER_EPISODE,
    )
    job_id = store.create()

    def work() -> None:
        # Unlike the shared server deps, these were built for this run alone.
        try:
            run_job(job_id, request, store, deps)
        finally:
            deps.close()

    threading.Thread(target=work, name=f"chorus-run-my-digest-{job_id}", daemon=True).start()
    return {"job_id": job_id}
