"""Subscriptions (Phase F, docs/DEVELOPMENT_PLAN.md §3 and §8 row F): a
subscription is a stored digest request plus a schedule and a delivery
address. `chorus/scheduler.py` fans a due subscription out into one digest
job (via the same `chorus.pipeline.run_job` orchestrator every other trigger
uses) and emails the result; `chorus/subscriptions_api.py` is the CRUD +
unsubscribe surface.

`SubscriptionStore` mirrors `chorus.jobs.JobStore`'s split: a Protocol every
backend implements, `SqliteSubscriptionStore` for local/dev/test, and
`chorus.stores.postgres.PostgresSubscriptionStore` for Vercel — selected by
`chorus.config_env.select_subscription_store` the same way the job store is.

Unsubscribe links are signed with HMAC-SHA256 (`unsubscribe_token` /
`verify_unsubscribe_token`) rather than trusting a bare subscription id in a
public, unauthenticated URL.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, Field, field_validator, model_validator

from chorus.jobs import DEFAULT_DB, MASTER_OWNER
from chorus.models import (
    MAX_CONTEXT_CHARS,
    MAX_EPISODES,
    MAX_HIGHLIGHTS,
    MAX_SOUL_CHARS,
    EpisodeInput,
    EpisodeProfile,
)

log = logging.getLogger("chorus.subscriptions")

# HMAC secret for unsubscribe tokens. Falls back to the master API token (a
# secret Chorus already holds server-side) and, failing that, a per-process
# random secret — logged loudly, since links signed with it stop verifying
# across a restart.
UNSUBSCRIBE_SECRET_ENV = "CHORUS_UNSUBSCRIBE_SECRET"
API_TOKEN_FALLBACK_ENV = "CHORUS_API_TOKEN"

CADENCES = ("weekly", "daily")
# MASTER_OWNER re-exported from chorus.jobs (the canonical definition, needed
# there too for JobStore.create's default) — kept importable from this module
# since chorus.app/chorus.subscriptions_api/tests already do `from
# chorus.subscriptions import MASTER_OWNER`.
__all__ = [
    "MASTER_OWNER",
    "Subscription",
    "SubscriptionCreate",
    "SubscriptionUpdate",
    "SubscriptionList",
    "SubscriptionStore",
    "SqliteSubscriptionStore",
    "unsubscribe_token",
    "verify_unsubscribe_token",
]

_random_secret: str | None = None


def _unsubscribe_secret() -> bytes:
    secret = os.environ.get(UNSUBSCRIBE_SECRET_ENV) or os.environ.get(API_TOKEN_FALLBACK_ENV)
    if secret:
        return secret.encode("utf-8")
    global _random_secret
    if _random_secret is None:
        _random_secret = secrets.token_urlsafe(32)
        log.warning(
            "subscriptions: neither %s nor %s is set — using a per-process random "
            "unsubscribe secret (existing links will stop verifying after a restart)",
            UNSUBSCRIBE_SECRET_ENV,
            API_TOKEN_FALLBACK_ENV,
        )
    return _random_secret.encode("utf-8")


def unsubscribe_token(subscription_id: str) -> str:
    """HMAC-SHA256 of `subscription_id` under the server's unsubscribe secret."""
    return hmac.new(
        _unsubscribe_secret(), subscription_id.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def verify_unsubscribe_token(subscription_id: str, token: str) -> bool:
    return hmac.compare_digest(unsubscribe_token(subscription_id), token)


def _require_tz_aware(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value


class Subscription(BaseModel):
    """A stored digest request plus a schedule and a delivery address."""

    subscription_id: str = Field(description="Opaque identifier for this subscription.")
    owner: str = Field(
        description='The issuing key\'s email, or "master" for the master token.'
    )
    email: str = Field(
        min_length=3,
        max_length=320,
        pattern=r"^[^\s@]+@[^\s@]+\.[^\s@]+$",
        description="Delivery address the digest email is sent to.",
    )
    soul: str = Field(
        min_length=1,
        max_length=MAX_SOUL_CHARS,
        description="Markdown persona and curation lens for the principal.",
    )
    context: str = Field(
        max_length=MAX_CONTEXT_CHARS,
        description="Current projects, reading, and priorities that tune relevance.",
    )
    episodes: list[EpisodeInput] | None = Field(
        default=None,
        max_length=MAX_EPISODES,
        description="Explicit episodes to digest each run; provide this or shows.",
    )
    shows: list[str] | None = Field(
        default=None,
        max_length=MAX_EPISODES,
        description="Catalog show names to resolve to episodes each run; provide this or episodes.",
    )
    highlight_count: int = Field(
        default=4,
        ge=1,
        le=MAX_HIGHLIGHTS,
        description="Maximum highlights to surface per episode.",
    )
    profile: EpisodeProfile | None = Field(
        default=None,
        description="Episode format and speakers; omit for the single-voice monologue default.",
    )
    cadence: Literal["weekly", "daily"] = Field(
        default="weekly", description='Run schedule: "weekly" (Friday) or "daily".'
    )
    next_run_at: datetime = Field(description="UTC timestamp of the next scheduled run.")
    active: bool = Field(default=True, description="False once unsubscribed or paused.")
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC), description="UTC creation timestamp."
    )
    last_job_id: str | None = Field(
        default=None, description="job_id of the most recent run, if any."
    )
    last_run_at: datetime | None = Field(
        default=None, description="UTC timestamp of the most recent run, if any."
    )

    @field_validator("next_run_at", "created_at")
    @classmethod
    def _validate_required_tz(cls, value: datetime, info) -> datetime:  # type: ignore[no-untyped-def]
        return _require_tz_aware(value, info.field_name)

    @field_validator("last_run_at")
    @classmethod
    def _validate_optional_tz(cls, value: datetime | None, info) -> datetime | None:  # type: ignore[no-untyped-def]
        if value is None:
            return value
        return _require_tz_aware(value, info.field_name)

    @model_validator(mode="after")
    def _episodes_or_shows(self) -> Subscription:
        if not self.episodes and not self.shows:
            raise ValueError("a subscription requires either episodes or shows")
        return self


