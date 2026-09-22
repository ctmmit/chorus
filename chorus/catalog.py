"""Layer-2 catalog: list selectable shows and resolve a selection to episodes.

v1 is fixture-backed (the pool in fixtures/episodes.json IS the catalog) — no
live discovery; that's Layer 3 (charts, post-July-11). The same resolve() seam
takes a real catalog source later.
"""
from __future__ import annotations

import json
from pathlib import Path

from chorus.models import EpisodeInput

EPISODES_JSON = Path(__file__).resolve().parent.parent / "fixtures" / "episodes.json"


def _load() -> list[dict]:
    data = json.loads(EPISODES_JSON.read_text(encoding="utf-8"))
    episodes: list[dict] = list(data.get("clean", []))
    ungrounded = data.get("ungrounded")
    if isinstance(ungrounded, dict) and ungrounded.get("video_id"):
        episodes.append(ungrounded)
    return [e for e in episodes if e.get("video_id")]


def list_shows() -> list[dict]:
    by_show: dict[str, list[dict]] = {}
    for e in _load():
        by_show.setdefault(e.get("show", "Unknown"), []).append(
            {"video_id": e["video_id"], "title": e.get("title")}
        )
    return [{"show": show, "episodes": eps} for show, eps in by_show.items()]


def enrich(episodes: list[EpisodeInput]) -> list[EpisodeInput]:
    """Fill a missing `show`/`title` from the catalog when the episode's id is
    in it, so a caller that submits bare video ids still gets titled digests.
    Episodes outside the catalog are returned unchanged."""
    by_id = {e["video_id"]: e for e in _load()}
    out: list[EpisodeInput] = []
    for ep in episodes:
        try:
            match = by_id.get(ep.resolved_id())
        except ValueError:
            match = None
        if match is None or (ep.show and ep.title):
            out.append(ep)
            continue
        out.append(
            ep.model_copy(
                update={"show": ep.show or match.get("show"), "title": ep.title or match.get("title")}
            )
        )
    return out


def resolve(
    shows: list[str] | None = None, video_ids: list[str] | None = None
) -> list[EpisodeInput]:
    episodes = _load()
    want_shows = {s.lower() for s in shows} if shows else set()
    want_ids = set(video_ids) if video_ids else set()

    out: list[EpisodeInput] = []
    seen: set[str] = set()
    for e in episodes:
        vid = e["video_id"]
        if vid in seen:
            continue
        if vid in want_ids or e.get("show", "").lower() in want_shows:
            seen.add(vid)
            out.append(EpisodeInput(video_id=vid, show=e.get("show"), title=e.get("title")))
    return out
