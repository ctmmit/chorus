"""FastAPI surface: the §6 async lifecycle a calling agent drives.

    POST /digest          -> {job_id}
    GET  /digest/{job_id} -> Job (status queued|digest_ready|done|failed)

One mechanism, progressive states (ENGINEERING_REVIEW Q3). `create_app` takes an
injectable store + deps so tests run fully offline against fixtures + mocks.
"""
from __future__ import annotations

import os

from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles

from chorus import catalog
from chorus.audio import ARTIFACT_DIR
from chorus.jobs import JobStore
from chorus.models import DigestRequest, Job, SelectionRequest
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

    @app.get("/shows")
    def list_shows() -> list[dict]:
        return catalog.list_shows()

    @app.post("/digest/select")
    def select_digest(request: SelectionRequest, background: BackgroundTasks) -> dict[str, str]:
        episodes = catalog.resolve(shows=request.shows, video_ids=request.video_ids)
        if not episodes:
            raise HTTPException(status_code=400, detail="selection resolved to no episodes")
        digest_request = DigestRequest(
            soul=request.soul,
            context=request.context,
            episodes=episodes,
            highlight_count=request.highlight_count,
            soul_origin=request.soul_origin,
        )
        job_id = store.create()
        background.add_task(run_job, job_id, digest_request, store, deps)
        return {"job_id": job_id}

    # Serve rendered episodes so audio_url ("/artifacts/<name>") is downloadable.
    app.mount("/artifacts", StaticFiles(directory=str(ARTIFACT_DIR), check_dir=False), name="artifacts")
    return app


app = create_app()


if __name__ == "__main__":
    # Local real-provider serving: `python -m chorus.app` (loads .env.local first).
    # On Railway, env vars are set in the platform, so the module-level `app`
    # above already gets real providers via the Procfile.
    import uvicorn

    from chorus.config import load_env

    load_env()
    uvicorn.run(create_app(), host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
