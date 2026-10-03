"""Subscriptions CRUD + unsubscribe + internal cron trigger (Phase F,
docs/DEVELOPMENT_PLAN.md §3 and §8 row F). One APIRouter, included from
chorus/app.py with one `app.include_router(...)` call so the route
definitions (and their ownership-scoping rules) live entirely in this
module.

Feed subscriptions: `POST /subscriptions` takes typed `sources` (RSS feed,
YouTube channel, catalog show); `POST /subscriptions/preview` shows what the
next run would pick up without saving anything. Podcast search/resolve/OPML
import live in chorus/podcasts_api.py.

Ownership: `request.state.owner` (set by chorus.app's auth middleware) is
either `"master"` (the master CHORUS_API_TOKEN) or the email an issued key
was issued to (chorus.keys.KeyStore.owner_of). The master token sees every
subscription; an issued key sees only its own; a subscription owned by
someone else looks exactly like a nonexistent one (404, never 403) so a
caller cannot enumerate other principals' subscription ids by trying them
and distinguishing "forbidden" from "not found".

Two routes are deliberately exempt from the normal bearer-token check
(chorus.app's `guards` middleware carries the exemption list):
- `GET /subscriptions/{id}/unsubscribe` — a public, HMAC-signed link clicked
  from an email client that holds no API token.
- `GET`/`POST /internal/cron/tick` — guarded by its own `Authorization:
  Bearer $CRON_SECRET` (Vercel Cron convention: Vercel always calls a cron
  path with GET and injects that header itself when CRON_SECRET is a
  project env var; POST also works, for curl/another scheduler/tests),
  checked inside this module.
"""
from __future__ import annotations

import logging
import os
import secrets
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field, ValidationError

from chorus.email import EmailSender
from chorus.feeds import SubscriptionPreview, preview_sources
from chorus.jobs import JobStore
from chorus.pipeline import Deps
from chorus.quotas import QuotaExceeded, enforce_job_quota
from chorus.saved_items import SavedItemStore, saved_queue_lister
from chorus.scheduler import due_subscriptions, next_run, run_subscription
from chorus.subscriptions import (
    DEFAULT_LOOKBACK_DAYS,
    DEFAULT_MAX_EPISODES_PER_RUN,
    MASTER_OWNER,
    MAX_EPISODES_PER_RUN,
    MAX_LOOKBACK_DAYS,
    MAX_SOURCES,
    Source,
    Subscription,
    SubscriptionCreate,
    SubscriptionStore,
    SubscriptionUpdate,
    verify_unsubscribe_token,
)

log = logging.getLogger("chorus.subscriptions_api")

# The deployed service's own public URL, used to build absolute links (the
# rendered audio episode, the unsubscribe link) in an email — a serverless
# process cannot infer this from a cron-triggered invocation the way it can
# from an incoming request's Host header. Falls back to the triggering
# request's own base_url (fine for local/dev), then a hardcoded local URL.
CHORUS_PUBLIC_URL_ENV = "CHORUS_PUBLIC_URL"
DEFAULT_LOCAL_BASE_URL = "http://localhost:8000"

UNSUBSCRIBED_BODY = "You have been unsubscribed from this Chorus digest.\n"


def resolve_base_url(request: Request | None = None) -> str:
    configured = os.environ.get(CHORUS_PUBLIC_URL_ENV)
    if configured:
        return configured
    if request is not None:
        return str(request.base_url)
    return DEFAULT_LOCAL_BASE_URL


def _owner(request: Request) -> str:
    return getattr(request.state, "owner", MASTER_OWNER)


def _get_owned_or_404(subscription_store: SubscriptionStore, subscription_id: str, owner: str) -> Subscription:
    sub = subscription_store.get(subscription_id)
    if sub is None or (owner != MASTER_OWNER and sub.owner != owner):
        # Same 404 either way: a caller must not be able to tell "exists,
        # not yours" from "does not exist" (enumeration).
        raise HTTPException(status_code=404, detail="unknown subscription_id")
    return sub


