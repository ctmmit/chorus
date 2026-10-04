"""Personas as sources: agents as each other's audience (IDEA_DOC.md §14).

A persona (chorus/personas.py) is a soul registered as a discoverable
agent. This module lets it publish, and lets other principals listen:

- **Publishing.** The persona's owner publishes one of their finished
  digests to it (`POST /personas/{id}/publish`, MCP `publish_to_persona`).
  A `Publication` snapshots what that digest surfaced: each episode's
  original identity and its highlights. It never exposes the owner's soul,
  context or other jobs.
- **Listening.** A subscription source of kind `persona` (`PersonaSource`)
  takes the original episodes a persona surfaced since the last run and
  curates them through the subscriber's own soul. The persona works as a
  discovery filter; the subscriber's highlights cite the primary source and
  timestamp, never the intermediary, so grounding holds across the hop.
- **Endorsements.** When a subscriber up-votes a highlight from an episode
  that arrived through a persona (chorus/feedback.py), the persona gains an
  endorsement, once per subscriber and highlight. That is the first quality
  signal that comes from listeners rather than clicks. `/network` shows the
  counts, and a persona's A2A card advertises its feed.
- **Feed.** `GET /personas/{id}/feed.xml` is a public podcast feed of the
  persona's published digests (chorus/podcast_feed.py renders it); its audio
  is served only for jobs that were published.
"""
from __future__ import annotations

import sqlite3
import threading
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from fastapi import APIRouter, HTTPException, Request, Response
from mcp.server.fastmcp import Context, FastMCP  # type: ignore[import-not-found,import-untyped]
from pydantic import BaseModel, Field

from chorus.artifacts import ArtifactStore
from chorus.chapters import mp3_duration_seconds
from chorus.jobs import DEFAULT_DB, MASTER_OWNER, JobStore
from chorus.models import EpisodeInput, Highlight, Job, JobStatus
from chorus.personas import Persona, PersonaRegistry

PUBLICATIONS_TABLE = "persona_publications"
ENDORSEMENTS_TABLE = "persona_endorsements"
MAX_PUBLICATIONS_LISTED = 50
RSS_ID_PREFIX = "rss-"


class PublishedEpisode(BaseModel):
    episode: EpisodeInput = Field(description="The original episode, fetchable again.")
    title: str | None = None
    show: str | None = None
    highlights: list[Highlight] = Field(default_factory=list)


class Publication(BaseModel):
    publication_id: str
    persona_id: str
    job_id: str
    published_at: datetime
    episodes: list[PublishedEpisode]
    audio_name: str | None = Field(
        default=None, description="The job's MP3 artifact, if it has one."
    )


class PublishError(ValueError):
    """A publish request that cannot be honored; the message says why."""


# --- Pure logic -----------------------------------------------------------------


def original_episode(digest_episode: Any) -> EpisodeInput | None:
    """The episode as first requested; for digests that predate
    `source_episode`, a YouTube id or the audio URL stands in."""
    if digest_episode.source_episode is not None:
        return digest_episode.source_episode  # type: ignore[no-any-return]
    if not digest_episode.episode_id.startswith(RSS_ID_PREFIX):
        return EpisodeInput(video_id=digest_episode.episode_id)
    if digest_episode.url:
        return EpisodeInput(audio_url=digest_episode.url)
    return None


def publication_from(job: Job, persona_id: str, now: datetime) -> Publication:
    """Snapshot what a finished digest surfaced, episode by episode."""
    if job.status is not JobStatus.done or job.digest is None:
        raise PublishError(f"job {job.job_id} has no finished digest to publish")
    episodes: list[PublishedEpisode] = []
    for ep in job.digest.episodes:
        source = original_episode(ep)
        if ep.highlights and source is not None:
            episodes.append(PublishedEpisode(
                episode=source, title=ep.episode_title, show=ep.show, highlights=ep.highlights,
            ))
    if not episodes:
        raise PublishError(f"job {job.job_id} surfaced nothing to publish")
    audio = job.audio_url.rsplit("/", 1)[-1] if job.audio_url and job.audio_url.endswith(".mp3") else None
    return Publication(
        publication_id=uuid.uuid4().hex, persona_id=persona_id, job_id=job.job_id,
        published_at=now, episodes=episodes, audio_name=audio,
    )


