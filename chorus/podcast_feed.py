"""The private podcast feed: every finished digest, delivered to the podcast
app the principal already uses.

A podcast app cannot send an `Authorization` header, so the feed and
everything it links to are authorized by a token in the URL instead:

- `feed_token(owner)` is an HMAC of the owner under `CHORUS_FEED_SECRET`
  (falling back to `CHORUS_API_TOKEN`). The URL never contains the owner's
  email, and rotating the secret revokes every feed at once.
- `owner_for_token` finds the owner a presented token belongs to by
  comparing against every owner with jobs, in constant time per owner.
- The episode audio, its Podcasting 2.0 chapters and its transcript are
  served under the same token (`/feed/{token}/{job_id}.mp3` and siblings),
  and only for a job that owner created. Any mismatch is a 404.

`render_feed` is pure: RSS 2.0 with the `itunes` and `podcast` namespaces,
`itunes:block` and `podcast:locked` set so directories never list a private
feed. Each item carries the enclosure (with its exact byte length and
duration), show notes listing the highlights with links that open each
source at the cited moment, `podcast:chapters` and `podcast:transcript`.

Locally there is no public URL, so `write_local_feed` writes
`~/.chorus/feed.xml` with `file://` enclosures, which desktop players can
open. A phone needs the hosted routes.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
from dataclasses import dataclass
from datetime import datetime
from email.utils import format_datetime
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

from fastapi import APIRouter, HTTPException, Request, Response
from mcp.server.fastmcp import Context, FastMCP  # type: ignore[import-not-found,import-untyped]

from chorus.artifacts import ArtifactStore
from chorus.chapters import chapters_json, mp3_duration_seconds, source_link
from chorus.jobs import MASTER_OWNER, FinishedJob, JobStore
from chorus.models import Job

log = logging.getLogger("chorus.podcast_feed")

FEED_SECRET_ENV = "CHORUS_FEED_SECRET"
API_TOKEN_FALLBACK_ENV = "CHORUS_API_TOKEN"
FEED_TOKEN_CHARS = 40
FEED_MAX_ITEMS = 50
MAX_NOTE_HIGHLIGHTS = 12
MAX_TITLE_SHOWS = 3
CHANNEL_TITLE = "Chorus"
CHANNEL_DESCRIPTION = "Your weekly Chorus digests: grounded highlights from the shows you follow."
MP3_TYPE = "audio/mpeg"
CHAPTERS_TYPE = "application/json+chapters"
TRANSCRIPT_TYPE = "text/plain"
LOCAL_FEED_NAME = "feed.xml"

ITUNES_NS = "http://www.itunes.com/dtds/podcast-1.0.dtd"
PODCAST_NS = "https://podcastindex.org/namespace/1.0"
ET.register_namespace("itunes", ITUNES_NS)
ET.register_namespace("podcast", PODCAST_NS)

_random_secret: str | None = None


# --- Tokens ----------------------------------------------------------------


def _feed_secret() -> bytes:
    secret = os.environ.get(FEED_SECRET_ENV) or os.environ.get(API_TOKEN_FALLBACK_ENV)
    if secret:
        return secret.encode("utf-8")
    global _random_secret
    if _random_secret is None:
        _random_secret = secrets.token_urlsafe(32)
        log.warning(
            "podcast feed: neither %s nor %s is set; using a per-process random secret "
            "(feed URLs stop working after a restart)",
            FEED_SECRET_ENV,
            API_TOKEN_FALLBACK_ENV,
        )
    return _random_secret.encode("utf-8")


def feed_token(owner: str) -> str:
    """The owner's feed token: an HMAC, so it reveals nothing about the owner."""
    digest = hmac.new(_feed_secret(), f"chorus-feed:{owner}".encode(), hashlib.sha256)
    return digest.hexdigest()[:FEED_TOKEN_CHARS]


def owner_for_token(token: str, owners: list[str]) -> str | None:
    """The owner whose feed token is `token`, or None."""
    match: str | None = None
    for owner in owners:
        if hmac.compare_digest(feed_token(owner), token):
            match = owner
    return match


