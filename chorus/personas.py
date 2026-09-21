"""Persona registry (docs/DEVELOPMENT_PLAN.md §8 row H, "Discovery").

A Persona is a Chorus soul registered as a discoverable, publishable agent
(IDEA_DOC.md §14: "personas as agents, agents as each other's audience").
`PersonaRegistry` is the Protocol every backend implements, mirroring
`chorus.jobs.JobStore`'s shape exactly (create/get/list/delete over a
JSON-payload column) so a future `chorus.stores.postgres.PostgresPersonaRegistry`
is a drop-in the same way `PostgresJobStore` is — see that module's docstring.
"""
from __future__ import annotations

import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from chorus.curation import soul_version as compute_soul_version
from chorus.jobs import DEFAULT_DB
from chorus.models import MAX_SOUL_CHARS

# Bounds mirror chorus.models' request bounds (same rationale: cap payload
# size from an anonymous or low-trust caller before it reaches storage).
MAX_PERSONA_ID_CHARS = 128
MAX_NAME_CHARS = 200
MAX_DESCRIPTION_CHARS = 2_000
MAX_SHOWS = 25
MAX_SHOW_NAME_CHARS = 200

PERSONA_TABLE = "personas"

Cadence = Literal["weekly", "daily", "adhoc"]


class Persona(BaseModel):
    """One registered persona: a soul with a name, a publication cadence, and
    the shows it listens to. `soul_version` is the same content-hash
    provenance Digest.soul_version uses (chorus.curation.soul_version), so a
    persona's published facts can be checked against the soul that actually
    produced a given digest."""

    persona_id: str = Field(min_length=1, max_length=MAX_PERSONA_ID_CHARS)
    name: str = Field(min_length=1, max_length=MAX_NAME_CHARS)
    description: str = Field(default="", max_length=MAX_DESCRIPTION_CHARS)
    # Bounded like DigestRequest.soul (chorus/models.py): the same lens text,
    # registered once instead of re-sent on every /digest call.
    soul: str = Field(min_length=1, max_length=MAX_SOUL_CHARS)
    shows: list[str] = Field(default_factory=list, max_length=MAX_SHOWS)
    cadence: Cadence = "weekly"
    created_at: str
    soul_version: str
    public: bool = True


@runtime_checkable
class PersonaRegistry(Protocol):
    def create(self, persona: Persona) -> Persona: ...

    def get(self, persona_id: str) -> Persona | None: ...

    def list(self, *, public_only: bool = False) -> list[Persona]: ...

    def delete(self, persona_id: str) -> bool: ...


def build_persona(
    *,
    name: str,
    description: str = "",
    soul: str,
    shows: list[str] | None = None,
    cadence: Cadence = "weekly",
    public: bool = True,
    persona_id: str | None = None,
) -> Persona:
    """Construct a new Persona with a fresh id, provenance timestamp, and
    soul_version — the one place those three derived fields get computed so
    every registry backend (and every test) builds them the same way."""
    return Persona(
        persona_id=persona_id or uuid.uuid4().hex,
        name=name,
        description=description,
        soul=soul,
        shows=shows or [],
        cadence=cadence,
        created_at=datetime.now(timezone.utc).isoformat(),
        soul_version=compute_soul_version(soul),
        public=public,
    )


class SqlitePersonaRegistry:
    """`personas(persona_id TEXT PRIMARY KEY, public INTEGER NOT NULL,
    payload TEXT NOT NULL)` in the shared chorus.db by default — same
    3-column pattern as `chorus.jobs.SqliteJobStore` (a query column plus the
    whole model as JSON), Postgres-ready without a schema rewrite."""

    def __init__(self, db_path: Path | str = DEFAULT_DB) -> None:
        self.db_path = str(db_path)
        # check_same_thread=False to match JobStore/TranscriptCache: this
        # registry is read from request-handling threads, not just the
        # pipeline's background thread.
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock:
            self._conn.execute(
                f"CREATE TABLE IF NOT EXISTS {PERSONA_TABLE} "
                "(persona_id TEXT PRIMARY KEY, public INTEGER NOT NULL, payload TEXT NOT NULL)"
            )
            self._conn.commit()

    def create(self, persona: Persona) -> Persona:
        with self._lock:
            self._conn.execute(
                f"INSERT OR REPLACE INTO {PERSONA_TABLE} (persona_id, public, payload) "
                "VALUES (?, ?, ?)",
                (persona.persona_id, int(persona.public), persona.model_dump_json()),
            )
            self._conn.commit()
        return persona

    def get(self, persona_id: str) -> Persona | None:
        with self._lock:
            row = self._conn.execute(
                f"SELECT payload FROM {PERSONA_TABLE} WHERE persona_id = ?", (persona_id,)
            ).fetchone()
        return Persona.model_validate_json(row[0]) if row else None

    def list(self, *, public_only: bool = False) -> list[Persona]:
        query = f"SELECT payload FROM {PERSONA_TABLE}"
        if public_only:
            query += " WHERE public = 1"
        with self._lock:
            rows = self._conn.execute(query).fetchall()
        return [Persona.model_validate_json(r[0]) for r in rows]

    def delete(self, persona_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                f"DELETE FROM {PERSONA_TABLE} WHERE persona_id = ?", (persona_id,)
            )
            self._conn.commit()
        return cur.rowcount > 0

    def close(self) -> None:
        with self._lock:
            self._conn.close()