class SubscriptionCreate(BaseModel):
    """POST /subscriptions request body: everything the caller controls.
    Server-assigned fields (subscription_id, owner, created_at, next_run_at,
    active) are added by the route handler."""

    email: str = Field(
        min_length=3,
        max_length=320,
        pattern=r"^[^\s@]+@[^\s@]+\.[^\s@]+$",
        description="Delivery address the digest email is sent to.",
    )
    soul: str = Field(
        min_length=1,
        max_length=MAX_SOUL_CHARS,
        description="Markdown persona and curation lens for the principal.",
    )
    context: str = Field(
        default="",
        max_length=MAX_CONTEXT_CHARS,
        description="Current projects, reading, and priorities that tune relevance.",
    )
    episodes: list[EpisodeInput] | None = Field(
        default=None,
        max_length=MAX_EPISODES,
        description="Explicit episodes to digest each run; provide this or shows.",
    )
    shows: list[str] | None = Field(
        default=None,
        max_length=MAX_EPISODES,
        description="Catalog show names to resolve to episodes each run; provide this or episodes.",
    )
    highlight_count: int = Field(
        default=4,
        ge=1,
        le=MAX_HIGHLIGHTS,
        description="Maximum highlights to surface per episode.",
    )
    profile: EpisodeProfile | None = Field(
        default=None,
        description="Episode format and speakers; omit for the single-voice monologue default.",
    )
    cadence: Literal["weekly", "daily"] = Field(
        default="weekly", description='Run schedule: "weekly" (Friday) or "daily".'
    )

    @model_validator(mode="after")
    def _episodes_or_shows(self) -> SubscriptionCreate:
        if not self.episodes and not self.shows:
            raise ValueError("a subscription requires either episodes or shows")
        return self


