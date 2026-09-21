"""FastAPI surface: the §6 async lifecycle a calling agent drives.

    POST /digest          -> {job_id}
    GET  /digest/{job_id} -> Job (status queued|digest_ready|done|failed)

One mechanism, progressive states (ENGINEERING_REVIEW Q3). `create_app` takes
injectable store/deps/runner so tests run fully offline against fixtures +
mocks, and so the Vercel port (Phase B) is purely additive: which store,
artifact backend, and job runner get used is decided by chorus.config_env
from the environment (SQLite/local/BackgroundTasks with nothing set; Postgres/
Blob/Inngest on Vercel), never by anything in this file.

Guards (the service holds paid provider keys server-side, so an open endpoint
is an open wallet):
- Bearer auth on every route except /api/inngest and the public discovery
  surface (docs/DEVELOPMENT_PLAN.md §8 row H, chorus/discovery.py):
  /.well-known/*, and GET (only) under /personas and /network. /api/inngest
  is exempt because Inngest signs its own requests (with
  INNGEST_SIGNING_KEY) and is not a browser/agent-facing route; requiring our
  bearer token there as well would just make Inngest's callback fail.
  Discovery documents and persona listings are exempt because another
  agent has to be able to find a persona before it can authenticate to
  anything — but POST/DELETE under /personas still require the token
  (DISCOVERY_PUBLIC_PREFIXES is method-gated, not just path-gated).
- Startup refuses to serve real providers without a token (lifespan check).
- Request bodies over MAX_BODY_BYTES are rejected before JSON parsing; the
  request models bound every field on top of that.
- Startup marks any job left in flight by a previous process as failed.
"""
from __future__ import annotations

import logging
import os
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from chorus import catalog, config_env, discovery
from chorus.artifacts import LocalArtifactStore
from chorus.jobs import DEFAULT_DB, JobStore
from chorus.models import DigestRequest, Job, SelectionRequest
from chorus.personas import PersonaRegistry, SqlitePersonaRegistry
from chorus.pipeline import Deps
from chorus.runners import InngestRunner, JobRunner

log = logging.getLogger("chorus.app")

API_TOKEN_ENV = "CHORUS_API_TOKEN"
PROVIDER_KEY_ENVS = ("ANTHROPIC_API_KEY", "ELEVENLABS_API_KEY")
# Largest legitimate body: MAX_SOUL_CHARS + MAX_CONTEXT_CHARS + episodes is well
# under 200 KB even with multi-byte characters. 1 MiB leaves headroom.
MAX_BODY_BYTES = 1 << 20
RESTART_REASON = "interrupted by service restart before completion"
# Inngest signs its own requests (INNGEST_SIGNING_KEY) and calls this path
# directly, not through an agent holding CHORUS_API_TOKEN.
INNGEST_EXEMPT_PATH = "/api/inngest"
# Discovery documents + persona listing (chorus/discovery.py) must be public:
# another agent has to be able to find a Chorus persona before it has any
# credential to authenticate with. Only GET under these prefixes is exempt —
# POST/DELETE on /personas still require the bearer token (see `guards`).
DISCOVERY_PUBLIC_PREFIXES = ("/.well-known/", "/personas", "/network")


def _provider_keys_present() -> bool:
    return any(os.environ.get(k) for k in PROVIDER_KEY_ENVS)


def _authorized(request: Request, token: str) -> bool:
    scheme, _, credential = request.headers.get("authorization", "").partition(" ")
    return scheme.lower() == "bearer" and secrets.compare_digest(credential.strip(), token)


