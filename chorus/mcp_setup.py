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

from chorus import agent_setup, host_mode
from chorus.jobs import JobStore
from chorus.onboarding import Brain, Voice, load_config

LOCAL_INSTRUCTIONS = (
    "Chorus curates the podcasts a principal cannot get to and returns cited highlights plus a "
    "short voiced episode. Start every session with onboarding_status. If ready is false, walk "
    "the principal through `next`: ask them its `ask` text, follow its agent_notes, call the "
    "matching onboarding_* tool, and repeat until ready. The soul step is required. Once ready, "
    "run_my_digest starts this week's digest. If it returns drive='host_next', you do part "
    "of the work: loop on host_next(job_id) and do what each task says (wait, score an "
    "episode, write the script, voice the audio) until it is done. Otherwise poll "
    "get_digest(job_id) until done or failed."
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

    def onboarding_smoke_test(run: bool = True, agent_model: str | None = None) -> dict[str, Any]:
        """Run (or skip) one test digest over a bundled sample transcript with
        the chosen brain and voice. May make billed calls; ask first. With the
        host brain it returns a host_job_id you drive with host_next; pass
        agent_model (the model you are)."""
        return agent_setup.smoke_test(run, agent_model=agent_model)

    def run_my_digest(
        episode_ids: list[str] | None = None,
        format: str = "monologue",
        agent_model: str | None = None,
    ) -> dict[str, str]:
        """Start a digest with the principal's saved soul and choices, over this
        week's shows and feeds (or the given episode ids). Returns {"job_id",
        "brain"} immediately, plus drive='host_next' when you do part of the
        work (your model thinks, or your voice tool renders): then loop on
        host_next(job_id). Otherwise poll get_digest. format: 'monologue' or
        'dialogue' (two hosts; host brain only). agent_model: the model you are
        (host brain only; recorded with the run). Refuses until setup is done."""
        return submit_configured_digest(store, episode_ids, format, agent_model)

    def host_next(job_id: str) -> dict[str, Any]:
        """Host brain: what to do now for this run. kind is wait (call again
        after wait_seconds), score (score episode windows, then
        host_submit_scores), script (write takes/turns, then
        host_submit_script), done (deliver `result`), or failed."""
        return host_mode.next_task(store, job_id).model_dump(mode="json")

    def host_episode(job_id: str, episode_id: str) -> dict[str, Any]:
        """Host brain: one episode's windows, for a subagent scoring it in
        parallel with the others listed in pending_episodes."""
        return host_mode.episode_windows(job_id, episode_id)

    def host_submit_scores(
        job_id: str, episode_id: str, scores: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """Host brain: submit one episode's window scores as
        [{"i": <window index>, "score": <0.0-1.0>, "reason": "<one line>"}].
        Returns the episode outcome and the next task."""
        return host_mode.submit_scores(store, job_id, episode_id, scores)

    def host_submit_script(
        job_id: str, takes: list[dict[str, Any]], turns: list[dict[str, Any]] | None = None
    ) -> dict[str, Any]:
        """Host brain: submit the script. takes: [{"ref", "take_type", "text"}];
        turns (two-host only): [{"ref", "speaker", "text"}]. Items without a
        valid highlight ref are dropped and listed. Renders the episode."""
        return host_mode.submit_script(store, load_config(), job_id, takes, turns)

    def host_submit_audio(
        job_id: str,
        chunks: list[dict[str, Any]] | None = None,
        skip_reason: str | None = None,
    ) -> dict[str, Any]:
        """Agent voice: submit every chunk of the render plan you voiced, as
        [{"index": <chunk index>, "path": "<mp3 file>"}] (or "base64" instead
        of "path"). Chorus checks they are MP3 and joins them in order. If you
        could not voice them, pass skip_reason instead; the digest is still
        delivered with the script as text."""
        return host_mode.submit_audio(store, job_id, chunks, skip_reason)

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
        host_next,
        host_episode,
        host_submit_scores,
        host_submit_script,
        host_submit_audio,
    ):
        server.add_tool(tool)


def submit_configured_digest(
    store: JobStore,
    episode_ids: list[str] | None,
    episode_format: str = "monologue",
    agent_model: str | None = None,
) -> dict[str, str]:
    from chorus.brains import BrainConfigError, build_local_deps
    from chorus.local_run import (
        HIGHLIGHTS_PER_EPISODE,
        read_context,
        recent_episodes,
    )
    from chorus.models import DigestRequest, EpisodeInput
    from chorus.onboarding import OnboardingError, status
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
    if config.brain is Brain.host or config.voice is Voice.host_plugin:
        job_id = host_mode.start(
            store, config, episodes, episode_format=episode_format, brain_model=agent_model
        )
        brain_name = config.brain.value if config.brain else "unknown"
        return {"job_id": job_id, "brain": brain_name, "drive": "host_next"}
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
    brain = config.brain.value if config.brain else "unknown"

    def work() -> None:
        # Unlike the shared server deps, these were built for this run alone.
        try:
            run_job(job_id, request, store, deps)
        finally:
            deps.close()

    threading.Thread(target=work, name=f"chorus-run-my-digest-{job_id}", daemon=True).start()
    return {"job_id": job_id, "brain": brain}
