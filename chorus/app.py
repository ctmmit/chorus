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
- Startup refuses to serve real providers without a token (lifespan check);
  CHORUS_ENV=production refuses to start with no token at all, even with no
  provider keys configured (R2, docs/REVIEW_WAVE1.md #2).
- Request bodies over MAX_BODY_BYTES are rejected before JSON parsing — both
  from a Content-Length header (fast path) and, for a chunked/lengthless
  body, from the actual bytes received as they stream in (R22): the guards
  middleware is a raw ASGI middleware (not `@app.middleware("http")`'s
  BaseHTTPMiddleware) specifically so it can wrap `receive` itself.
- Startup marks in-flight jobs stale per chorus.jobs.JobStore.fail_in_flight
  (SQLite: everything, single-process; Postgres: only rows older than
  CHORUS_STALE_JOB_SECONDS — R6, docs/REVIEW_WAVE1.md #6, a shared Postgres
  deployment has more than one live instance, so a blanket sweep would mark
  another instance's active job failed).
- On a request that passes auth, `request.state.owner` is set to "master"
  (the master token) or the issued key's email (chorus.keys.KeyStore.
  owner_of). Every job records its creating owner (R4); GET /digest/{id}
  and GET /artifacts/{name} 404 (never 403 — no ownership-enumeration signal)
  for a job/artifact that exists but belongs to someone else; master sees
  everything. A handful of routes are public by design (self-serve key
  issuance, the unsubscribe link, the internal cron trigger, GET discovery
  documents) and are listed in PUBLIC_ROUTES / PUBLIC_ROUTE_SUFFIXES /
  DISCOVERY_PUBLIC_PREFIXES below rather than scattered through the
  middleware.
- Per-owner spend quotas (R3, chorus.quotas) are enforced before every job
  creation; self-serve key issuance is throttled per IP and, optionally, to
  an email-domain allowlist (chorus.keys).
- CORS (R13): CORSMiddleware, allowlist from CHORUS_CORS_ORIGINS, added
  OUTSIDE the guards middleware so a preflight OPTIONS never reaches the
  bearer check.
"""
from __future__ import annotations

import logging
import os
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from chorus import __version__, catalog, config_env, discovery
from chorus.artifacts import job_id_from_artifact_name
from chorus.ask import build_ask_router
from chorus.email import EmailSender, get_email_sender
from chorus.feedback import (
    FeedbackService,
    build_feedback_router,
    is_feedback_link,
    proposal_writer_for,
)
from chorus.jobs import MASTER_OWNER, JobStore, SqliteJobStore
from chorus.keys import (
    IpIssueRateLimiter,
    KeyRateLimited,
    KeyStore,
    email_domain_allowed,
)
from chorus.library_api import LibraryService, build_library_router
from chorus.mcp_server import mount_mcp
from chorus.models import DigestRequest, Job, JobStatus, KeyRequest, SelectionRequest
from chorus.personas import PersonaRegistry
from chorus.pipeline import Deps
from chorus.podcast_feed import FEED_PUBLIC_PREFIX, build_feed_router
from chorus.podcasts_api import PodcastDirectory, build_podcasts_router
from chorus.quick_take import build_quick_take_router, share_quick_taker
from chorus.quotas import QuotaExceeded, enforce_job_quota
from chorus.runners import InngestRunner, JobRunner
from chorus.saved_items import SavedItemStore
from chorus.scheduler import CRON_SECRET_ENV
from chorus.subscriptions import SubscriptionStore
from chorus.subscriptions_api import build_subscriptions_router

log = logging.getLogger("chorus.app")

API_TOKEN_ENV = "CHORUS_API_TOKEN"
# R2 (docs/REVIEW_WAVE1.md #2): every credential that can cause provider
# spend counts — transcript (Supadata), TTS/LLM, speech-to-text (Deepgram),
# transactional email (Resend), object storage (Vercel Blob: a paid write
# surface even before anything reads from it), and Inngest (fans out into
# unbounded pipeline runs). All of these being present with no
# CHORUS_API_TOKEN is exactly the "open wallet" scenario the startup guard
# below exists to prevent.
PROVIDER_KEY_ENVS = (
    "ANTHROPIC_API_KEY",
    "ELEVENLABS_API_KEY",
    "TRANSCRIPT_API_KEY",
    "DEEPGRAM_API_KEY",
    "ASSEMBLYAI_API_KEY",
    "RESEND_API_KEY",
    "BLOB_READ_WRITE_TOKEN",
    "INNGEST_EVENT_KEY",
)
# R2: an explicit production mode that refuses to start with no
# CHORUS_API_TOKEN regardless of which (if any) provider keys happen to be
# configured — belt-and-suspenders for a deployment that, say, only has mock
# providers today but is still meant to be a real, access-controlled
# instance.
CHORUS_ENV_ENV = "CHORUS_ENV"
PRODUCTION_ENV_VALUE = "production"
# Largest legitimate body: MAX_SOUL_CHARS + MAX_CONTEXT_CHARS + episodes is well
# under 200 KB even with multi-byte characters. 1 MiB leaves headroom.
MAX_BODY_BYTES = 1 << 20
RESTART_REASON = "interrupted by service restart before completion"
# R6: a Postgres (shared, multi-instance) deployment only sweeps in-flight
# jobs whose row hasn't been touched in this long — a fresh cold start must
# not stomp another instance's actively-running job. SQLite is always swept
# in full (older_than_seconds=0): it is inherently single-process, so no
# in-flight row there can belong to a still-live worker.
STALE_JOB_SECONDS_ENV = "CHORUS_STALE_JOB_SECONDS"
DEFAULT_STALE_JOB_SECONDS = 1800
# R13: comma-separated CORS origin allowlist; unset/empty means no CORS
# headers are added at all (today's behavior — same-origin/non-browser
# callers only).
CORS_ORIGINS_ENV = "CHORUS_CORS_ORIGINS"
# Base URL of the web app (web/): when set, the key email carries a sign-in
# link `<url>/subscribe#token=<token>` in addition to the token text.
VIEWER_URL_ENV = "CHORUS_VIEWER_URL"
# Inngest signs its own requests (INNGEST_SIGNING_KEY) and calls this path
# directly, not through an agent holding CHORUS_API_TOKEN.
INNGEST_EXEMPT_PATH = "/api/inngest"
# Discovery documents + persona listing (chorus/discovery.py) must be public:
# another agent has to be able to find a Chorus persona before it has any
# credential to authenticate with. Only GET under these prefixes is exempt —
# POST/DELETE on /personas still require the bearer token (see `guards`).
DISCOVERY_PUBLIC_PREFIXES = ("/.well-known/", "/personas", "/network")
# (method, exact path) routes that never require the user bearer token, each
# with its own reason: POST /keys is how a caller obtains a token in the
# first place; GET/POST /internal/cron/tick carries its own CRON_SECRET
# bearer (chorus/subscriptions_api.py) — GET because Vercel Cron always
# calls a cron path with GET, POST for curl/another scheduler/tests.
PUBLIC_ROUTES: tuple[tuple[str, str], ...] = (
    ("POST", "/keys"),
    ("GET", "/internal/cron/tick"),
    ("POST", "/internal/cron/tick"),
)
# (method, path suffix) routes matched by suffix because they carry a path
# parameter: GET /subscriptions/{id}/unsubscribe is a public, HMAC-signed
# link clicked from an email client holding no API token.
PUBLIC_ROUTE_SUFFIXES: tuple[tuple[str, str], ...] = (
    ("GET", "/unsubscribe"),
)


def _provider_keys_present() -> bool:
    return any(os.environ.get(k) for k in PROVIDER_KEY_ENVS)


def _is_production_env() -> bool:
    return os.environ.get(CHORUS_ENV_ENV, "").strip().lower() == PRODUCTION_ENV_VALUE


def _stale_job_seconds() -> float:
    raw = os.environ.get(STALE_JOB_SECONDS_ENV)
    if not raw:
        return DEFAULT_STALE_JOB_SECONDS
    try:
        value = float(raw)
    except ValueError:
        log.warning(
            "%s=%r is not a number; using default %s", STALE_JOB_SECONDS_ENV, raw, DEFAULT_STALE_JOB_SECONDS
        )
        return DEFAULT_STALE_JOB_SECONDS
    return value if value >= 0 else DEFAULT_STALE_JOB_SECONDS


def _cors_origins() -> list[str]:
    raw = os.environ.get(CORS_ORIGINS_ENV, "")
    return [origin.strip() for origin in raw.split(",") if origin.strip()]


def _is_public_route(request: Request) -> bool:
    if request.url.path.startswith(INNGEST_EXEMPT_PATH):
        return True
    method, path = request.method, request.url.path
    if (method, path) in PUBLIC_ROUTES:
        return True
    if method == "GET" and path.startswith(DISCOVERY_PUBLIC_PREFIXES):
        return True
    if method == "GET" and path.startswith(FEED_PUBLIC_PREFIX):
        # The private podcast feed and its files carry their own HMAC token
        # in the path (chorus/podcast_feed.py); a podcast app sends no header.
        return True
    if is_feedback_link(method, path):
        # The one-click rating link in a digest email, HMAC-signed
        # (chorus/feedback.py); an email client holds no API token.
        return True
    return any(method == m and path.endswith(suffix) for m, suffix in PUBLIC_ROUTE_SUFFIXES)


def _resolve_owner(request: Request, token: str | None, key_store: KeyStore) -> str | None:
    """"master" for the master token, the issued key's email for a valid
    key, or None when unauthenticated/invalid. `token=None` (auth disabled)
    still resolves a presented key's owner so request.state.owner is always
    populated for route handlers, but never returns None in that case."""
    scheme, _, credential = request.headers.get("authorization", "").partition(" ")
    supplied = credential.strip() if scheme.lower() == "bearer" else ""
    if token is not None and supplied and secrets.compare_digest(supplied, token):
        return MASTER_OWNER
    if supplied and key_store.is_valid(supplied):
        return key_store.owner_of(supplied) or MASTER_OWNER
    return None if token is not None else MASTER_OWNER


def _request_owner(request: Request) -> str:
    return getattr(request.state, "owner", MASTER_OWNER)


class _BodyTooLarge(Exception):
    """Raised from inside the wrapped `receive` once a streamed body's
    running byte total exceeds MAX_BODY_BYTES (R22) — caught by
    `GuardsMiddleware` around the inner ASGI call, which is still safe to
    turn into a 413 at that point because nothing downstream has started
    sending a response yet (the whole body must finish parsing before any
    Pydantic-validated route handler runs)."""


class GuardsMiddleware:
    """Raw ASGI middleware — NOT `@app.middleware("http")` (which is
    `BaseHTTPMiddleware` and only exposes a `Request`/`call_next` pair, with
    no way to intercept `receive` itself) — so the streaming body-size limit
    below (R22) can actually enforce the limit on the real bytes received,
    not just a `Content-Length` header a client can omit or lie about.
    """

    def __init__(self, app: ASGIApp, token: str | None, key_store: KeyStore) -> None:
        self.app = app
        self.token = token
        self.key_store = key_store

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope)

        # Fast path: a Content-Length that already says "too big" is
        # rejected before reading anything at all.
        length = request.headers.get("content-length", "")
        if length.isdigit() and int(length) > MAX_BODY_BYTES:
            await JSONResponse({"detail": "request body too large"}, status_code=413)(scope, receive, send)
            return

        if not _is_public_route(request):
            owner = _resolve_owner(request, self.token, self.key_store)
            if owner is None:
                response = JSONResponse(
                    {"detail": "missing or invalid bearer token"},
                    status_code=401,
                    headers={"WWW-Authenticate": "Bearer"},
                )
                await response(scope, receive, send)
                return
            scope.setdefault("state", {})["owner"] = owner

        total = 0

        async def limited_receive() -> Message:
            nonlocal total
            message = await receive()
            if message["type"] == "http.request":
                total += len(message.get("body") or b"")
                if total > MAX_BODY_BYTES:
                    raise _BodyTooLarge()
            return message

        try:
            await self.app(scope, limited_receive, send)
        except _BodyTooLarge:
            response = JSONResponse({"detail": "request body too large"}, status_code=413)
            await response(scope, receive, send)


def create_app(
    store: JobStore | None = None,
    deps: Deps | None = None,
    api_token: str | None = None,
    runner: JobRunner | None = None,
    key_store: KeyStore | None = None,
    email_sender: EmailSender | None = None,
    personas: PersonaRegistry | None = None,
    subscription_store: SubscriptionStore | None = None,
    saved_item_store: SavedItemStore | None = None,
) -> FastAPI:
    """`api_token=None` reads CHORUS_API_TOKEN; unset means open access, which
    the lifespan check allows only when no real provider key is configured
    AND CHORUS_ENV is not "production" (R2). `personas=None` uses
    `chorus.config_env.select_persona_registry` (R1: Postgres when
    DATABASE_URL is set, else SQLite co-located with `store`'s file)."""
    store = store or config_env.select_job_store()
    deps = deps or config_env.build_deps()
    runner = runner or config_env.select_runner(store, deps)
    personas = personas or config_env.select_persona_registry(store)
    token = api_token if api_token is not None else os.environ.get(API_TOKEN_ENV) or None
    owns_key_store = key_store is None
    owns_subscription_store = subscription_store is None
    owns_saved_item_store = saved_item_store is None
    # SQLite: co-located with `store`'s file (chorus/config_env.py). Postgres
    # (DATABASE_URL set): survives across Vercel invocations, unlike SQLite.
    key_store = key_store or config_env.select_key_store(store)
    subscription_store = subscription_store or config_env.select_subscription_store(store)
    saved_item_store = saved_item_store or config_env.select_saved_item_store(store)
    email_sender = email_sender or get_email_sender()
    cron_secret = os.environ.get(CRON_SECRET_ENV) or None
    key_ip_limiter = IpIssueRateLimiter()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if token is None and (_provider_keys_present() or _is_production_env()):
            reason = (
                f"{CHORUS_ENV_ENV}={PRODUCTION_ENV_VALUE}"
                if _is_production_env() and not _provider_keys_present()
                else "a provider key is configured"
            )
            raise RuntimeError(
                f"refusing to start: {reason} but {API_TOKEN_ENV} is not. "
                "An open endpoint with real keys (or a declared production "
                "deployment) is an open wallet."
            )
        if token is None:
            log.warning("auth: %s unset — endpoints are OPEN (mock providers only)", API_TOKEN_ENV)
        # SQLite (local/single-process, including Railway): every in-flight
        # job predates this boot by definition, so sweep everything, exactly
        # as before. Postgres (possibly shared across live instances, R6):
        # only rows whose updated_at predates CHORUS_STALE_JOB_SECONDS — a
        # blanket sweep here could mark another instance's active job
        # failed, or race its own finalize and lose (the conditional `save`
        # in chorus.jobs/chorus.stores.postgres protects the latter either
        # way, but staying windowed avoids even attempting it).
        older_than = 0.0 if isinstance(store, SqliteJobStore) else _stale_job_seconds()
        swept = store.fail_in_flight(RESTART_REASON, older_than_seconds=older_than)
        if swept:
            log.warning("startup: marked %d in-flight job(s) failed (%s)", swept, RESTART_REASON)
        yield
        # R24: close everything Deps owns, then the stores this app instance
        # itself constructed (never a store/deps/personas/subscription_store
        # the caller injected — those are the caller's to close).
        deps.close()
        store.close()
        if owns_key_store and hasattr(key_store, "close"):
            key_store.close()
        if hasattr(personas, "close"):
            personas.close()
        if owns_subscription_store and hasattr(subscription_store, "close"):
            subscription_store.close()
        if owns_saved_item_store:
            saved_item_store.close()

    app = FastAPI(title="Chorus", version=__version__, lifespan=lifespan)
    app.state.personas = personas
    app.include_router(discovery.router)

    app.add_middleware(GuardsMiddleware, token=token, key_store=key_store)
    cors_origins = _cors_origins()
    if cors_origins:
        # Added AFTER (which Starlette/FastAPI makes OUTERMOST — the last
        # middleware added wraps everything before it): a preflight OPTIONS
        # is answered by CORSMiddleware itself and never reaches
        # GuardsMiddleware's bearer check (R13).
        app.add_middleware(
            CORSMiddleware,
            allow_origins=cors_origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["Authorization", "Content-Type"],
        )

    @app.post("/keys", status_code=202)
    def issue_key(request: KeyRequest, http_request: Request) -> Response:
        client_ip = http_request.client.host if http_request.client else "unknown"
        if not key_ip_limiter.allow(client_ip):
            raise HTTPException(
                status_code=429,
                detail="too many key requests from this address; try again later",
            )
        if not email_domain_allowed(request.email):
            # Same shape as KeyRateLimited below: a clear 403, no key issued,
            # no per-email attempt consumed (R3).
            raise HTTPException(status_code=403, detail="this email domain is not allowed to self-serve a key")
        try:
            issued_token = key_store.issue(request.email)
        except KeyRateLimited as err:
            raise HTTPException(status_code=429, detail=str(err)) from err
        subject = "Your Chorus API key"
        text = (
            "Your Chorus API key is below. Store it securely; it will not be shown again.\n\n"
            f"{issued_token}\n"
        )
        viewer_url = os.environ.get(VIEWER_URL_ENV, "").strip().rstrip("/")
        if viewer_url:
            # The token rides in the URL fragment, which browsers never send to
            # a server and which therefore stays out of access logs.
            text += f"\nOr sign in to the web app: {viewer_url}/subscribe#token={issued_token}\n"
        try:
            email_sender.send(request.email, subject, text)
        except Exception as err:
            key_store.revoke(issued_token)
            log.error("keys: issued but delivery failed for %s: %s", request.email, err)
            raise HTTPException(
                status_code=502,
                detail="key was issued but delivery failed; the key has been revoked — try again",
            ) from err
        return Response(status_code=202)

    @app.post("/digest")
    def create_digest(request: DigestRequest, http_request: Request, background: BackgroundTasks) -> Response:
        owner = _request_owner(http_request)
        try:
            enforce_job_quota(store, owner)
        except QuotaExceeded as err:
            raise HTTPException(status_code=429, detail=str(err)) from err
        job_id = store.create(owner=owner)
        try:
            runner.submit(job_id, request, background)
        except Exception as err:  # noqa: BLE001 - R8: never strand a queued job
            return _dispatch_failure_response(store, job_id, err)
        return JSONResponse({"job_id": job_id})

    @app.get("/digest/{job_id}")
    def get_digest(job_id: str, http_request: Request) -> Job:
        job = store.get(job_id)
        owner = _request_owner(http_request)
        if job is None or (owner != MASTER_OWNER and job.owner != owner):
            # Same 404 either way (R4): a caller must not be able to tell
            # "exists, not yours" from "does not exist".
            raise HTTPException(status_code=404, detail="unknown job_id")
        return job

    @app.get("/shows")
    def list_shows() -> list[dict]:
        return catalog.list_shows()

    @app.post("/digest/select")
    def select_digest(request: SelectionRequest, http_request: Request, background: BackgroundTasks) -> Response:
        owner = _request_owner(http_request)
        try:
            enforce_job_quota(store, owner)
        except QuotaExceeded as err:
            raise HTTPException(status_code=429, detail=str(err)) from err
        episodes = catalog.resolve(shows=request.shows, video_ids=request.video_ids)
        if not episodes:
            raise HTTPException(status_code=400, detail="selection resolved to no episodes")
        digest_request = DigestRequest(
            soul=request.soul,
            context=request.context,
            episodes=episodes,
            highlight_count=request.highlight_count,
            soul_origin=request.soul_origin,
            profile=request.profile,  # R17: previously dropped
        )
        job_id = store.create(owner=owner)
        try:
            runner.submit(job_id, digest_request, background)
        except Exception as err:  # noqa: BLE001 - R8: never strand a queued job
            return _dispatch_failure_response(store, job_id, err)
        return JSONResponse({"job_id": job_id})

    @app.get("/artifacts/{name}")
    def get_artifact(name: str, http_request: Request) -> Response:
        """R5: replaces the old StaticFiles mount. Resolves the owning job
        from `name` (chorus.artifacts.artifact_stem's `episode_<job_id>.<ext>`
        convention — no separate name->job table needed), checks the
        caller's owner against it exactly like GET /digest/{job_id}, and only
        then asks the artifact store for the bytes — local disk or a private
        Vercel Blob object either way, never a bare public URL."""
        job_id = job_id_from_artifact_name(name)
        owner = _request_owner(http_request)
        job = store.get(job_id) if job_id else None
        if job is None or (owner != MASTER_OWNER and job.owner != owner):
            raise HTTPException(status_code=404, detail="unknown artifact")
        result = deps.artifacts.get(name)
        if result is None:
            raise HTTPException(status_code=404, detail="unknown artifact")
        data, content_type = result
        return Response(content=data, media_type=content_type)

    # Subscriptions (Phase F): CRUD + unsubscribe + POST /internal/cron/tick,
    # all defined in chorus/subscriptions_api.py.
    app.include_router(
        build_subscriptions_router(
            subscription_store, store, deps, email_sender, cron_secret, saved_item_store
        )
    )
    # Podcast search/resolve/OPML import and POST /souls/interview, sharing
    # one cache and throttle with the MCP tools.
    podcasts = PodcastDirectory()
    app.include_router(build_podcasts_router(podcasts))
    # Library import (chorus/library_api.py): saved episodes and followed
    # shows from Readwise, Apple, Spotify, OPML, pushed by the agent.
    library = LibraryService(saved_item_store, podcasts, subscription_store)
    app.include_router(
        build_library_router(library, share_quick_taker(store, deps, subscription_store))
    )
    # One episode, judged now (chorus/quick_take.py).
    app.include_router(build_quick_take_router(store, deps))
    # The private podcast feed (chorus/podcast_feed.py): GET /feed for the
    # caller's URL, and the token-authorized feed, audio, chapters, transcript.
    app.include_router(build_feed_router(store, deps.artifacts))
    # Highlight ratings and soul proposals (chorus/feedback.py).
    feedback = FeedbackService(
        config_env.select_feedback_store(store),
        store,
        subscription_store,
        writer_factory=proposal_writer_for(deps.llm),
    )
    app.include_router(build_feedback_router(feedback))
    # Questions answered from a digest's own transcripts (chorus/ask.py).
    app.include_router(build_ask_router(store, deps.provider, deps.llm))

    # Inngest only when it is actually the active runner (a durable-step
    # invocation needs the same store/deps every step reads and writes).
    if isinstance(runner, InngestRunner):
        from chorus.inngest_app import register

        register(
            app, runner.client, store, deps, subscription_store, email_sender, saved_item_store
        )

    # MCP submissions go through this SAME runner (R9) so hosted (Inngest)
    # deployments get the same durable-step semantics an HTTP submit gets,
    # rather than awaiting the whole pipeline inline within one MCP call.
    # Mounts at / internally so its own route stays exactly /mcp. Keep last:
    # Starlette resolves routes in order and this mount is intentionally catch-all.
    mount_mcp(app, store, deps, runner, subscription_store, podcasts, library)
    return app


def _dispatch_failure_response(store: JobStore, job_id: str, err: Exception) -> JSONResponse:
    """R8: a runner.submit() failure must never strand a job `queued` with
    nothing driving it. Marks it `failed` (conditional save — chorus.jobs —
    so this can never regress an already-terminal row) and reports 503 with
    enough detail that the caller can stop polling immediately instead of
    waiting for a sweep that (on Postgres, R6) may not even run for a while."""
    reason = f"dispatch failed: {type(err).__name__}: {err}"
    job = store.get(job_id)
    if job is not None:
        job.status = JobStatus.failed
        job.error = reason
        store.save(job)
    log.error("job %s: %s", job_id, reason)
    return JSONResponse({"job_id": job_id, "status": "failed", "error": reason}, status_code=503)


app = create_app()


LOCAL_HOST_DEFAULT = "127.0.0.1"
LOCAL_PORT_DEFAULT = "8000"


def serve() -> None:
    """Local real-provider serving: the `chorus-api` command (or
    `python -m chorus.app`). Loads .env.local first. Binds loopback by default;
    set HOST=0.0.0.0 to expose it on the network. On Vercel, env vars are set
    in the platform and the module-level `app` above is what gets served."""
    import uvicorn  # type: ignore[import-not-found]

    from chorus.config import load_env

    load_env()
    uvicorn.run(
        create_app(),
        host=os.environ.get("HOST", LOCAL_HOST_DEFAULT),
        port=int(os.environ.get("PORT", LOCAL_PORT_DEFAULT)),
    )


if __name__ == "__main__":
    serve()
