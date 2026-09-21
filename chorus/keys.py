"""API-key issuance and validation backed by SQLite."""
from __future__ import annotations

import hashlib
import secrets
import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol, runtime_checkable

from chorus.jobs import DEFAULT_DB

KEY_PREFIX = "chorus_"
KEY_BYTES = 32
KEY_RATE_LIMIT = timedelta(hours=1)


class KeyRateLimited(RuntimeError):
    """Raised when an email address requests more than one key per hour."""


@runtime_checkable
class KeyStore(Protocol):
    def issue(self, email: str) -> str: ...

    def is_valid(self, token: str) -> bool: ...

    def revoke(self, token: str) -> None: ...


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
            recent = self._conn.execute(
                "SELECT 1 FROM api_keys WHERE email = ? AND created_at > ? LIMIT 1",
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

    def close(self) -> None:
        with self._lock:
            self._conn.close()