class PreviewRequest(BaseModel):
    """POST /subscriptions/preview request body."""

    sources: list[Source] = Field(
        min_length=1, max_length=MAX_SOURCES, description="Sources to check for recent episodes."
    )
    lookback_days: int = Field(
        default=DEFAULT_LOOKBACK_DAYS,
        ge=1,
        le=MAX_LOOKBACK_DAYS,
        description="Include episodes published within this many days (a first run's window).",
    )
    max_episodes_per_run: int = Field(
        default=DEFAULT_MAX_EPISODES_PER_RUN,
        ge=1,
        le=MAX_EPISODES_PER_RUN,
        description="Per-run episode cap shared round-robin across sources.",
    )


def preview_for(
    request: PreviewRequest,
    owner: str = MASTER_OWNER,
    saved_items: SavedItemStore | None = None,
) -> SubscriptionPreview:
    saved = (
        saved_queue_lister(saved_items, owner, datetime.now(UTC))
        if saved_items is not None
        else None
    )
    return preview_sources(
        request.sources,
        lookback_days=request.lookback_days,
        max_episodes_per_run=request.max_episodes_per_run,
        saved=saved,
    )


def new_subscription(payload: SubscriptionCreate, owner: str, now: datetime) -> Subscription:
    """The stored Subscription for a create request (shared by the HTTP route
    and the MCP `subscribe` tool)."""
    return Subscription(
        subscription_id=uuid.uuid4().hex,
        owner=owner,
        email=payload.email,
        soul=payload.soul,
        context=payload.context,
        episodes=payload.episodes,
        shows=payload.shows,
        sources=payload.sources,
        highlight_count=payload.highlight_count,
        profile=payload.profile,
        cadence=payload.cadence,
        max_episodes_per_run=payload.max_episodes_per_run,
        lookback_days_first_run=payload.lookback_days_first_run,
        notify_when_empty=payload.notify_when_empty,
        next_run_at=next_run(payload.cadence, now),
        active=True,
        created_at=now,
    )


def apply_update(sub: Subscription, payload: SubscriptionUpdate) -> Subscription:
    """`sub` with the supplied fields of `payload` applied. A subscription
    has exactly one input mode, so setting `sources` clears the legacy
    episodes/shows (unless the same request sets them) and setting legacy
    episodes/shows clears `sources`. `seen_episode_ids` and the run cursor
    are kept. Raises ValueError when the result is invalid."""
    updates = payload.model_dump(exclude_unset=True)
    if updates.get("sources"):
        updates.setdefault("episodes", None)
        updates.setdefault("shows", None)
    elif updates.get("episodes") or updates.get("shows"):
        updates.setdefault("sources", None)
    merged = sub.model_dump(mode="json")
    merged.update(updates)
    try:
        return Subscription.model_validate(merged)
    except ValidationError as err:
        raise ValueError(str(err)) from err


def get_owned(subscription_store: SubscriptionStore, subscription_id: str, owner: str) -> Subscription:
    """Owner-scoped fetch; raises ValueError("unknown subscription_id") for a
    missing or foreign id alike (no enumeration signal)."""
    sub = subscription_store.get(subscription_id)
    if sub is None or (owner != MASTER_OWNER and sub.owner != owner):
        raise ValueError("unknown subscription_id")
    return sub