# --- Episodes ----------------------------------------------------------------


@dataclass(frozen=True)
class FeedEpisode:
    job_id: str
    title: str
    published: datetime
    notes: str
    audio_url: str
    audio_bytes: int
    duration_seconds: float
    chapters_url: str | None = None
    transcript_url: str | None = None


def _clock(seconds: float) -> str:
    total = int(seconds)
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def episode_title(job: Job, published: datetime) -> str:
    """`Chorus · 04 Oct 2026 · Show A, Show B`: the date and the shows heard."""
    shows: list[str] = []
    for episode in job.digest.episodes if job.digest else []:
        if episode.highlights and episode.show and episode.show not in shows:
            shows.append(episode.show)
    title = f"{CHANNEL_TITLE} · {published.strftime('%d %b %Y')}"
    if shows:
        more = len(shows) - MAX_TITLE_SHOWS
        title += " · " + ", ".join(shows[:MAX_TITLE_SHOWS]) + (f" and {more} more" if more > 0 else "")
    return title


def episode_notes(job: Job) -> str:
    """Plain-text show notes: each highlight, where it came from, why it
    surfaced, and a link that opens the source at that moment."""
    if job.digest is None:
        return ""
    urls = {e.episode_id: e.url for e in job.digest.episodes}
    lines: list[str] = []
    for highlight in job.digest.highlights[:MAX_NOTE_HIGHLIGHTS]:
        source = highlight.show or highlight.episode_title or highlight.episode_id
        if highlight.show and highlight.episode_title:
            source = f"{highlight.show}: {highlight.episode_title}"
        lines.append(f"• {source} ({_clock(highlight.segment_timestamp)}): {highlight.why_surface}")
        lines.append(f'  "{highlight.quote}"')
        link = source_link(urls.get(highlight.episode_id), highlight.segment_timestamp)
        if link:
            lines.append(f"  {link}")
    return "\n".join(lines)


def _artifact_name(job: Job) -> str | None:
    if not job.audio_url:
        return None
    return job.audio_url.rsplit("/", 1)[-1]


def build_episodes(
    finished: list[FinishedJob], base_url: str, token: str, artifacts: ArtifactStore
) -> list[FeedEpisode]:
    """Feed items for an owner's finished jobs, with absolute token URLs.
    A job whose audio is missing from the store is left out."""
    root = f"{base_url.rstrip('/')}/feed/{token}"
    out: list[FeedEpisode] = []
    for item in finished:
        name = _artifact_name(item.job)
        stored = artifacts.get(name) if name else None
        if stored is None:
            continue
        data, _ = stored
        job_root = f"{root}/{item.job.job_id}"
        out.append(
            FeedEpisode(
                job_id=item.job.job_id,
                title=episode_title(item.job, item.created_at),
                published=item.created_at,
                notes=episode_notes(item.job),
                audio_url=f"{job_root}.mp3",
                audio_bytes=len(data),
                duration_seconds=mp3_duration_seconds(data),
                chapters_url=f"{job_root}/chapters.json" if item.job.chapters else None,
                transcript_url=f"{job_root}/transcript.txt" if item.job.script else None,
            )
        )
    return out


# --- RSS ---------------------------------------------------------------------


def _sub(parent: ET.Element, tag: str, text: str | None = None, **attrs: str) -> ET.Element:
    element = ET.SubElement(parent, tag, attrs)
    if text is not None:
        element.text = text
    return element


