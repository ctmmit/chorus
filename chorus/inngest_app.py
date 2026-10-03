"""Inngest durable-step orchestrator for the digest pipeline (Phase B,
docs/DEVELOPMENT_PLAN.md §5): the Vercel replacement for FastAPI
BackgroundTasks, whose in-process job dies the instant a Vercel function is
frozen after responding.

`run_digest` is triggered by the `chorus/digest.requested` event that
chorus.runners.InngestRunner.submit sends. Its body calls the SAME stage
functions from chorus/pipeline.py (`stage_ingest`, `stage_curate_episode`,
`stage_brief`, `stage_outline`, `stage_script`, `stage_audio`) that the
BackgroundRunner path calls, one per
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
from datetime import UTC, datetime
from typing import Any, Protocol

import inngest
import inngest.fast_api
from fastapi import FastAPI

from chorus.curation import soul_version
from chorus.email import EmailSender
from chorus.errors import is_retryable
from chorus.jobs import IN_FLIGHT_STATUSES, JobStore
from chorus.models import (
    CurateResult,
    Digest,
    DigestRequest,
    EpisodeOutline,
    IngestResult,
    Job,
    JobStatus,
    JobUsage,
    ResolvedEpisode,
    Script,
)
from chorus.pipeline import (
    PLACEHOLDER_AUDIO_WARNING,
    AllEpisodesFailed,
    AudioResult,
    BriefResult,
    Deps,
    stage_audio,
    stage_brief,
    stage_curate_episode,
    stage_ingest,
    stage_outline,
    stage_script,
    sum_llm_tokens,
)
from chorus.saved_items import SavedItemStore
from chorus.scheduler import TICK_CRON_SCHEDULE, due_subscriptions, run_subscription
from chorus.subscriptions import Subscription, SubscriptionStore
from chorus.subscriptions_api import resolve_base_url

log = logging.getLogger("chorus.inngest_app")

# The event chorus.runners.InngestRunner.submit sends and this module's
# function is triggered by.
INNGEST_DIGEST_EVENT = "chorus/digest.requested"
INNGEST_SERVE_PATH = "/api/inngest"
# Subscription fan-out (Phase F, docs/DEVELOPMENT_PLAN.md §3/§8 row F): one
# cron-triggered function id, `chorus/tick`, polled on TICK_CRON_SCHEDULE —
# covers both "weekly" and "daily" cadences, since due() only compares
# next_run_at to now (see chorus/scheduler.py's module docstring for why this
# is one tick rather than a separate weekly + hourly-daily pair).
INNGEST_TICK_FUNCTION_ID = "chorus-tick"
# R7: up to 3 retries for a run_digest_body invocation that raised a
# retryable failure (chorus.errors.is_retryable). Verified 21 Sep 2026
# against inngest-py's `create_function(retries=...)` (an int 0-20; Inngest's
# own default is also 3 when unset, made explicit here for clarity).
RUN_DIGEST_RETRIES = 3


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
    curated: list[CurateResult] = []
    for resolved in ingested.resolved:
        episode_id = resolved.episode.resolved_id()

        async def _curate(resolved: ResolvedEpisode = resolved) -> dict[str, Any]:
            return stage_curate_episode(resolved, request, deps.llm).model_dump(mode="json")

        data = await step.run(f"curate:{episode_id}", _curate)
        curated.append(CurateResult.model_validate(data))

    job.digest = Digest(
        soul_version=soul_version(request.soul),
        soul_origin=request.soul_origin,
        episodes=[c.digest for c in curated],
    )
    usage.stage_seconds["curate"] = time.perf_counter() - t0
    # R14: the Inngest path previously recorded no LLM usage at all (each
    # `step.run` result is durable/memoized independently, so there was no
    # shared client-level counter to even snapshot from here). Summing each
    # step's own isolated CurateResult.tokens fixes that.
    usage.llm_tokens = sum_llm_tokens([c.tokens for c in curated])
    job.status = JobStatus.digest_ready

    async def _save_digest_ready() -> dict[str, str]:
        return _save_job(store, job)

    await step.run("save-digest-ready", _save_digest_ready)

    t0 = time.perf_counter()
    try:
        digest = job.digest

        # Three steps so a failed segment call retries without re-briefing.
        async def _brief(digest: Digest = digest) -> dict[str, Any]:
            return stage_brief(digest, request, deps.composer).model_dump(mode="json")

        briefs = BriefResult.model_validate(await step.run("brief", _brief)).briefs

        async def _outline(digest: Digest = digest) -> dict[str, Any]:
            return stage_outline(digest, briefs, request, deps.composer).model_dump(mode="json")

        outline = EpisodeOutline.model_validate(await step.run("outline", _outline))

        async def _script(digest: Digest = digest) -> dict[str, Any]:
            return stage_script(digest, request, deps.composer, briefs, outline).model_dump(
                mode="json"
            )

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

            async def _audio(script: Script = script) -> dict[str, object]:
                # Step outputs must be JSON; AudioResult round-trips through model_dump.
                return stage_audio(
                    script, request, job.job_id, deps.renderer, deps.artifacts
                ).model_dump()

            audio = AudioResult.model_validate(await step.run("audio", _audio))
            job.audio_url = audio.url
            if audio.placeholder:
                job.warnings.append(PLACEHOLDER_AUDIO_WARNING)
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
    """Terminal-state guarantee, mirroring chorus.pipeline.run_job, but
    retry-aware (docs/REVIEW_WAVE1.md #7): an exception escaping `_execute`
    is classified with `chorus.errors.is_retryable`.

    - Retryable (an LLM/transcript-provider/database/transport failure that
      might succeed on a later attempt): persist a diagnostic on the job
      (`warnings`, status left unchanged — the conditional `save` in
      chorus.jobs never regresses a row, so this is safe to call from a
      retried attempt too) and RE-RAISE, so Inngest retries the whole
      function per its `retries=3` configuration (see `register` below).
    - Terminal (deterministic: bad input, every provider genuinely and
      permanently failed, a coding error): mark the job `failed` and return
      normally — Inngest acknowledges the run, no retry, exactly as before.
      Retrying a deterministic failure cannot help and would just burn
      retries.

    `run_digest_on_failure` (registered as `run_digest`'s `on_failure`
    handler) is the last line of defense once every retry is exhausted for a
    genuinely retryable failure.
    """
    job_id = event_data.get("job_id", "<unknown>")
    try:
        await _execute(step, event_data, store, deps)
    except Exception as err:
        retryable = is_retryable(err)
        reason = f"{type(err).__name__}: {err}"
        log.exception("inngest run_digest %s failed (retryable=%s)", job_id, retryable)
        job = store.get(job_id)
        if job is not None:
            if retryable:
                job.warnings.append(f"retryable failure (Inngest will retry): {reason}")
            else:
                job.status = JobStatus.failed
                job.error = reason
            try:
                store.save(job)
            except Exception:
                log.exception(
                    "job %s: could not persist %s diagnostic",
                    job_id,
                    "retry" if retryable else "failed",
                )
        if retryable:
            raise


def _job_id_from_failure_event_data(data: dict[str, Any]) -> str | None:
    """Best-effort extraction of the original `job_id` from an Inngest
    `inngest/function.failed` event's data. Verified 21 Sep 2026 against
    inngest-py's failure-event shape (`{"event": {...original event...},
    "function_id": ..., "error": {...}}`); defensive about both that shape
    and a flatter one, since it is an SDK internal rather than a stable,
    separately-versioned contract."""
    original_event = data.get("event")
    if isinstance(original_event, dict):
        original_data = original_event.get("data")
        if isinstance(original_data, dict):
            job_id = original_data.get("job_id")
            if isinstance(job_id, str):
                return job_id
    job_id = data.get("job_id")
    return job_id if isinstance(job_id, str) else None


def finalize_after_exhausted_retries(store: JobStore, job_id: str | None) -> None:
    """The `run_digest` `on_failure` handler's core logic, factored out so it
    is directly testable without a real `inngest.Context`. Called once
    Inngest has exhausted every retry for a genuinely retryable failure (or
    immediately, for a failure raised outside `run_digest_body`'s own
    classification — e.g. a crash in Inngest's own step machinery).
    Finalizes the job `failed` so it never polls forever even after retries
    are spent. Idempotent via the same conditional `save` as everywhere else
    — a no-op if the job already reached a terminal state through another
    path (e.g. `run_digest_body` already marked it failed once retryable
    turned out false on a later attempt). Never raises: an exception here
    would just be Inngest's own problem to retry-loop on, with no useful
    recovery available."""
    if job_id is None:
        log.error("run_digest on_failure: no job_id in the failure event")
        return
    try:
        job = store.get(job_id)
        if job is not None and job.status in IN_FLIGHT_STATUSES:
            job.status = JobStatus.failed
            job.error = "exhausted retries (see warnings for the last retryable failure)"
            store.save(job)
    except Exception:
        log.exception("run_digest on_failure: could not finalize job %s", job_id)


async def run_tick_body(
    step: StepLike,
    store: JobStore,
    deps: Deps,
    subscription_store: SubscriptionStore,
    email_sender: EmailSender,
    saved_items: SavedItemStore | None = None,
) -> dict[str, Any]:
    """The `chorus/tick` function body: one `step.run` per due subscription
    (so one subscription's failure retries alone, exactly like `_execute`'s
    per-episode `curate:<episode_id>` steps), each calling
    chorus.scheduler.run_subscription — the same fan-out
    `POST /internal/cron/tick` (chorus/subscriptions_api.py) runs inline for
    deployments without Inngest. Exercised directly with a fake `step` in
    tests, same pattern as `run_digest_body`."""
    now = datetime.now(UTC)
    base_url = resolve_base_url(None)
    ran: list[dict[str, str | None]] = []
    for subscription in due_subscriptions(subscription_store, now):

        async def _run(subscription: Subscription = subscription) -> dict[str, str | None]:
            job_id = run_subscription(
                subscription,
                store,
                deps,
                subscription_store,
                email_sender,
                base_url,
                now,
                saved_items=saved_items,
            )
            return {"subscription_id": subscription.subscription_id, "job_id": job_id}

        result = await step.run(f"run:{subscription.subscription_id}", _run)
        ran.append(result)
    return {"ran": len(ran), "subscriptions": ran}


def build_inngest_client() -> inngest.Inngest:
    from chorus.config_env import INNGEST_APP_ID, INNGEST_EVENT_KEY_ENV, INNGEST_SIGNING_KEY_ENV

    return inngest.Inngest(
        app_id=INNGEST_APP_ID,
        event_key=os.environ.get(INNGEST_EVENT_KEY_ENV),
        signing_key=os.environ.get(INNGEST_SIGNING_KEY_ENV),
    )


def register(
    app: FastAPI,
    client: inngest.Inngest,
    store: JobStore,
    deps: Deps,
    subscription_store: SubscriptionStore | None = None,
    email_sender: EmailSender | None = None,
    saved_items: SavedItemStore | None = None,
) -> None:
    """Define `run_digest` (and, when `subscription_store`/`email_sender`
    are given, the `chorus/tick` cron function) bound to `store`/`deps`, and
    mount both at `/api/inngest`. Kept as a function (rather than
    module-level globals) so tests can build a function against
    fixture/mock deps without touching the real Inngest client."""

    async def run_digest_on_failure(ctx: inngest.Context) -> None:
        finalize_after_exhausted_retries(
            store, _job_id_from_failure_event_data(dict(ctx.event.data or {}))
        )

    @client.create_function(
        fn_id="run-digest",
        trigger=inngest.TriggerEvent(event=INNGEST_DIGEST_EVENT),
        retries=RUN_DIGEST_RETRIES,
        on_failure=run_digest_on_failure,
    )
    async def run_digest(ctx: inngest.Context) -> dict[str, Any]:
        await run_digest_body(ctx.step, dict(ctx.event.data), store, deps)
        return {"job_id": ctx.event.data.get("job_id")}

    functions = [run_digest]

    if subscription_store is not None and email_sender is not None:

        @client.create_function(
            fn_id=INNGEST_TICK_FUNCTION_ID,
            trigger=inngest.TriggerCron(cron=TICK_CRON_SCHEDULE),
        )
        async def run_tick(ctx: inngest.Context) -> dict[str, Any]:
            return await run_tick_body(
                ctx.step, store, deps, subscription_store, email_sender, saved_items
            )

        functions.append(run_tick)

    inngest.fast_api.serve(app, client, functions, serve_path=INNGEST_SERVE_PATH)