def build_subscriptions_router(
    subscription_store: SubscriptionStore,
    store: JobStore,
    deps: Deps,
    email_sender: EmailSender,
    cron_secret: str | None,
    saved_items: SavedItemStore | None = None,
) -> APIRouter:
    router = APIRouter()

    @router.post("/subscriptions")
    def create_subscription(payload: SubscriptionCreate, request: Request) -> Subscription:
        subscription = new_subscription(payload, _owner(request), datetime.now(UTC))
        subscription_store.create(subscription)
        return subscription

    @router.post("/subscriptions/preview")
    def preview_subscription(payload: PreviewRequest, request: Request) -> SubscriptionPreview:
        """What the next run would pick up for these sources; saves nothing."""
        return preview_for(payload, _owner(request), saved_items)

    @router.get("/subscriptions")
    def list_subscriptions(request: Request) -> list[Subscription]:
        owner = _owner(request)
        return subscription_store.list(owner=None if owner == MASTER_OWNER else owner)

    @router.get("/subscriptions/{subscription_id}")
    def get_subscription(subscription_id: str, request: Request) -> Subscription:
        return _get_owned_or_404(subscription_store, subscription_id, _owner(request))

    @router.patch("/subscriptions/{subscription_id}")
    def update_subscription(
        subscription_id: str, payload: SubscriptionUpdate, request: Request
    ) -> Subscription:
        sub = _get_owned_or_404(subscription_store, subscription_id, _owner(request))
        try:
            updated = apply_update(sub, payload)
        except ValueError as err:
            raise HTTPException(status_code=422, detail=str(err)) from err
        subscription_store.save(updated)
        return updated

    @router.delete("/subscriptions/{subscription_id}", status_code=204)
    def delete_subscription(subscription_id: str, request: Request) -> Response:
        _get_owned_or_404(subscription_store, subscription_id, _owner(request))
        subscription_store.delete(subscription_id)
        return Response(status_code=204)

    @router.post("/subscriptions/{subscription_id}/run")
    def run_subscription_now(subscription_id: str, request: Request) -> dict[str, str | None]:
        sub = _get_owned_or_404(subscription_store, subscription_id, _owner(request))
        try:
            # R3: quota is the SUBSCRIPTION's owner's, not necessarily the
            # caller's — the master token may run-now someone else's
            # subscription, and it is that owner's budget being spent.
            enforce_job_quota(store, sub.owner)
        except QuotaExceeded as err:
            raise HTTPException(status_code=429, detail=str(err)) from err
        base_url = resolve_base_url(request)
        job_id = run_subscription(
            sub,
            store,
            deps,
            subscription_store,
            email_sender,
            base_url,
            datetime.now(UTC),
            saved_items=saved_items,
        )
        summary = sub.last_run_summary
        return {"job_id": job_id, "skipped_reason": summary.skipped_reason if summary else None}

    @router.get("/subscriptions/{subscription_id}/unsubscribe")
    def unsubscribe(subscription_id: str, token: str) -> Response:
        sub = subscription_store.get(subscription_id)
        if sub is None:
            raise HTTPException(status_code=404, detail="unknown subscription_id")
        if not verify_unsubscribe_token(subscription_id, token):
            raise HTTPException(status_code=403, detail="invalid unsubscribe token")
        if sub.active:  # idempotent: a second click with the same valid link just no-ops
            sub.active = False
            subscription_store.save(sub)
        return Response(content=UNSUBSCRIBED_BODY, media_type="text/plain")

    # GET *and* POST: Vercel Cron always issues a GET to the configured
    # `path` (vercel.json's "crons" entry) and adds the Authorization
    # header itself when CRON_SECRET is set as a project env var — it does
    # not support choosing POST. GET is the one Vercel Cron actually calls;
    # POST is kept too so the same route also works as an ordinary
    # POST-triggered action (curl, another scheduler, tests).
    @router.api_route("/internal/cron/tick", methods=["GET", "POST"])
    def cron_tick(request: Request) -> dict[str, object]:
        if not cron_secret:
            raise HTTPException(status_code=503, detail="CRON_SECRET is not configured")
        scheme, _, credential = request.headers.get("authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not secrets.compare_digest(credential.strip(), cron_secret):
            raise HTTPException(status_code=401, detail="missing or invalid cron bearer token")

        now = datetime.now(UTC)
        base_url = resolve_base_url(request)
        ran: list[dict[str, str | None]] = []
        for sub in due_subscriptions(subscription_store, now):
            job_id = run_subscription(
                sub, store, deps, subscription_store, email_sender, base_url, now,
                saved_items=saved_items,
            )
            ran.append({"subscription_id": sub.subscription_id, "job_id": job_id})
        return {"ran": len(ran), "subscriptions": ran}

    return router