def render_feed(episodes: list[FeedEpisode], feed_url: str, owner_label: str = "") -> str:
    """RSS 2.0 for a private feed, newest episode first."""
    rss = ET.Element("rss", {"version": "2.0"})
    channel = _sub(rss, "channel")
    title = f"{CHANNEL_TITLE} for {owner_label}" if owner_label else CHANNEL_TITLE
    _sub(channel, "title", title)
    _sub(channel, "link", feed_url)
    _sub(channel, "description", CHANNEL_DESCRIPTION)
    _sub(channel, "language", "en")
    _sub(channel, f"{{{ITUNES_NS}}}author", CHANNEL_TITLE)
    _sub(channel, f"{{{ITUNES_NS}}}block", "Yes")
    _sub(channel, f"{{{ITUNES_NS}}}explicit", "false")
    _sub(channel, f"{{{PODCAST_NS}}}locked", "yes")
    ordered = sorted(episodes, key=lambda e: e.published, reverse=True)[:FEED_MAX_ITEMS]
    for episode in ordered:
        item = _sub(channel, "item")
        _sub(item, "title", episode.title)
        _sub(item, "guid", episode.job_id, isPermaLink="false")
        _sub(item, "pubDate", format_datetime(episode.published))
        _sub(item, "description", episode.notes)
        _sub(
            item, "enclosure",
            url=episode.audio_url, length=str(episode.audio_bytes), type=MP3_TYPE,
        )
        _sub(item, f"{{{ITUNES_NS}}}duration", str(int(round(episode.duration_seconds))))
        if episode.chapters_url:
            _sub(item, f"{{{PODCAST_NS}}}chapters", url=episode.chapters_url, type=CHAPTERS_TYPE)
        if episode.transcript_url:
            _sub(
                item, f"{{{PODCAST_NS}}}transcript",
                url=episode.transcript_url, type=TRANSCRIPT_TYPE,
            )
    return ET.tostring(rss, encoding="unicode", xml_declaration=True)


# --- Byte ranges ---------------------------------------------------------------


def byte_range(header: str | None, size: int) -> tuple[int, int] | None:
    """The inclusive (start, end) a single-range `Range: bytes=...` header
    asks for, or None to serve the whole body. An unsatisfiable range
    raises ValueError (the caller answers 416)."""
    if not header or not header.startswith("bytes=") or "," in header:
        return None
    first, _, last = header[len("bytes="):].strip().partition("-")
    if not first and not last:
        return None
    if not first:  # suffix: the last N bytes
        length = int(last)
        if length <= 0:
            raise ValueError("empty suffix range")
        return max(0, size - length), size - 1
    start = int(first)
    end = min(int(last), size - 1) if last else size - 1
    if start >= size or start > end:
        raise ValueError(f"range {header} outside {size} bytes")
    return start, end


def _audio_response(data: bytes, range_header: str | None) -> Response:
    try:
        span = byte_range(range_header, len(data))
    except ValueError:
        return Response(status_code=416, headers={"Content-Range": f"bytes */{len(data)}"})
    if span is None:
        return Response(content=data, media_type=MP3_TYPE, headers={"Accept-Ranges": "bytes"})
    start, end = span
    return Response(
        content=data[start : end + 1],
        status_code=206,
        media_type=MP3_TYPE,
        headers={"Accept-Ranges": "bytes", "Content-Range": f"bytes {start}-{end}/{len(data)}"},
    )


# --- HTTP ------------------------------------------------------------------------

FEED_PUBLIC_PREFIX = "/feed/"
FEED_INSTRUCTIONS = (
    "Add this URL to a podcast app that accepts private feeds (Overcast: Add URL; "
    "Pocket Casts: Discover > paste the URL; Apple Podcasts on macOS: File > Follow a Show "
    "by URL). Treat it like a password: anyone with it can hear your digests."
)


def feed_url_for(owner: str, base_url: str) -> str:
    return f"{base_url.rstrip('/')}/feed/{feed_token(owner)}.xml"


