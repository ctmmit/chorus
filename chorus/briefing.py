"""Source briefs: what a listener needs to know before any commentary.

A digest highlight is a moment worth discussing, but on its own it says
nothing about who is talking, what the piece is, or what it argues. Before
the outline and script stages run, each featured source gets a `SourceBrief`
built from its title, show, publication date, show notes, the opening of the
transcript (where hosts name themselves and their guest) and its surfaced
excerpts.

This module holds the pure parts: choosing which sources the writer may draw
on, building the brief prompt, validating a model's brief against the material it
was given, and the deterministic mock brief. The model call itself lives in
chorus/script.py next to the other script-stage calls.
"""
from __future__ import annotations

import re
from typing import Any

from pydantic import ValidationError

from chorus.models import BriefPoint, Digest, EpisodeDigest, Person, SourceBrief

# Every source with highlights is a candidate the writer may use, and the
# writer decides which ones get airtime (chorus/outline.py). This caps how many
# are briefed, one model call each; it is a cost guard, not an editorial limit.
MAX_CANDIDATE_SOURCES = 12
# A source's rank is the sum of its best few highlight scores, so one lucky
# window doesn't outrank an episode that is on-lens throughout.
RANK_TOP_HIGHLIGHTS = 3
# A name token shorter than this ("J.", "Jr") is not checked against the source.
MIN_NAME_TOKEN_CHARS = 2

_NAME_TOKEN_RE = re.compile(r"[\w'’-]+")
_SENTENCE_END_RE = re.compile(r"(?<=[.!?])\s+")


def source_rank(episode: EpisodeDigest) -> float:
    scores = sorted((h.relevance_score for h in episode.highlights), reverse=True)
    return sum(scores[:RANK_TOP_HIGHLIGHTS])


def select_sources(
    digest: Digest, max_sources: int = MAX_CANDIDATE_SOURCES
) -> tuple[list[EpisodeDigest], list[EpisodeDigest]]:
    """(candidates, overflow): episodes with highlights, best first, split at
    `max_sources`. Refused episodes are in neither list. Ties keep digest
    order, so the result is deterministic."""
    if max_sources < 1:
        raise ValueError(f"max_sources must be >= 1, got {max_sources}")
    candidates = [ep for ep in digest.episodes if ep.highlights and not ep.refused]
    ranked = sorted(candidates, key=source_rank, reverse=True)
    return ranked[:max_sources], ranked[max_sources:]


def format_timestamp(seconds: float) -> str:
    total = int(seconds)
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def source_material(episode: EpisodeDigest) -> str:
    """Everything known about one source, as prompt text. Shared by the brief
    and script prompts so both stages read the same facts."""
    published = episode.published_at.strftime("%d %b %Y") if episode.published_at else "unknown"
    lines = [
        f"episode_id: {episode.episode_id}",
        f"Show: {episode.show or 'unknown'}",
        f"Episode title: {episode.episode_title or 'unknown'}",
        f"Published: {published}",
    ]
    if episode.description:
        lines.append(f"Show notes: {episode.description}")
    if episode.intro_excerpt:
        lines.append(f"Opening of the transcript:\n{episode.intro_excerpt}")
    lines.append("Surfaced moments (timestamp in seconds, why it matters to this listener, excerpt):")
    for h in sorted(episode.highlights, key=lambda h: h.segment_timestamp):
        excerpt = h.excerpt or h.quote
        lines.append(
            f"- timestamp={h.segment_timestamp:g} ({format_timestamp(h.segment_timestamp)})\n"
            f"  why: {h.why_surface}\n"
            f"  excerpt: {excerpt}"
        )
    return "\n".join(lines)


BRIEF_SYSTEM = """\
You prepare a source brief for the writer of a podcast episode. The listener \
has not heard this source. From the brief alone, a host must be able to \
introduce it accurately in a few sentences: who is talking and why they are \
worth hearing, what the piece is, why it exists, and what it argues.

Use only the material you are given. Never invent a name, title, credential, \
number or date. If the material does not name a person, leave them out; the \
script will say "the guest". Labels such as "Speaker A" or "Speaker 0" are \
diarization labels, not names."""


