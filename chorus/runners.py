"""Job runners: how a submitted job actually gets executed.

Local/dev and today's production (`BackgroundRunner`): FastAPI's
`BackgroundTasks` runs `chorus.pipeline.run_job` in-process after the
response is sent — exactly as before Phase B.

Vercel (`InngestRunner`): a Vercel function can be frozen the instant the
response is sent, so BackgroundTasks cannot survive there. InngestRunner
instead sends an event; the Inngest function in chorus/inngest_app.py picks
it up and drives the pipeline through durable, independently-retried steps.

`create_app` selects one of these (chorus.config_env.select_runner) and the
route handlers stay a two-line call to `runner.submit(...)` either way.
"""
from __future__ import annotations

import logging
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
        """Start the job running. `background` is FastAPI's per-request
        BackgroundTasks instance; BackgroundRunner requires it, InngestRunner
        ignores it (Inngest schedules its own invocation of /api/inngest)."""
        ...


class BackgroundRunner:
    """In-process runner: hands `run_job` to the current request's
    BackgroundTasks so it runs after the response is sent, exactly as
    chorus.app did pre-Phase-B."""

    def __init__(self, store: JobStore, deps: Deps) -> None:
        self.store = store
        self.deps = deps

    def submit(
        self, job_id: str, request: DigestRequest, background: BackgroundTasks | None = None
    ) -> None:
        if background is None:
            raise RuntimeError("BackgroundRunner.submit requires a BackgroundTasks instance")
        background.add_task(run_job, job_id, request, self.store, self.deps)


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
