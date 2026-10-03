"""Run a digest synchronously for a local install (`chorus run`, the onboarding
smoke test). Same pipeline as the API (`chorus.pipeline.run_job`), with the
job store, transcript cache and artifacts under `~/.chorus/`."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from chorus import paths
from chorus.brains import build_local_deps
from chorus.jobs import SqliteJobStore
from chorus.models import DigestRequest, EpisodeInput, Job
from chorus.onboarding import OnboardingConfig, OnboardingError, status
from chorus.pipeline import Deps, run_job
from chorus.soul import load_soul

SMOKE_EPISODE = "sample_public"
CONTEXT_FILENAME = "context.md"
LOOKBACK_DAYS = 7
MAX_EPISODES_PER_RUN = 8
HIGHLIGHTS_PER_EPISODE = 4


def context_path() -> Path:
    return paths.home() / CONTEXT_FILENAME


def read_context() -> str:
    """Optional free-text "what I'm working on this week" that tunes relevance."""
    target = context_path()
    return target.read_text(encoding="utf-8") if target.is_file() else ""


def recent_episodes(config: OnboardingConfig, now: datetime | None = None) -> list[EpisodeInput]:
    """The past week's episodes across the configured shows and feeds, picked
    fairly across sources (`chorus.feeds.round_robin`)."""
    from chorus.feeds import gather_episodes, round_robin
    from chorus.subscriptions import RssSource, ShowSource, Source

    sources: list[Source] = [ShowSource(kind="show", show=s) for s in config.shows]
    sources += [RssSource(kind="rss", feed_url=f) for f in config.feeds]
    since = (now or datetime.now(UTC)) - timedelta(days=LOOKBACK_DAYS)
    gathered = gather_episodes(sources, since, MAX_EPISODES_PER_RUN)
    return [fe.episode for fe in round_robin(gathered.per_source, MAX_EPISODES_PER_RUN)]


def run_digest(
    config: OnboardingConfig,
    episodes: list[EpisodeInput],
    *,
    require_ready: bool = True,
    deps: Deps | None = None,
) -> Job:
    if require_ready:
        current = status(config)
        if not current.ready:
            raise OnboardingError(
                f"onboarding is incomplete (next step: {current.next_step}); run `chorus onboard`"
            )
    if not config.soul:
        raise OnboardingError("no soul configured; run `chorus onboard`")
    if not episodes:
        raise OnboardingError("no episodes to digest")
    request = DigestRequest(
        soul=load_soul(config.soul),
        context=read_context(),
        episodes=episodes,
        highlight_count=HIGHLIGHTS_PER_EPISODE,
        soul_origin="supplied",
    )
    paths.ensure_home()
    store = SqliteJobStore(paths.db_path())
    owned_deps = deps is None
    active = deps or build_local_deps(config)
    try:
        job_id = store.create()
        run_job(job_id, request, store, active)
        job = store.get(job_id)
        if job is None:
            raise RuntimeError(f"job {job_id} vanished from {paths.db_path()}")
        return job
    finally:
        if owned_deps:
            active.close()
        store.close()


def smoke_test(config: OnboardingConfig, deps: Deps | None = None) -> Job:
    """One digest over the bundled synthetic transcript, through the chosen
    brain and voice. Runs before onboarding is marked complete."""
    return run_digest(
        config, [EpisodeInput(video_id=SMOKE_EPISODE)], require_ready=False, deps=deps
    )


def artifact_file(job: Job) -> Path | None:
    """Local path of the job's rendered audio (or text placeholder)."""
    if not job.audio_url:
        return None
    name = job.audio_url.rsplit("/", 1)[-1]
    candidate = paths.artifacts_dir() / name
    return candidate if candidate.is_file() else None
