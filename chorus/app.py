"""FastAPI surface: the §6 async lifecycle a calling agent drives.

    POST /digest          -> {job_id}
    GET  /digest/{job_id} -> Job (status queued|digest_ready|done|failed)

One mechanism, progressive states (ENGINEERING_REVIEW Q3). `create_app` takes an
injectable store + deps so tests run fully offline against fixtures + mocks.
"""
from __future__ import annotations

from fastapi import BackgroundTasks, FastAPI, HTTPException

from chorus.jobs import JobStore
from chorus.models import DigestRequest, Job
from chorus.pipeline import Deps, default_deps, run_job


def create_app(store: JobStore | None = None, deps: Deps | None = None) -> FastAPI:
    store = store or JobStore()
    deps = deps or default_deps()
    app = FastAPI(title="Chorus", version="0.1.0")

    @app.post("/digest")
    def create_digest(request: DigestRequest, background: BackgroundTasks) -> dict[str, str]:
        job_id = store.create()
        background.add_task(run_job, job_id, request, store, deps)
        return {"job_id": job_id}

    @app.get("/digest/{job_id}")
    def get_digest(job_id: str) -> Job:
        job = store.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="unknown job_id")
        return job

    return app


app = create_app()
