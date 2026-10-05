"""Run a digest synchronously for a local install (`chorus run`, the onboarding
smoke test). Same pipeline as the API (`chorus.pipeline.run_job`), with the
job store, transcript cache and artifacts under `~/.chorus/`."""
from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

from chorus import paths
from chorus.brains import build_local_deps
from chorus.context import ContextProvider, ReadwiseContextProvider
from chorus.jobs import SqliteJobStore
from chorus.models import ContextBlock, DigestRequest, EpisodeInput, Job
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


READWISE_TOKEN_ENV = "READWISE_TOKEN"


def local_context_blocks(
    now: datetime, providers: list[ContextProvider] | None = None
) -> tuple[list[ContextBlock], list[str]]:
    """Context pulled from sources the principal connected locally: today,
    Readwise highlights from the past week when READWISE_TOKEN is set. A
    source that fails is reported, never fatal (chorus/context.py)."""
    if providers is None:
        token = os.environ.get(READWISE_TOKEN_ENV)
        providers = [ReadwiseContextProvider(token)] if token else []
    blocks: list[ContextBlock] = []
    problems: list[str] = []
    for provider in providers:
        try:
            block = provider.fetch(now - timedelta(days=LOOKBACK_DAYS))
            if block.items:
                blocks.append(block)
        except Exception as err:  # noqa: BLE001 - context is optional
            problems.append(f"context source unavailable: {type(err).__name__}: {err}")
        finally:
            close = getattr(provider, "close", None)
            if callable(close):
                close()
    return blocks, problems


def run_digest(
    config: OnboardingConfig,
    episodes: list[EpisodeInput],
    *,
    require_ready: bool = True,
    deps: Deps | None = None,
    context_providers: list[ContextProvider] | None = None,
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
    blocks, context_problems = local_context_blocks(datetime.now(UTC), context_providers)
    request = DigestRequest(
        soul=load_soul(config.soul),
        context=read_context(),
        episodes=episodes,
        highlight_count=HIGHLIGHTS_PER_EPISODE,
        soul_origin="supplied",
        context_blocks=blocks,
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
        if context_problems:
            job.warnings.extend(context_problems)
            store.save(job)
        return job
    finally:
        if owned_deps:
            active.close()
        store.close()


def smoke_test(config: OnboardingConfig, deps: Deps | None = None) -> Job:
    """One digest over the bundled synthetic transcript, through the chosen
    brain and voice. Runs before onboarding is marked complete."""
    return run_digest(
        config,
        [EpisodeInput(video_id=SMOKE_EPISODE)],
        require_ready=False,
        deps=deps,
        context_providers=[],  # the smoke test stays offline apart from the chosen brain
    )


DIGESTS_DIRNAME = "digests"


def write_digest_markdown(job: Job) -> Path:
    """`~/.chorus/digests/<date>-<job>.md`: what a scheduled run leaves behind
    for the principal to read, with every highlight's timestamp and quote."""
    from datetime import UTC, datetime

    lines = [f"# Chorus digest, {datetime.now(UTC).strftime('%d %b %Y')}", ""]
    if job.error:
        lines += [f"Run failed: {job.error}", ""]
    for episode in job.digest.episodes if job.digest else []:
        lines.append(f"## {episode.episode_title or episode.episode_id}")
        if episode.refused:
            lines += [f"Nothing surfaced ({episode.refusal_reason}).", ""]
            continue
        for h in episode.highlights:
            minutes, seconds = divmod(int(h.segment_timestamp), 60)
            lines.append(f"- **{minutes}:{seconds:02d}** \"{h.quote}\"  ")
            lines.append(f"  {h.why_surface}")
        lines.append("")
    audio = artifact_file(job)
    if audio is not None:
        lines += [f"Episode: {audio}", ""]
    lines += [f"Note: {w}" for w in job.warnings]
    target = paths.home() / DIGESTS_DIRNAME
    target.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y-%m-%d")
    out = target / f"{stamp}-{job.job_id[:8]}.md"
    out.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return out


def artifact_file(job: Job) -> Path | None:
    """Local path of the job's rendered audio (or text placeholder)."""
    if not job.audio_url:
        return None
    name = job.audio_url.rsplit("/", 1)[-1]
    candidate = paths.artifacts_dir() / name
    return candidate if candidate.is_file() else None