def persona_feed_episodes(
    persona: Persona, publications: list[Publication], since: datetime, limit: int
) -> list[Any]:
    """FeedEpisodes for a PersonaSource: the original episodes of the
    persona's publications since `since`, newest first, each once."""
    from chorus.feeds import FeedEpisode

    out: list[FeedEpisode] = []
    seen: set[str] = set()
    for publication in sorted(publications, key=lambda p: p.published_at, reverse=True):
        if publication.published_at < since:
            continue
        for published in publication.episodes:
            episode_id = published.episode.resolved_id()
            if episode_id in seen:
                continue
            seen.add(episode_id)
            out.append(FeedEpisode(
                source_title=f"{persona.name} (persona)",
                title=published.title or episode_id,
                published_at=publication.published_at,
                episode=published.episode,
                via_persona=persona.persona_id,
            ))
    return out[:limit]


# --- Storage ----------------------------------------------------------------------


@runtime_checkable
class PublicationStore(Protocol):
    def publish(self, publication: Publication) -> None:
        """Upsert by (persona_id, job_id): republishing refreshes it."""
        ...

    def list(self, persona_id: str, limit: int = MAX_PUBLICATIONS_LISTED) -> list[Publication]:
        """Newest first."""
        ...

    def endorse(self, persona_id: str, endorser: str, highlight_id: str) -> bool:
        """Record one endorsement; False if this endorser already gave it."""
        ...

    def endorsement_counts(self) -> dict[str, int]: ...

    def close(self) -> None: ...


class SqlitePublicationStore:
    def __init__(self, db_path: Path | str = DEFAULT_DB) -> None:
        self.db_path = str(db_path)
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock:
            self._conn.execute(
                f"CREATE TABLE IF NOT EXISTS {PUBLICATIONS_TABLE} (persona_id TEXT NOT NULL, "
                "job_id TEXT NOT NULL, published_at TEXT NOT NULL, payload TEXT NOT NULL, "
                "PRIMARY KEY (persona_id, job_id))"
            )
            self._conn.execute(
                f"CREATE TABLE IF NOT EXISTS {ENDORSEMENTS_TABLE} (persona_id TEXT NOT NULL, "
                "endorser TEXT NOT NULL, highlight_id TEXT NOT NULL, "
                "PRIMARY KEY (persona_id, endorser, highlight_id))"
            )
            self._conn.commit()

    def publish(self, publication: Publication) -> None:
        with self._lock:
            self._conn.execute(
                f"INSERT OR REPLACE INTO {PUBLICATIONS_TABLE} "
                "(persona_id, job_id, published_at, payload) VALUES (?, ?, ?, ?)",
                (publication.persona_id, publication.job_id,
                 publication.published_at.isoformat(), publication.model_dump_json()),
            )
            self._conn.commit()

    def list(self, persona_id: str, limit: int = MAX_PUBLICATIONS_LISTED) -> list[Publication]:
        with self._lock:
            rows = self._conn.execute(
                f"SELECT payload FROM {PUBLICATIONS_TABLE} WHERE persona_id = ? "
                "ORDER BY published_at DESC LIMIT ?",
                (persona_id, limit),
            ).fetchall()
        return [Publication.model_validate_json(r[0]) for r in rows]

    def endorse(self, persona_id: str, endorser: str, highlight_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                f"INSERT OR IGNORE INTO {ENDORSEMENTS_TABLE} (persona_id, endorser, highlight_id) "
                "VALUES (?, ?, ?)",
                (persona_id, endorser, highlight_id),
            )
            self._conn.commit()
        return cur.rowcount > 0

    def endorsement_counts(self) -> dict[str, int]:
        with self._lock:
            rows = self._conn.execute(
                f"SELECT persona_id, COUNT(*) FROM {ENDORSEMENTS_TABLE} GROUP BY persona_id"
            ).fetchall()
        return {str(r[0]): int(r[1]) for r in rows}

    def close(self) -> None:
        with self._lock:
            self._conn.close()


# --- Endorsements from ratings ------------------------------------------------------


def endorsement_hook(store: PublicationStore) -> Callable[[Any, Job], None]:
    """For chorus.feedback.FeedbackService: an up-vote on a highlight whose
    episode came through a persona endorses that persona."""

    def on_rating(rating: Any, job: Job) -> None:
        if rating.vote != "up" or job.digest is None:
            return
        for ep in job.digest.episodes:
            if ep.via_persona and any(h.highlight_id == rating.highlight_id for h in ep.highlights):
                store.endorse(ep.via_persona, rating.owner, rating.highlight_id)

    return on_rating


# --- The service -----------------------------------------------------------------------


