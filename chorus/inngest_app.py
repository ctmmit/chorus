"""Inngest durable-step orchestrator for the digest pipeline (Phase B,
docs/DEVELOPMENT_PLAN.md §5): the Vercel replacement for FastAPI
BackgroundTasks, whose in-process job dies the instant a Vercel function is
frozen after responding.

`run_digest` is triggered by the `chorus/digest.requested` event that
chorus.runners.InngestRunner.submit sends. Its body calls the SAME stage
functions from chorus/pipeline.py (`stage_ingest`, `stage_curate_episode`,
`stage_script`, `stage_audio`) that the BackgroundRunner path calls, one per
`step.run` — including one `curate:<episode_id>` step PER resolved episode,
so a single episode's transient failure retries alone rather than re-running
the whole digest. Between steps the Job is loaded/saved through the store,
exactly mirroring `chorus.pipeline._run`'s status transitions and
non-fatal-degradation semantics (script/audio failures -> warnings, not a
failed job).

Verified 21 Sep 2026 against github.com/inngest/inngest-py and
inngest.com/docs/reference/python (no separately-versioned REST spec; the
SDK IS the documented surface):

    client = inngest.Inngest(app_id=..., event_key=..., signing_key=...)

    @client.create_function(
        fn_id="...",
        trigger=inngest.TriggerEvent(event="..."),
    )
    async def handler(ctx: inngest.Context) -> Any:
        result = await ctx.step.run("step-id", some_callable)
        ...

    inngest.fast_api.serve(app, client, [handler], serve_path="/api/inngest")

`ctx.event.data` is the event's JSON payload dict; `ctx.step.run(step_id, fn)`
runs `fn` durably (memoized — a retried function invocation replays already-
succeeded steps from their cached result instead of re-running them) and is
awaited. Each step's return value must be JSON-serializable, so every step
callable below dumps its Pydantic result with `.model_dump(mode="json")` and
the orchestrator reconstructs the model on the other side of the `await`.

`run_digest_body` (the part below the `@client.create_function` line) takes a
plain `step`-like object — anything with an async `run(step_id, fn)` — so it
is exercised directly in tests with a fake step and no dev server, no network,
and no real `inngest` package behavior beyond what we assume above.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any, Protocol

import inngest
import inngest.fast_api
from fastapi import FastAPI

from chorus.curation import soul_version
from chorus.jobs import JobStore
from chorus.models import (
    Digest,
    DigestRequest,
    EpisodeDigest,
    IngestResult,
    Job,
    JobStatus,
    JobUsage,
    ResolvedEpisode,
    Script,
)
from chorus.pipeline import (
    AllEpisodesFailed,
    Deps,
    stage_audio,
    stage_curate_episode,
    stage_ingest,
    stage_script,
)

log = logging.getLogger("chorus.inngest_app")

# The event chorus.runners.InngestRunner.submit sends and this module's
# function is triggered by.
INNGEST_DIGEST_EVENT = "chorus/digest.requested"
INNGEST_SERVE_PATH = "/api/inngest"


class StepLike(Protocol):
    async def run(self, step_id: str, handler: Any) -> Any: ...


def _save_job(store: JobStore, job: Job) -> dict[str, str]:
    """A step body: persists the job and returns a JSON-serializable marker
    (the step's return value must be JSON, and callers don't need it back —
    they already hold `job`)."""
    store.save(job)
    return {"status": job.status.value}


async def _execute(step: StepLike, event_data: dict[str, Any], store: JobStore, deps: Deps) -> None:
    job_id = event_data["job_id"]
    request = DigestRequest.model_validate(event_data["request"])

    job = store.get(job_id)
    if job is None:
        log.error("run_digest: unknown job_id %s", job_id)
        return

    usage = job.usage or JobUsage()
    job.usage = usage

    # Every step.run handler below is `async def`, even though the stage
    # functions themselves are plain sync calls: inngest's Step.run requires
    # an awaitable handler (it `await`s the callable's return value), so each
    # closure just awaits nothing and returns the sync call's result.

    async def _ingest() -> dict[str, Any]:
        return stage_ingest(request, deps.provider).model_dump(mode="json")

    t0 = time.perf_counter()
    try:
        ingested_data = await step.run("ingest", _ingest)
    except AllEpisodesFailed as err:
        usage.stage_seconds["ingest"] = time.perf_counter() - t0
        job.status = JobStatus.failed
        job.error = str(err)

        async def _finalize_failed() -> dict[str, str]:
            return _save_job(store, job)

        await step.run("finalize-failed", _finalize_failed)
        return
    usage.stage_seconds["ingest"] = time.perf_counter() - t0
    ingested = IngestResult.model_validate(ingested_data)
    usage.skipped = ingested.skipped
    usage.transcript_sources = {
        r.episode.resolved_id(): r.transcript.source or "unknown" for r in ingested.resolved
    }

    t0 = time.perf_counter()
    episode_digests: list[EpisodeDigest] = []
    for resolved in ingested.resolved:
        episode_id = resolved.episode.resolved_id()

        async def _curate(resolved: ResolvedEpisode = resolved) -> dict[str, Any]:
            return stage_curate_episode(resolved, request, deps.llm).model_dump(mode="json")

        data = await step.run(f"curate:{episode_id}", _curate)
        episode_digests.append(EpisodeDigest.model_validate(data))

    job.digest = Digest(
        soul_version=soul_version(request.soul),
        soul_origin=request.soul_origin,
        episodes=episode_digests,
    )
    usage.stage_seconds["curate"] = time.perf_counter() - t0
    job.status = JobStatus.digest_ready

    async def _save_digest_ready() -> dict[str, str]:
        return _save_job(store, job)

    await step.run("save-digest-ready", _save_digest_ready)

    t0 = time.perf_counter()
    try:
        digest = job.digest

        async def _script(digest: Digest = digest) -> dict[str, Any]:
            return stage_script(digest, request, deps.composer).model_dump(mode="json")

        script_data = await step.run("script", _script)
        job.script = Script.model_validate(script_data)
    except Exception as err:  # noqa: BLE001 - non-fatal: digest is still valid
        job.script = None
        job.warnings.append(f"script synthesis failed: {type(err).__name__}: {err}")
        log.warning("job %s: script synthesis failed (non-fatal): %s", job.job_id, err)
    usage.stage_seconds["script"] = time.perf_counter() - t0

    if job.script is not None:
        t0 = time.perf_counter()
        try:
            script = job.script

            async def _audio(script: Script = script) -> str:
                return stage_audio(script, request, job.job_id, deps.renderer, deps.artifacts)

            job.audio_url = await step.run("audio", _audio)
        except Exception as err:  # noqa: BLE001 - audio failure is non-fatal
            job.audio_url = None
            job.warnings.append(f"audio render failed: {type(err).__name__}: {err}")
            log.warning("job %s: audio render failed (non-fatal): %s", job.job_id, err)
        usage.stage_seconds["audio"] = time.perf_counter() - t0

    job.status = JobStatus.done

    async def _finalize_done() -> dict[str, str]:
        return _save_job(store, job)

    await step.run("finalize-done", _finalize_done)


async def run_digest_body(step: StepLike, event_data: dict[str, Any], store: JobStore, deps: Deps) -> None:
    """Terminal-state guarantee, mirroring chorus.pipeline.run_job: any
    unexpected exception ends the job `failed` with a reason rather than
    leaving it stuck, and does NOT re-raise — Inngest would otherwise retry
    the whole function, which cannot help with a non-transient failure (a
    coding error, a malformed request) and would just burn retries."""
    job_id = event_data.get("job_id", "<unknown>")
    try:
        await _execute(step, event_data, store, deps)
    except Exception as err:  # noqa: BLE001 - last line of defense: terminal state, never stuck
        log.exception("inngest run_digest %s failed", job_id)
        job = store.get(job_id)
        if job is not None:
            job.status = JobStatus.failed
            job.error = f"{type(err).__name__}: {err}"
            try:
                store.save(job)
            except Exception:  # noqa: BLE001 - store itself is broken; nothing more to do
                log.exception("job %s: could not persist failed status", job_id)


def build_inngest_client() -> inngest.Inngest:
    from chorus.config_env import INNGEST_APP_ID, INNGEST_EVENT_KEY_ENV, INNGEST_SIGNING_KEY_ENV

    return inngest.Inngest(
        app_id=INNGEST_APP_ID,
        event_key=os.environ.get(INNGEST_EVENT_KEY_ENV),
        signing_key=os.environ.get(INNGEST_SIGNING_KEY_ENV),
    )


def register(app: FastAPI, client: inngest.Inngest, store: JobStore, deps: Deps) -> None:
    """Define `run_digest` bound to `store`/`deps` and mount it at
    `/api/inngest`. Kept as a function (rather than module-level globals) so
    tests can build a function against fixture/mock deps without touching the
    real Inngest client."""

    @client.create_function(
        fn_id="run-digest",
        trigger=inngest.TriggerEvent(event=INNGEST_DIGEST_EVENT),
    )
    async def run_digest(ctx: inngest.Context) -> dict[str, Any]:
        await run_digest_body(ctx.step, dict(ctx.event.data), store, deps)
        return {"job_id": ctx.event.data.get("job_id")}

    inngest.fast_api.serve(app, client, [run_digest], serve_path=INNGEST_SERVE_PATH)
