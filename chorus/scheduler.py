"""Subscription scheduler (Phase F, docs/DEVELOPMENT_PLAN.md §3 and §8 row F):
pure schedule arithmetic (`next_run`), due selection (`due_subscriptions`),
and the fan-out body (`run_subscription`) that turns one due subscription
into one digest job — run through the exact same `chorus.pipeline.run_job`
orchestrator every other trigger uses — and emails the result.

Two triggers call `run_subscription` per due subscription, both defined
elsewhere: an Inngest cron function (`chorus/inngest_app.py`, event
`chorus/tick`, `TICK_CRON_SCHEDULE`) and `POST /internal/cron/tick`
(`chorus/subscriptions_api.py`, Vercel Cron convention, guarded by
`CRON_SECRET`) for deployments without Inngest. One tick schedule covers
both "weekly" and "daily" subscriptions — `due()` only compares
`next_run_at` to `now`, so a single `*/30 * * * *` poll is simpler than a
separate weekly + hourly-daily pair of cron functions and never misses a
run by more than the poll interval.

`run_subscription` never leaves a subscription stuck: a failed job still
advances `next_run_at` and sends a short failure email rather than retrying
silently or leaving the subscription due forever.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from chorus import catalog
from chorus.digest_email import render_digest_email, unsubscribe_headers
from chorus.email import EmailSender
from chorus.jobs import JobStore
from chorus.models import DigestRequest, JobStatus
from chorus.pipeline import Deps, run_job
from chorus.subscriptions import Subscription, SubscriptionStore, unsubscribe_token

log = logging.getLogger("chorus.scheduler")

# Friday, UTC (datetime.weekday(): Monday=0 ... Sunday=6). "Friday morning
# ET" per docs/DEVELOPMENT_PLAN.md §3; kept simple as a UTC wall-clock hour
# rather than importing a timezone database (DST-irrelevant by construction:
# the contract is "13:00 UTC", not "9am ET").
WEEKLY_WEEKDAY_UTC = 4
RUN_HOUR_UTC = 13

# Named per docs/DEVELOPMENT_PLAN.md §8 row F: one tick, not a separate
# weekly cron + hourly daily cron. Every trigger (Inngest cron and the
# Vercel-Cron-guarded /internal/cron/tick route) polls on this schedule.
TICK_CRON_SCHEDULE = "*/30 * * * *"

# Vercel Cron convention: `Authorization: Bearer $CRON_SECRET` on the
# internal tick route, exempt from the normal user bearer-token check.
CRON_SECRET_ENV = "CRON_SECRET"

FAILED_DIGEST_SUBJECT = "This week's digest failed"


def next_run(cadence: str, after: datetime) -> datetime:
    """Next scheduled run strictly after `after` (UTC, tz-aware in and out).
    "weekly" -> the next Friday 13:00 UTC; "daily" -> the next 13:00 UTC."""
    if after.tzinfo is None:
        raise ValueError("next_run: `after` must be timezone-aware")
    after = after.astimezone(UTC)

    if cadence == "daily":
        candidate = after.replace(hour=RUN_HOUR_UTC, minute=0, second=0, microsecond=0)
        if candidate <= after:
            candidate += timedelta(days=1)
        return candidate

    if cadence == "weekly":
        candidate = after.replace(hour=RUN_HOUR_UTC, minute=0, second=0, microsecond=0)
        days_ahead = (WEEKLY_WEEKDAY_UTC - candidate.weekday()) % 7
        candidate += timedelta(days=days_ahead)
        if candidate <= after:
            candidate += timedelta(days=7)
        return candidate

    raise ValueError(f"next_run: unknown cadence {cadence!r}")


def due_subscriptions(store: SubscriptionStore, now: datetime) -> list[Subscription]:
    """Thin, named wrapper over `store.due` so callers (the Inngest tick
    function, the /internal/cron/tick route, tests) depend on one seam
    rather than the store directly."""
    return store.due(now)


def _build_request(subscription: Subscription) -> DigestRequest:
    if subscription.episodes:
        episodes = subscription.episodes
    else:
        assert subscription.shows, "Subscription requires episodes or shows (model_validator)"
        episodes = catalog.resolve(shows=subscription.shows)
        if not episodes:
            raise ValueError(
                f"subscription {subscription.subscription_id}: shows "
                f"{subscription.shows!r} resolved to no episodes"
            )
    return DigestRequest(
        soul=subscription.soul,
        context=subscription.context,
        episodes=episodes,
        highlight_count=subscription.highlight_count,
        profile=subscription.profile,
    )


def _unsubscribe_url(base_url: str, subscription_id: str) -> str:
    token = unsubscribe_token(subscription_id)
    return f"{base_url.rstrip('/')}/subscriptions/{subscription_id}/unsubscribe?token={token}"


def run_subscription(
    subscription: Subscription,
    store: JobStore,
    deps: Deps,
    subscription_store: SubscriptionStore,
    email_sender: EmailSender,
    base_url: str,
    now: datetime,
) -> str:
    """Create a job, run it in-process via chorus.pipeline.run_job (the same
    orchestrator BackgroundRunner uses), deliver the result email, record
    last_job_id/last_run_at, and advance next_run_at — unconditionally, even
    when the job or the email failed, so a subscription is never left due
    forever. Returns the job_id."""
    # R4: the job belongs to the subscription's owner, not necessarily
    # whoever triggered this run (the master token can run-now someone
    # else's subscription; the resulting job must still only be readable by
    # that subscription's own owner, or master).
    job_id = store.create(owner=subscription.owner)
    try:
        request = _build_request(subscription)
        run_job(job_id, request, store, deps)
    except Exception as err:  # noqa: BLE001 - mirrors run_job's own terminal-state guarantee
        log.exception("subscription %s: job %s did not run", subscription.subscription_id, job_id)
        job = store.get(job_id)
        if job is not None:
            job.status = JobStatus.failed
            job.error = f"{type(err).__name__}: {err}"
            store.save(job)

    job = store.get(job_id)
    assert job is not None  # store.create() above guarantees a row exists

    unsubscribe_url = _unsubscribe_url(base_url, subscription.subscription_id)
    headers = unsubscribe_headers(unsubscribe_url)
    try:
        if job.status == JobStatus.done and job.digest is not None:
            content = render_digest_email(job, subscription, base_url, unsubscribe_url)
            email_sender.send(
                subscription.email, content.subject, content.text, content.html, headers
            )
        else:
            reason = job.error or "unknown error"
            text = f"This week's digest failed: {reason}\n\nUnsubscribe: {unsubscribe_url}\n"
            email_sender.send(subscription.email, FAILED_DIGEST_SUBJECT, text, headers=headers)
    except Exception:  # noqa: BLE001 - delivery failure must not strand the schedule
        log.exception("subscription %s: email delivery failed", subscription.subscription_id)

    subscription.last_job_id = job_id
    subscription.last_run_at = now
    subscription.next_run_at = next_run(subscription.cadence, now)
    subscription_store.save(subscription)
    return job_id
