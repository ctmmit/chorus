"""API-key issuance and validation backed by SQLite.

R3/R23 (docs/REVIEW_WAVE1.md #3/#23): self-serve issuance is guarded three
ways before a token is ever minted — a per-IP-per-hour token bucket
(`IpIssueRateLimiter`, in-process, so on serverless each instance keeps its
own budget: documented in DEPLOY.md, not a global ceiling), an optional
email-domain allowlist (`email_domain_allowed`), and the pre-existing
per-email-per-hour limit below. If delivery fails AFTER a key is minted, the
caller (chorus.app's `POST /keys`) revokes it; `issue`'s own per-email check
excludes revoked rows so that compensating revoke never itself blocks a
retry.
"""
from __future__ import annotations

import hashlib
import logging
import os
import secrets
import sqlite3
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol, runtime_checkable

from chorus.jobs import DEFAULT_DB

log = logging.getLogger("chorus.keys")

KEY_PREFIX = "chorus_"
KEY_BYTES = 32
KEY_RATE_LIMIT = timedelta(hours=1)

# R3: per-IP token-bucket ceiling on POST /keys, and an optional allowlist of
# email domains that may self-serve a key at all.
KEY_ISSUE_PER_IP_PER_HOUR_ENV = "CHORUS_KEY_ISSUE_PER_IP_PER_HOUR"
DEFAULT_KEY_ISSUE_PER_IP_PER_HOUR = 3
KEY_ALLOWED_EMAIL_DOMAINS_ENV = "CHORUS_KEY_ALLOWED_EMAIL_DOMAINS"


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        log.warning("keys: %s=%r is not an integer; using default %d", name, raw, default)
        return default
    return value if value > 0 else default


def key_issue_per_ip_per_hour() -> int:
    return _int_env(KEY_ISSUE_PER_IP_PER_HOUR_ENV, DEFAULT_KEY_ISSUE_PER_IP_PER_HOUR)


def allowed_email_domains() -> frozenset[str]:
    """Empty set means "any domain" (the default: no allowlist configured)."""
    raw = os.environ.get(KEY_ALLOWED_EMAIL_DOMAINS_ENV, "")
    return frozenset(d.strip().lower() for d in raw.split(",") if d.strip())


def email_domain_allowed(email: str) -> bool:
    domains = allowed_email_domains()
    if not domains:
        return True
    _, _, domain = email.rpartition("@")
    return domain.lower() in domains


class IpIssueRateLimiter:
    """In-process token bucket: at most `max_per_hour` POST /keys successes
    per client IP per rolling hour. In-process only — a serverless deployment
    with multiple concurrent instances gets one independent budget PER
    instance, not one shared ceiling (documented in DEPLOY.md); still a real
    bound within a single instance's lifetime, and the per-email limit below
    provides a second, storage-backed layer that IS shared."""

    def __init__(self, max_per_hour: int | None = None) -> None:
        self.max_per_hour = max_per_hour if max_per_hour is not None else key_issue_per_ip_per_hour()
        self._hits: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def allow(self, ip: str) -> bool:
        """True (and records the hit) if `ip` is under budget; False if not —
        the caller must NOT count a False result as a used attempt."""
        now = time.monotonic()
        cutoff = now - 3600
        with self._lock:
            hits = [t for t in self._hits.get(ip, ()) if t > cutoff]
            if len(hits) >= self.max_per_hour:
                self._hits[ip] = hits
                return False
            hits.append(now)
            self._hits[ip] = hits
            return True


class KeyRateLimited(RuntimeError):
    """Raised when an email address requests more than one key per hour."""


@runtime_checkable
class KeyStore(Protocol):
    def issue(self, email: str) -> str: ...

    def is_valid(self, token: str) -> bool: ...

    def revoke(self, token: str) -> None: ...

    def owner_of(self, token: str) -> str | None:
        """The email an issued (non-revoked) key was issued to, or None when
        `token` is unknown or revoked (Phase F: request.state.owner)."""
        ...


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class SqliteKeyStore:
    """Persist only SHA-256 token hashes; plaintext leaves through ``issue`` once."""

    def __init__(self, db_path: Path | str = DEFAULT_DB) -> None:
        self.db_path = str(db_path)
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock:
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS api_keys "
                "(key_hash TEXT PRIMARY KEY, email TEXT NOT NULL, created_at TEXT NOT NULL, "
                "revoked INTEGER NOT NULL DEFAULT 0)"
            )
            self._conn.commit()

    def issue(self, email: str) -> str:
        normalized_email = email.strip().lower()
        now = datetime.now(UTC)
        cutoff = (now - KEY_RATE_LIMIT).isoformat()
        with self._lock:
            # revoked = 0: a key revoked by chorus.app's compensating revoke
            # (R23, delivery failed after mint) must not itself block a retry.
            recent = self._conn.execute(
                "SELECT 1 FROM api_keys WHERE email = ? AND created_at > ? AND revoked = 0 LIMIT 1",
                (normalized_email, cutoff),
            ).fetchone()
            if recent is not None:
                raise KeyRateLimited("one API key may be issued per email per hour")

            token = f"{KEY_PREFIX}{secrets.token_urlsafe(KEY_BYTES)}"
            self._conn.execute(
                "INSERT INTO api_keys (key_hash, email, created_at, revoked) VALUES (?, ?, ?, 0)",
                (_hash_token(token), normalized_email, now.isoformat()),
            )
            self._conn.commit()
        return token

    def is_valid(self, token: str) -> bool:
        key_hash = _hash_token(token)
        with self._lock:
            row = self._conn.execute(
                "SELECT revoked FROM api_keys WHERE key_hash = ?", (key_hash,)
            ).fetchone()
        return row is not None and row[0] == 0

    def revoke(self, token: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE api_keys SET revoked = 1 WHERE key_hash = ?", (_hash_token(token),)
            )
            self._conn.commit()

    def owner_of(self, token: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT email FROM api_keys WHERE key_hash = ? AND revoked = 0",
                (_hash_token(token),),
            ).fetchone()
        return row[0] if row else None

    def close(self) -> None:
        with self._lock:
            self._conn.close()