def build_feed_router(store: JobStore, artifacts: ArtifactStore) -> APIRouter:
    from chorus.subscriptions_api import resolve_base_url

    router = APIRouter()

    def _owner(token: str) -> str:
        owner = owner_for_token(token, store.owners())
        if owner is None:
            raise HTTPException(status_code=404, detail="unknown feed")
        return owner

    def _job(token: str, job_id: str) -> Job:
        owner = _owner(token)
        job = store.get(job_id)
        if job is None or job.owner != owner:
            raise HTTPException(status_code=404, detail="unknown episode")
        return job

    @router.get("/feed")
    def my_feed(request: Request) -> dict[str, Any]:
        """The caller's private feed URL (authenticated like every other route)."""
        owner = getattr(request.state, "owner", MASTER_OWNER)
        return {
            "feed_url": feed_url_for(owner, resolve_base_url(request)),
            "episodes": len(store.list_finished(owner, FEED_MAX_ITEMS)),
            "instructions": FEED_INSTRUCTIONS,
        }

    @router.get("/feed/{token}.xml")
    def feed(token: str, request: Request) -> Response:
        owner = _owner(token)
        base_url = resolve_base_url(request)
        episodes = build_episodes(
            store.list_finished(owner, FEED_MAX_ITEMS), base_url, token, artifacts
        )
        body = render_feed(episodes, feed_url_for(owner, base_url))
        return Response(content=body, media_type="application/rss+xml")

    @router.get("/feed/{token}/{job_id}.mp3")
    def audio(token: str, job_id: str, request: Request) -> Response:
        job = _job(token, job_id)
        name = _artifact_name(job)
        stored = artifacts.get(name) if name and name.endswith(".mp3") else None
        if stored is None:
            raise HTTPException(status_code=404, detail="unknown episode")
        return _audio_response(stored[0], request.headers.get("range"))

    @router.get("/feed/{token}/{job_id}/chapters.json")
    def chapters(token: str, job_id: str) -> Response:
        job = _job(token, job_id)
        return Response(content=chapters_json(job.chapters), media_type=CHAPTERS_TYPE)

    @router.get("/feed/{token}/{job_id}/transcript.txt")
    def transcript(token: str, job_id: str) -> Response:
        job = _job(token, job_id)
        if job.script is None:
            raise HTTPException(status_code=404, detail="no transcript")
        return Response(content=job.script.monologue, media_type=TRANSCRIPT_TYPE)

    return router


# --- Local feed --------------------------------------------------------------------


def write_local_feed(store: JobStore, artifacts_dir: Path, out: Path) -> tuple[Path, int]:
    """`out` as a feed of the local owner's episodes with file:// enclosures."""
    episodes: list[FeedEpisode] = []
    for item in store.list_finished(MASTER_OWNER, FEED_MAX_ITEMS):
        name = _artifact_name(item.job)
        file = artifacts_dir / name if name else None
        if file is None or not file.is_file():
            continue
        data = file.read_bytes()
        episodes.append(
            FeedEpisode(
                job_id=item.job.job_id,
                title=episode_title(item.job, item.created_at),
                published=item.created_at,
                notes=episode_notes(item.job),
                audio_url=file.resolve().as_uri(),
                audio_bytes=len(data),
                duration_seconds=mp3_duration_seconds(data),
            )
        )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_feed(episodes, out.resolve().as_uri()), encoding="utf-8")
    return out, len(episodes)


# --- MCP ---------------------------------------------------------------------------


def register_feed_tools(server: FastMCP, store: JobStore, local: bool = False) -> None:
    """`get_podcast_feed`: the hosted feed URL, or a local feed file."""

    def get_podcast_feed(ctx: Context | None = None) -> dict[str, Any]:
        """Your private podcast feed of finished digests, with chapters and
        links back to each source moment. Hosted: a URL to add to a podcast
        app once. Local: writes ~/.chorus/feed.xml for a desktop player."""
        if local:
            from chorus import paths

            out, count = write_local_feed(
                store, paths.artifacts_dir(), paths.home() / LOCAL_FEED_NAME
            )
            return {
                "feed_file": str(out),
                "episodes": count,
                "instructions": (
                    "Open this file in a desktop podcast player. A phone needs the hosted "
                    "service's feed URL, because a local file is not reachable from it."
                ),
            }
        from chorus.mcp_server import _owner_from_context
        from chorus.subscriptions_api import resolve_base_url

        request = None
        if ctx is not None:
            try:
                request = ctx.request_context.request
            except ValueError:
                request = None
        owner = _owner_from_context(ctx)
        return {
            "feed_url": feed_url_for(owner, resolve_base_url(request)),
            "episodes": len(store.list_finished(owner, FEED_MAX_ITEMS)),
            "instructions": FEED_INSTRUCTIONS,
        }

    server.add_tool(get_podcast_feed)