def brief_prompt(episode: EpisodeDigest) -> str:
    return (
        f"SOURCE\n{source_material(episode)}\n\n"
        "Reply with ONLY a JSON object, no prose and no code fences:\n"
        '{"people": [{"name": "...", "role": "host|guest|author|other", '
        '"credential": "... or null"}], "context": "...", "thesis": "...", '
        '"key_points": [{"text": "...", "timestamp": <seconds>}]}\n\n'
        "- people: everyone the material names as speaking in or authoring this source. "
        "credential is why they are worth hearing (role, company, track record), only as "
        "the material states it.\n"
        "- context: one or two sentences on why this conversation is happening (a launch, a "
        "fundraise, a book, a news event) if the material says; otherwise what the show is "
        "and what this episode sets out to do.\n"
        "- thesis: the core argument of the source in one or two plain sentences.\n"
        "- key_points: one per surfaced moment worth discussing, in the order they occur, each "
        "a plain paraphrase of what is said there. Copy timestamp exactly from the list above."
    )


def _corpus(episode: EpisodeDigest) -> str:
    parts = [
        episode.show or "",
        episode.episode_title or "",
        episode.description or "",
        episode.intro_excerpt,
        *(h.excerpt or h.quote for h in episode.highlights),
    ]
    return " ".join(parts).lower()


def name_supported(name: str, corpus: str) -> bool:
    """A person's name is kept only if every substantive token of it appears in
    the source material: the brief may not introduce people the source never
    names. `corpus` is lowercased."""
    tokens = [t for t in _NAME_TOKEN_RE.findall(name.lower()) if len(t) >= MIN_NAME_TOKEN_CHARS]
    return bool(tokens) and all(re.search(rf"\b{re.escape(t)}\b", corpus) for t in tokens)


class BriefError(ValueError):
    """A model's brief could not be used (unparseable, or missing its context
    or thesis). Carries a message the composer feeds back on a retry."""


def parse_brief(data: Any, episode: EpisodeDigest) -> SourceBrief:
    """Validate a model's brief against the episode it describes. Key points
    must cite a surfaced timestamp and people must be named in the material;
    anything else is dropped rather than trusted."""
    if not isinstance(data, dict):
        raise BriefError("reply is not a JSON object")
    context = str(data.get("context") or "").strip()
    thesis = str(data.get("thesis") or "").strip()
    if not context or not thesis:
        raise BriefError("context and thesis are both required and must be non-empty")

    corpus = _corpus(episode)
    people: list[Person] = []
    for raw in data.get("people") or []:
        try:
            person = Person.model_validate(raw)
        except ValidationError:
            continue
        if name_supported(person.name, corpus):
            people.append(person)

    valid_ts = {round(h.segment_timestamp): h.segment_timestamp for h in episode.highlights}
    points: list[BriefPoint] = []
    for raw in data.get("key_points") or []:
        if not isinstance(raw, dict):
            continue
        try:
            ts = float(raw.get("timestamp"))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
        text = str(raw.get("text") or "").strip()
        if text and round(ts) in valid_ts:
            points.append(BriefPoint(text=text, segment_timestamp=valid_ts[round(ts)]))

    return SourceBrief(
        episode_id=episode.episode_id,
        show=episode.show,
        title=episode.episode_title,
        published_at=episode.published_at,
        people=people,
        context=context,
        thesis=thesis,
        key_points=points,
    )


def _first_sentence(text: str) -> str:
    return _SENTENCE_END_RE.split(text.strip(), maxsplit=1)[0]


def mock_brief(episode: EpisodeDigest) -> SourceBrief:
    """Deterministic brief from metadata alone (no model): the offline and test path."""
    label = episode.episode_title or episode.show or "this episode"
    context = (
        _first_sentence(episode.description)
        if episode.description
        else f"{episode.show or 'This show'} published {label}."
    )
    ordered = sorted(episode.highlights, key=lambda h: h.segment_timestamp)
    top = max(episode.highlights, key=lambda h: h.relevance_score)
    return SourceBrief(
        episode_id=episode.episode_id,
        show=episode.show,
        title=episode.episode_title,
        published_at=episode.published_at,
        people=[],
        context=context,
        thesis=top.why_surface,
        key_points=[
            BriefPoint(text=_first_sentence(h.excerpt or h.quote), segment_timestamp=h.segment_timestamp)
            for h in ordered
        ],
    )