class PublishService:
    def __init__(
        self,
        publications: PublicationStore,
        personas: PersonaRegistry,
        jobs: JobStore,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.publications = publications
        self.personas = personas
        self.jobs = jobs
        self.clock = clock

    def publish(self, owner: str, persona_id: str, job_id: str) -> Publication:
        persona = self.personas.get(persona_id)
        if persona is None or (owner != MASTER_OWNER and persona.owner != owner):
            raise PublishError(f"unknown persona {persona_id}")
        job = self.jobs.get(job_id)
        if job is None or (owner != MASTER_OWNER and job.owner != owner):
            raise PublishError(f"unknown job {job_id}")
        publication = publication_from(job, persona_id, self.clock())
        self.publications.publish(publication)
        return publication


# --- HTTP ---------------------------------------------------------------------------------


class PublishRequest(BaseModel):
    job_id: str = Field(min_length=1, max_length=128)


def build_publications_router(service: PublishService, artifacts: ArtifactStore) -> APIRouter:
    from chorus.podcast_feed import FeedEpisode, _audio_response, episode_notes, render_feed
    from chorus.subscriptions_api import resolve_base_url

    router = APIRouter()

    def _public_persona(persona_id: str) -> Persona:
        persona = service.personas.get(persona_id)
        if persona is None or not persona.public:
            raise HTTPException(status_code=404, detail="unknown persona_id")
        return persona

    @router.post("/personas/{persona_id}/publish")
    def publish(persona_id: str, body: PublishRequest, request: Request) -> dict[str, Any]:
        owner = getattr(request.state, "owner", MASTER_OWNER)
        try:
            publication = service.publish(owner, persona_id, body.job_id)
        except PublishError as err:
            code = 404 if str(err).startswith("unknown") else 422
            raise HTTPException(status_code=code, detail=str(err)) from err
        return publication.model_dump(mode="json")

    @router.get("/personas/{persona_id}/published")
    def published(persona_id: str) -> list[dict[str, Any]]:
        """Public: what this persona published, newest first."""
        _public_persona(persona_id)
        return [p.model_dump(mode="json") for p in service.publications.list(persona_id)]

    @router.get("/personas/{persona_id}/feed.xml")
    def feed(persona_id: str, request: Request) -> Response:
        persona = _public_persona(persona_id)
        base = resolve_base_url(request).rstrip("/")
        items: list[FeedEpisode] = []
        for publication in service.publications.list(persona_id):
            stored = artifacts.get(publication.audio_name) if publication.audio_name else None
            if stored is None:
                continue
            job = service.jobs.get(publication.job_id)
            if job is None:
                continue
            data, _ = stored
            items.append(FeedEpisode(
                job_id=publication.job_id,
                title=f"{persona.name} · {publication.published_at.strftime('%d %b %Y')}",
                published=publication.published_at,
                notes=episode_notes(job),
                audio_url=f"{base}/personas/{persona_id}/episodes/{publication.job_id}.mp3",
                audio_bytes=len(data),
                duration_seconds=mp3_duration_seconds(data),
            ))
        body = render_feed(items, f"{base}/personas/{persona_id}/feed.xml", persona.name)
        return Response(content=body, media_type="application/rss+xml")

    @router.get("/personas/{persona_id}/episodes/{job_id}.mp3")
    def audio(persona_id: str, job_id: str, request: Request) -> Response:
        _public_persona(persona_id)
        match = next((p for p in service.publications.list(persona_id) if p.job_id == job_id), None)
        stored = artifacts.get(match.audio_name) if match and match.audio_name else None
        if stored is None:
            raise HTTPException(status_code=404, detail="not a published episode")
        return _audio_response(stored[0], request.headers.get("range"))

    return router


# --- MCP ----------------------------------------------------------------------------------


def register_publication_tools(server: FastMCP, get_service: Callable[[], PublishService]) -> None:
    """publish_to_persona: share one finished digest through a persona."""
    from chorus.mcp_server import _owner_from_context

    def publish_to_persona(persona_id: str, job_id: str, ctx: Context | None = None) -> dict[str, Any]:
        """Publish a finished digest to a persona you registered. Other
        principals subscribed to that persona (a source of kind "persona")
        get the episodes it surfaced, curated through their own soul. Only
        the episodes and highlights are shared, never your soul or context."""
        try:
            return get_service().publish(_owner_from_context(ctx), persona_id, job_id).model_dump(
                mode="json"
            )
        except PublishError as err:
            return {"error": str(err)}

    server.add_tool(publish_to_persona)
