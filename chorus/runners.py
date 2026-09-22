"""Job runners: how a submitted job actually gets executed.

Local/dev and today's production (`BackgroundRunner`): FastAPI's
`BackgroundTasks` runs `chorus.pipeline.run_job` in-process after the
response is sent — exactly as before Phase B. When no `BackgroundTasks`
instance is available at all (R9, docs/REVIEW_WAVE1.md #9: chorus.mcp_server
submits through this SAME runner rather than awaiting `run_job` inline), it
runs the job on a daemon thread instead, so the caller can still return
immediately.

Vercel (`InngestRunner`): a Vercel function can be frozen the instant the
response is sent, so BackgroundTasks cannot survive there. InngestRunner
instead sends an event; the Inngest function in chorus/inngest_app.py picks
it up and drives the pipeline through durable, independently-retried steps.

`create_app` selects one of these (chorus.config_env.select_runner) and the
route handlers stay a two-line call to `runner.submit(...)` either way. R8
(docs/REVIEW_WAVE1.md #8): `submit` raising is the caller's signal to mark
the job failed and report a clear dispatch error rather than leaving it
queued forever — every caller (chorus.app's routes, chorus.mcp_server's
tools) does that around its own `submit()` call.
"""
from __future__ import annotations

import logging
import threading
from typing import Any, Protocol, runtime_checkable

from fastapi import BackgroundTasks

from chorus.jobs import JobStore
from chorus.models import DigestRequest
from chorus.pipeline import Deps, run_job

log = logging.getLogger("chorus.runners")

# The event chorus/inngest_app.py's `run_digest` function is triggered by.
INNGEST_DIGEST_EVENT = "chorus/digest.requested"


@runtime_checkable
class JobRunner(Protocol):
    def submit(
        self, job_id: str, request: DigestRequest, background: BackgroundTasks | None = None
    ) -> None:
        """Start the job running, or raise if dispatch itself failed (R8) —
        never leave `job_id` stranded queued with nothing driving it.
        `background` is FastAPI's per-request BackgroundTasks instance when
        one is available (every HTTP route has one); BackgroundRunner uses it
        when given, and falls back to a daemon thread when it is None (the
        MCP tool call path, R9); InngestRunner ignores it either way (Inngest
        schedules its own invocation of /api/inngest)."""
        ...


class BackgroundRunner:
    """In-process runner: hands `run_job` to the current request's
    BackgroundTasks so it runs after the response is sent, exactly as
    chorus.app did pre-Phase-B — or, with no BackgroundTasks instance (R9:
    chorus.mcp_server has no per-request BackgroundTasks to hand the pipeline
    to), runs it on a daemon thread so the caller can still return the job id
    immediately instead of awaiting the whole pipeline inline."""

    def __init__(self, store: JobStore, deps: Deps) -> None:
        self.store = store
        self.deps = deps

    def submit(
        self, job_id: str, request: DigestRequest, background: BackgroundTasks | None = None
    ) -> None:
        if background is not None:
            background.add_task(run_job, job_id, request, self.store, self.deps)
            return
        thread = threading.Thread(
            target=run_job,
            args=(job_id, request, self.store, self.deps),
            name=f"chorus-run-job-{job_id}",
            daemon=True,
        )
        thread.start()


class InngestRunner:
    """Sends the `chorus/digest.requested` event that chorus/inngest_app.py's
    `run_digest` function is triggered by. `client` is `inngest.Inngest` in
    production; tests pass a fake with a `.send_sync(events)` method that
    records what it was given, so no dev server or network is needed to test
    submission.

    Uses `client.send_sync` (not the async `client.send`): the route handlers
    in chorus/app.py are plain `def`s (FastAPI runs them in a threadpool, as
    it always has), and JobRunner.submit is a sync Protocol method to match —
    inngest-py ships both an async and a sync send for exactly this case.
    """

    def __init__(self, client: Any) -> None:
        self.client = client

    def submit(
        self, job_id: str, request: DigestRequest, background: BackgroundTasks | None = None
    ) -> None:
        import inngest  # local import: only required when this runner is active

        event = inngest.Event(
            name=INNGEST_DIGEST_EVENT,
            data={"job_id": job_id, "request": request.model_dump(mode="json")},
        )
        self.client.send_sync(event)
        log.info("inngest: sent %s for job %s", INNGEST_DIGEST_EVENT, job_id)
