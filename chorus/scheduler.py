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

Two subscription shapes:

- Feed subscriptions (`sources` set): each run lists every source's episodes
  published since the cursor (`last_run_at`, or `lookback_days_first_run`
  before now on the first run), drops ids already in `seen_episode_ids`,
  picks up to `max_episodes_per_run` round-robin across sources, digests
  them, and records what was sent. A run with nothing new creates no job.
- Legacy subscriptions (`episodes` / `shows`): unchanged — the same fixed
  request is digested every run.

`run_subscription` never leaves a subscription stuck: a failed job still
advances `next_run_at` and sends a short failure email rather than retrying
silently or leaving the subscription due forever. For a feed subscription a
failed job additionally leaves the cursor and seen-list alone, so the same
episodes are retried on the next run instead of being lost.
"""
from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from chorus import catalog, feeds
from chorus.digest_email import (
    render_digest_email,
    render_empty_email,
    unsubscribe_headers,
)
from chorus.email import EmailSender
from chorus.jobs import JobStore
from chorus.models import DigestRequest, EpisodeInput, Job, JobStatus
from chorus.netguard import Resolver
from chorus.pipeline import Deps, run_job
from chorus.quotas import QuotaExceeded, enforce_job_quota
from chorus.subscriptions import (
    MAX_SKIPPED_REASON_CHARS,
    SEEN_EPISODE_IDS_MAX,
    LastRunSummary,
    Subscription,
    SubscriptionStore,
    unsubscribe_token,
)

if TYPE_CHECKING:
    from chorus.saved_items import SavedItemStore

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
SKIPPED_DIGEST_SUBJECT = "This week's digest was skipped"
NO_NEW_EPISODES = "no new episodes"
# Episodes listed per source before the seen filter. Comfortably above the
# per-run cap (MAX_EPISODES_PER_RUN is 20), so already-seen items near the top
# of a feed cannot hide a new one.
PER_SOURCE_LIST_LIMIT = feeds.PREVIEW_LIST_LIMIT

FeedLister = feeds.FeedLister


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
    return _request_for(subscription, episodes)


def _request_for(subscription: Subscription, episodes: Sequence[EpisodeInput]) -> DigestRequest:
    return DigestRequest(
        soul=subscription.soul,
        soul_origin=subscription.soul_origin,
        context=subscription.context,
        episodes=list(episodes),
        highlight_count=subscription.highlight_count,
        profile=subscription.profile,
    )


def _unsubscribe_url(base_url: str, subscription_id: str) -> str:
    token = unsubscribe_token(subscription_id)
    return f"{base_url.rstrip('/')}/subscriptions/{subscription_id}/unsubscribe?token={token}"


def _execute_and_deliver(
    subscription: Subscription,
    store: JobStore,
    deps: Deps,
    email_sender: EmailSender,
    base_url: str,
    build_request: Callable[[], DigestRequest],
    *,
    episodes: Sequence[EpisodeInput] | None = None,
    feed_errors: Sequence[str] = (),
    not_included: int = 0,
) -> Job:
    """Create a job owned by the subscription's owner, run it in-process via
    chorus.pipeline.run_job (the same orchestrator BackgroundRunner uses), and
    email the outcome. Returns the finished job; never raises."""
    # R4: the job belongs to the subscription's owner, not necessarily
    # whoever triggered this run (the master token can run-now someone
    # else's subscription; the resulting job must still only be readable by
    # that subscription's own owner, or master).
    job_id = store.create(owner=subscription.owner)
    try:
        request = build_request()
        run_job(job_id, request, store, deps)
    except Exception as err:
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
            content = render_digest_email(
                job,
                subscription,
                base_url,
                unsubscribe_url,
                episodes=episodes,
                feed_errors=feed_errors,
                not_included=not_included,
            )
            email_sender.send(
                subscription.email, content.subject, content.text, content.html, headers
            )
        else:
            reason = job.error or "unknown error"
            text = f"This week's digest failed: {reason}\n\n"
            if feed_errors:
                text += "Could not check:\n" + "".join(f"- {line}\n" for line in feed_errors) + "\n"
            text += f"Unsubscribe: {unsubscribe_url}\n"
            email_sender.send(subscription.email, FAILED_DIGEST_SUBJECT, text, headers=headers)
    except Exception:
        log.exception("subscription %s: email delivery failed", subscription.subscription_id)
    return job


def _clip(text: str) -> str:
    return text if len(text) <= MAX_SKIPPED_REASON_CHARS else text[: MAX_SKIPPED_REASON_CHARS - 1] + "…"


def _run_feed_subscription(
    subscription: Subscription,
    store: JobStore,
    deps: Deps,
    subscription_store: SubscriptionStore,
    email_sender: EmailSender,
    base_url: str,
    now: datetime,
    lister: FeedLister | None,
    resolver: Resolver | None,
    saved_items: SavedItemStore | None,
) -> str | None:
    sources = subscription.sources
    assert sources, "feed subscription requires sources"
    since = subscription.last_run_at or now - timedelta(days=subscription.lookback_days_first_run)
    seen = set(subscription.seen_episode_ids)
    saved = None
    if saved_items is not None:
        from chorus.saved_items import saved_queue_lister

        saved = saved_queue_lister(saved_items, subscription.owner, now)

    gathered = feeds.gather_episodes(
        sources,
        since,
        PER_SOURCE_LIST_LIMIT,
        resolver=resolver,
        lister=lister,
        saved=saved,
        exclude_ids=frozenset(seen),
    )
    fresh = [
        [e for e in listed if e.episode.resolved_id() not in seen] for listed in gathered.per_source
    ]
    picked = feeds.round_robin(fresh, subscription.max_episodes_per_run)
    distinct_fresh = {e.episode.resolved_id() for listed in fresh for e in listed}
    not_included = len(distinct_fresh) - len(picked)

    feed_errors = [f"{feeds.source_label(e.source)}: {e.reason}" for e in gathered.errors]
    all_failed = len(gathered.errors) == len(sources)
    unsubscribe_url = _unsubscribe_url(base_url, subscription.subscription_id)

    if not picked:
        reason = NO_NEW_EPISODES
        if feed_errors:
            reason += f"; {len(feed_errors)} of {len(sources)} source(s) could not be read"
        if subscription.notify_when_empty:
            content = render_empty_email(
                subscription,
                unsubscribe_url,
                sources_checked=[feeds.source_label(s) for s in sources],
                since=since,
                feed_errors=feed_errors,
            )
            try:
                email_sender.send(
                    subscription.email,
                    content.subject,
                    content.text,
                    content.html,
                    unsubscribe_headers(unsubscribe_url),
                )
            except Exception:
                log.exception(
                    "subscription %s: empty-week email delivery failed",
                    subscription.subscription_id,
                )
        subscription.last_run_summary = LastRunSummary(
            ran_at=now, new_episodes=0, job_id=None, skipped_reason=_clip(reason)
        )
        if not all_failed:
            # Nothing was missed: every source answered. When all of them
            # failed, episodes may exist that we could not see, so the cursor
            # stays put and the next run covers the gap.
            subscription.last_run_at = now
        subscription.next_run_at = next_run(subscription.cadence, now)
        subscription_store.save(subscription)
        return None

    try:
        enforce_job_quota(store, subscription.owner)
    except QuotaExceeded as err:
        log.warning("subscription %s: quota exceeded: %s", subscription.subscription_id, err)
        try:
            email_sender.send(
                subscription.email,
                SKIPPED_DIGEST_SUBJECT,
                f"This week's digest was skipped: {err}\n\nThe new episodes will be "
                f"included in the next run.\n\nUnsubscribe: {unsubscribe_url}\n",
                headers=unsubscribe_headers(unsubscribe_url),
            )
        except Exception:
            log.exception("subscription %s: email delivery failed", subscription.subscription_id)
        subscription.last_run_summary = LastRunSummary(
            ran_at=now, new_episodes=0, job_id=None, skipped_reason=_clip(f"quota exceeded: {err}")
        )
        subscription.next_run_at = next_run(subscription.cadence, now)
        subscription_store.save(subscription)
        return None

    episodes = [e.episode for e in picked]
    job = _execute_and_deliver(
        subscription,
        store,
        deps,
        email_sender,
        base_url,
        lambda: _request_for(subscription, episodes),
        episodes=episodes,
        feed_errors=feed_errors,
        not_included=not_included,
    )

    subscription.last_job_id = job.job_id
    if job.status == JobStatus.done:
        subscription.seen_episode_ids = (
            subscription.seen_episode_ids + [ep.resolved_id() for ep in episodes]
        )[-SEEN_EPISODE_IDS_MAX:]
        subscription.last_run_at = now
        subscription.last_run_summary = LastRunSummary(
            ran_at=now, new_episodes=len(episodes), job_id=job.job_id, skipped_reason=None
        )
    else:
        # Cursor and seen-list untouched: the same episodes are retried.
        subscription.last_run_summary = LastRunSummary(
            ran_at=now,
            new_episodes=0,
            job_id=job.job_id,
            skipped_reason=_clip(f"digest job failed: {job.error or 'unknown error'}"),
        )
    subscription.next_run_at = next_run(subscription.cadence, now)
    subscription_store.save(subscription)
    return job.job_id


def run_subscription(
    subscription: Subscription,
    store: JobStore,
    deps: Deps,
    subscription_store: SubscriptionStore,
    email_sender: EmailSender,
    base_url: str,
    now: datetime,
    *,
    lister: FeedLister | None = None,
    resolver: Resolver | None = None,
    saved_items: SavedItemStore | None = None,
) -> str | None:
    """Run one due subscription and return the digest job_id, or None when a
    feed subscription found no new episodes (no job is created then).

    Legacy (episodes/shows) subscriptions: create a job, run it in-process,
    deliver the email, record last_job_id/last_run_at, and advance
    next_run_at — unconditionally, even when the job or the email failed, so a
    subscription is never left due forever. Feed subscriptions follow the
    same schedule guarantee; see the module docstring for the cursor rules.

    `lister`/`resolver` are test seams for feed listing and DNS.
    `saved_items` backs any SavedQueueSource (without it those sources
    report as unavailable and the rest of the run proceeds)."""
    if subscription.sources:
        return _run_feed_subscription(
            subscription,
            store,
            deps,
            subscription_store,
            email_sender,
            base_url,
            now,
            lister,
            resolver,
            saved_items,
        )

    job = _execute_and_deliver(
        subscription, store, deps, email_sender, base_url, lambda: _build_request(subscription)
    )
    subscription.last_job_id = job.job_id
    subscription.last_run_at = now
    subscription.next_run_at = next_run(subscription.cadence, now)
    subscription_store.save(subscription)
    return job.job_id