def create_app(
    store: JobStore | None = None,
    deps: Deps | None = None,
    api_token: str | None = None,
    runner: JobRunner | None = None,
    personas: PersonaRegistry | None = None,
) -> FastAPI:
    """`api_token=None` reads CHORUS_API_TOKEN; unset means open access, which
    the lifespan check allows only when no real provider key is configured.
    `personas=None` defaults to a SqlitePersonaRegistry on the job store's own
    db path when `store` is SQLite-backed (so persona + job data live in the
    same file, like chorus.transcript_cache does), else DEFAULT_DB — there is
    no Postgres PersonaRegistry yet (chorus/personas.py), so a Postgres job
    store still gets a local SQLite persona table rather than failing."""
    store = store or config_env.select_job_store()
    deps = deps or config_env.build_deps()
    runner = runner or config_env.select_runner(store, deps)
    personas = personas or SqlitePersonaRegistry(getattr(store, "db_path", DEFAULT_DB))
    token = api_token if api_token is not None else os.environ.get(API_TOKEN_ENV) or None

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if token is None and _provider_keys_present():
            raise RuntimeError(
                f"refusing to start: a provider key is configured but {API_TOKEN_ENV} is not. "
                "An open endpoint with real keys is an open wallet."
            )
        if token is None:
            log.warning("auth: %s unset — endpoints are OPEN (mock providers only)", API_TOKEN_ENV)
        # Single-process assumption (local/Railway run one worker; on Vercel
        # each invocation is its own process): at startup no background task
        # or in-flight Inngest step from THIS process can be alive, so every
        # in-flight job recorded before this boot is stranded.
        swept = store.fail_in_flight(RESTART_REASON)
        if swept:
            log.warning("startup: marked %d in-flight job(s) failed (%s)", swept, RESTART_REASON)
        yield
        store.close()
        close_personas = getattr(personas, "close", None)
        if close_personas is not None:
            close_personas()

    app = FastAPI(title="Chorus", version="0.1.0", lifespan=lifespan)
    app.state.personas = personas
    app.include_router(discovery.router)

    @app.middleware("http")
    async def guards(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        length = request.headers.get("content-length", "")
        if length.isdigit() and int(length) > MAX_BODY_BYTES:
            return JSONResponse({"detail": "request body too large"}, status_code=413)
        # Inngest signs its own requests and calls this path directly, not
        # through an agent holding CHORUS_API_TOKEN — see module docstring.
        is_inngest = request.url.path.startswith(INNGEST_EXEMPT_PATH)
        # Discovery documents + persona/network listing are public; only GET
        # is exempt, so POST/DELETE on /personas still requires the token.
        is_public_discovery = request.method == "GET" and request.url.path.startswith(
            DISCOVERY_PUBLIC_PREFIXES
        )
        if not is_inngest and not is_public_discovery and token is not None and not _authorized(request, token):
            return JSONResponse(
                {"detail": "missing or invalid bearer token"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
        return await call_next(request)

    @app.post("/digest")
    def create_digest(request: DigestRequest, background: BackgroundTasks) -> dict[str, str]:
        job_id = store.create()
        runner.submit(job_id, request, background)
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
        runner.submit(job_id, digest_request, background)
        return {"job_id": job_id}

    # Inngest only when it is actually the active runner (a durable-step
    # invocation needs the same store/deps every step reads and writes).
    if isinstance(runner, InngestRunner):
        from chorus.inngest_app import register

        register(app, runner.client, store, deps)

    # Serve rendered episodes locally so audio_url ("/artifacts/<name>") is
    # downloadable. When artifacts live in Vercel Blob, audio_url is already
    # an absolute URL and this mount would just serve an empty directory.
    if isinstance(deps.artifacts, LocalArtifactStore):
        app.mount(
            "/artifacts",
            StaticFiles(directory=str(deps.artifacts.directory), check_dir=False),
            name="artifacts",
        )
    return app


app = create_app()


if __name__ == "__main__":
    # Local real-provider serving: `python -m chorus.app` (loads .env.local first).
    # On Railway/Vercel, env vars are set in the platform, so the module-level
    # `app` above already gets real providers via config_env's selection.
    import uvicorn  # type: ignore[import-not-found]

    from chorus.config import load_env

    load_env()
    uvicorn.run(create_app(), host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