class SubscriptionUpdate(BaseModel):
    """PATCH /subscriptions/{id} request body: every field optional, only
    supplied fields are applied."""

    context: str | None = Field(
        default=None,
        max_length=MAX_CONTEXT_CHARS,
        description="Replacement context (context refresh before the next run).",
    )
    active: bool | None = Field(
        default=None, description="Set false to pause without deleting, true to resume."
    )
    cadence: Literal["weekly", "daily"] | None = Field(
        default=None, description='Change the run schedule: "weekly" or "daily".'
    )
    episodes: list[EpisodeInput] | None = Field(
        default=None, max_length=MAX_EPISODES, description="Replace the explicit episode list."
    )
    shows: list[str] | None = Field(
        default=None, max_length=MAX_EPISODES, description="Replace the catalog show list."
    )
    highlight_count: int | None = Field(
        default=None, ge=1, le=MAX_HIGHLIGHTS, description="Replace the per-episode highlight cap."
    )


# A plain `list[Subscription]` alias, used as every list-returning method's
# annotation below: a Protocol/class that defines a method literally named
# `list` shadows the builtin `list` name for the rest of ITS OWN class body's
# (deferred, `from __future__ import annotations`) annotations, which
# confuses mypy on unrelated methods like `due`. The alias sidesteps it.
SubscriptionList = list[Subscription]


@runtime_checkable
class SubscriptionStore(Protocol):
    def create(self, subscription: Subscription) -> str: ...

    def get(self, subscription_id: str) -> Subscription | None: ...

    def list(self, owner: str | None = None) -> SubscriptionList: ...

    def save(self, subscription: Subscription) -> None: ...

    def delete(self, subscription_id: str) -> None: ...

    def due(self, now: datetime) -> SubscriptionList: ...

    def close(self) -> None: ...


class SqliteSubscriptionStore:
    """`subscriptions(subscription_id TEXT PRIMARY KEY, owner TEXT NOT NULL,
    active INTEGER NOT NULL, next_run_at TEXT NOT NULL, payload TEXT NOT
    NULL)` in the local `chorus.db` by default, with `owner`/`active`/
    `next_run_at` broken out as real columns (not just JSON in `payload`) so
    `due()` and owner-scoped `list()` are indexed queries, not a full scan."""

    def __init__(self, db_path: Path | str = DEFAULT_DB) -> None:
        self.db_path = str(db_path)
        # check_same_thread=False: the scheduler/cron path may run on a
        # worker thread, exactly like chorus.jobs.SqliteJobStore.
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock:
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS subscriptions ("
                "subscription_id TEXT PRIMARY KEY, owner TEXT NOT NULL, "
                "active INTEGER NOT NULL, next_run_at TEXT NOT NULL, payload TEXT NOT NULL)"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_subscriptions_due "
                "ON subscriptions (active, next_run_at)"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_subscriptions_owner ON subscriptions (owner)"
            )
            self._conn.commit()

    def _upsert(self, subscription: Subscription) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO subscriptions "
                "(subscription_id, owner, active, next_run_at, payload) VALUES (?, ?, ?, ?, ?)",
                (
                    subscription.subscription_id,
                    subscription.owner,
                    int(subscription.active),
                    subscription.next_run_at.isoformat(),
                    subscription.model_dump_json(),
                ),
            )
            self._conn.commit()

    def create(self, subscription: Subscription) -> str:
        self._upsert(subscription)
        return subscription.subscription_id

    def get(self, subscription_id: str) -> Subscription | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT payload FROM subscriptions WHERE subscription_id = ?", (subscription_id,)
            ).fetchone()
        return Subscription.model_validate_json(row[0]) if row else None

    def list(self, owner: str | None = None) -> SubscriptionList:
        with self._lock:
            if owner is None:
                rows = self._conn.execute("SELECT payload FROM subscriptions").fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT payload FROM subscriptions WHERE owner = ?", (owner,)
                ).fetchall()
        return [Subscription.model_validate_json(r[0]) for r in rows]

    def save(self, subscription: Subscription) -> None:
        self._upsert(subscription)

    def delete(self, subscription_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM subscriptions WHERE subscription_id = ?", (subscription_id,)
            )
            self._conn.commit()

    def due(self, now: datetime) -> SubscriptionList:
        with self._lock:
            rows = self._conn.execute(
                "SELECT payload FROM subscriptions WHERE active = 1 AND next_run_at <= ?",
                (now.isoformat(),),
            ).fetchall()
        return [Subscription.model_validate_json(r[0]) for r in rows]

    def close(self) -> None:
        with self._lock:
            self._conn.close()
