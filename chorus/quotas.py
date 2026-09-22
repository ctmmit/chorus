"""Per-owner spend quotas (docs/REVIEW_WAVE1.md #3): a rolling-24h job-count
ceiling and a concurrent in-flight-job ceiling, enforced BEFORE job creation
by every job-submitting route/tool: `POST /digest`, `POST /digest/select`,
`POST /subscriptions/{id}/run`, and the MCP submit tools. The master owner
(the deployer's own CHORUS_API_TOKEN) is exempt — it is not a self-served
principal we need to protect the deployment from.

Kept as its own module (not inline in chorus.app) so chorus.mcp_server and
chorus.subscriptions_api enforce the exact same policy without importing
chorus.app.
"""
from __future__ import annotations

import logging
import os
from datetime import UTC, datetime, timedelta

from chorus.jobs import JobStore, MASTER_OWNER

log = logging.getLogger("chorus.quotas")

MAX_JOBS_PER_DAY_ENV = "CHORUS_MAX_JOBS_PER_DAY"
MAX_INFLIGHT_JOBS_ENV = "CHORUS_MAX_INFLIGHT_JOBS"
DEFAULT_MAX_JOBS_PER_DAY = 20
DEFAULT_MAX_INFLIGHT_JOBS = 3
ROLLING_WINDOW = timedelta(hours=24)


class QuotaExceeded(RuntimeError):
    """Raised by `enforce_job_quota` when `owner` is over its rolling-window
    job count or concurrent in-flight job count. Callers turn this into a 429
    (HTTP) or a tool error (MCP)."""


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        log.warning("quotas: %s=%r is not an integer; using default %d", name, raw, default)
        return default
    return value if value > 0 else default


def max_jobs_per_day() -> int:
    return _int_env(MAX_JOBS_PER_DAY_ENV, DEFAULT_MAX_JOBS_PER_DAY)


def max_inflight_jobs() -> int:
    return _int_env(MAX_INFLIGHT_JOBS_ENV, DEFAULT_MAX_INFLIGHT_JOBS)


def enforce_job_quota(store: JobStore, owner: str) -> None:
    """Raise QuotaExceeded before a new job is created for `owner`. No-op for
    the master owner (see module docstring)."""
    if owner == MASTER_OWNER:
        return
    since = datetime.now(UTC) - ROLLING_WINDOW
    limit_daily = max_jobs_per_day()
    daily = store.count_for_owner(owner, since)
    if daily >= limit_daily:
        raise QuotaExceeded(
            f"owner has created {daily} job(s) in the last 24h (limit {limit_daily}); try again later"
        )
    limit_inflight = max_inflight_jobs()
    in_flight = store.count_in_flight(owner)
    if in_flight >= limit_inflight:
        raise QuotaExceeded(
            f"owner has {in_flight} job(s) already in flight (limit {limit_inflight}); "
            "wait for one to finish"
        )
