"""Highlight feedback, and soul updates proposed from it.

The soul is the lens curation looks through, and until now it changed only
when someone edited it. This module closes the loop: the principal rates
highlights (`up` or `down`, with an optional note), the ratings are
summarized, and a writer proposes concrete edits to the soul. Nothing is
applied without an explicit accept, so the principal stays the author of
their own lens.

- Ratings arrive from the MCP tool `rate_highlight`, `POST /feedback`, or a
  signed one-click link in the digest email. Each rating snapshots the
  highlight it is about (show, quote, reason, score), so summaries never
  need the job again. Re-rating the same highlight overwrites.
- `summarize_feedback` is pure: tallies by show and by score band, plus the
  principal's notes verbatim.
- `propose_soul_update` refuses below `MIN_RATINGS_FOR_PROPOSAL`, so two
  clicks never rewrite a lens. Each proposed edit names the section it
  touches and the ratings that justify it.
- `apply_soul_update` is pure: it applies the accepted edits to the soul's
  markdown and refuses a result that no longer validates.

`FeedbackService` holds the stores and is shared by the HTTP router and the
MCP tools, the same split as chorus.library_api.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import sqlite3
import threading
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from mcp.server.fastmcp import Context, FastMCP  # type: ignore[import-not-found,import-untyped]
from pydantic import BaseModel, Field

from chorus.curation import soul_version
from chorus.jobs import DEFAULT_DB, MASTER_OWNER, JobStore
from chorus.models import Highlight, Job
from chorus.soul import SoulSection, _classify, describe_problems, validate_soul

log = logging.getLogger("chorus.feedback")

Vote = Literal["up", "down"]
MIN_RATINGS_FOR_PROPOSAL = 8
MAX_NOTE_CHARS = 500
SHOW_RULE_MIN_VOTES = 3
MAX_EDITS = 8
MAX_SUMMARY_NOTES = 20
HIGH_SCORE = 0.7
LOW_SCORE = 0.5
FEEDBACK_TABLE = "highlight_ratings"
PROPOSALS_TABLE = "soul_proposals"

# Sections a proposal may edit: the topic signal curation reads. Identity and
# guidance prose stay the principal's to write.
EditableSection = Literal["attention_triggers", "anti_interests", "core_interests"]
_SECTION_HEADINGS: dict[str, SoulSection] = {
    "attention_triggers": SoulSection.attention_triggers,
    "anti_interests": SoulSection.anti_interests,
    "core_interests": SoulSection.core_interests,
}


class FeedbackError(ValueError):
    """A rating or proposal request that cannot be honored; the message says why."""


class NotEnoughFeedback(FeedbackError):
    """Too few ratings to justify changing the soul."""


# --- Models --------------------------------------------------------------------


class Rating(BaseModel):
    owner: str
    job_id: str
    highlight_id: str
    vote: Vote
    note: str = Field(default="", max_length=MAX_NOTE_CHARS)
    rated_at: datetime
    episode_id: str
    show: str | None = None
    quote: str = ""
    why_surface: str = ""
    relevance_score: float = 0.0


class ShowTally(BaseModel):
    up: int = 0
    down: int = 0


class FeedbackSummary(BaseModel):
    total: int
    up: int
    down: int
    by_show: dict[str, ShowTally] = Field(default_factory=dict)
    by_band: dict[str, ShowTally] = Field(
        default_factory=dict, description='Votes by relevance band: "high", "mid", "low".'
    )
    notes_up: list[str] = Field(default_factory=list)
    notes_down: list[str] = Field(default_factory=list)
    quotes_down: list[str] = Field(default_factory=list)


class SoulEdit(BaseModel):
    section: EditableSection
    action: Literal["add", "remove"]
    text: str = Field(min_length=1, max_length=300, description="The bullet's text, no leading dash.")
    evidence: str = Field(description="The ratings that justify this edit, in plain words.")


class SoulProposal(BaseModel):
    proposal_id: str
    owner: str
    base_soul_version: str
    edits: list[SoulEdit]
    summary: FeedbackSummary
    created_at: datetime
    applied_at: datetime | None = None


# --- Pure logic -------------------------------------------------------------------


def find_highlight(job: Job, highlight_id: str) -> Highlight:
    for highlight in job.digest.highlights if job.digest else []:
        if highlight.highlight_id == highlight_id:
            return highlight
    raise FeedbackError(f"job {job.job_id} has no highlight {highlight_id}")


def make_rating(
    job: Job, highlight_id: str, vote: Vote, note: str, now: datetime
) -> Rating:
    highlight = find_highlight(job, highlight_id)
    return Rating(
        owner=job.owner,
        job_id=job.job_id,
        highlight_id=highlight_id,
        vote=vote,
        note=note.strip()[:MAX_NOTE_CHARS],
        rated_at=now,
        episode_id=highlight.episode_id,
        show=highlight.show,
        quote=highlight.quote,
        why_surface=highlight.why_surface,
        relevance_score=highlight.relevance_score,
    )


def _band(score: float) -> str:
    if score >= HIGH_SCORE:
        return "high"
    return "mid" if score >= LOW_SCORE else "low"


def summarize_feedback(ratings: list[Rating]) -> FeedbackSummary:
    """Tallies and the principal's own words, newest ratings first."""
    ordered = sorted(ratings, key=lambda r: r.rated_at, reverse=True)
    by_show: dict[str, ShowTally] = {}
    by_band: dict[str, ShowTally] = {}
    for rating in ordered:
        for key, table in ((rating.show, by_show), (_band(rating.relevance_score), by_band)):
            if key is None:
                continue
            tally = table.setdefault(key, ShowTally())
            if rating.vote == "up":
                tally.up += 1
            else:
                tally.down += 1
    return FeedbackSummary(
        total=len(ordered),
        up=sum(r.vote == "up" for r in ordered),
        down=sum(r.vote == "down" for r in ordered),
        by_show=by_show,
        by_band=by_band,
        notes_up=[r.note for r in ordered if r.vote == "up" and r.note][:MAX_SUMMARY_NOTES],
        notes_down=[r.note for r in ordered if r.vote == "down" and r.note][:MAX_SUMMARY_NOTES],
        quotes_down=[r.quote for r in ordered if r.vote == "down" and r.quote][:MAX_SUMMARY_NOTES],
    )


def _section_bounds(lines: list[str], section: SoulSection) -> tuple[int, int] | None:
    """(heading line, end line exclusive) of the first heading that is `section`."""
    start: int | None = None
    for i, line in enumerate(lines):
        if line.startswith("## "):
            if start is not None:
                return start, i
            if _classify(line[3:].strip()) is section:
                start = i
    return (start, len(lines)) if start is not None else None


def apply_soul_update(soul: str, proposal: SoulProposal, accept: list[int]) -> str:
    """The soul with the accepted edits (by index into `proposal.edits`)
    applied. An add appends a bullet to the end of its section, creating the
    section if the soul has none; a remove deletes the matching bullet. The
    result must still validate."""
    unknown = [i for i in accept if not 0 <= i < len(proposal.edits)]
    if unknown:
        raise FeedbackError(f"no edit at index {unknown}; the proposal has {len(proposal.edits)}")
    lines = soul.rstrip("\n").split("\n")
    for index in sorted(set(accept)):
        edit = proposal.edits[index]
        section = _SECTION_HEADINGS[edit.section]
        bullet = f"- {edit.text.strip()}"
        bounds = _section_bounds(lines, section)
        if edit.action == "remove":
            if bounds is None:
                continue
            start, end = bounds
            lines = lines[:start] + [
                line for line in lines[start:end] if line.strip() != bullet
            ] + lines[end:]
            continue
        if bounds is None:
            lines += ["", f"## {section.value}", bullet]
            continue
        start, end = bounds
        insert_at = end
        while insert_at > start + 1 and not lines[insert_at - 1].strip():
            insert_at -= 1
        lines.insert(insert_at, bullet)
    updated = "\n".join(lines) + "\n"
    check = validate_soul(updated)
    if not check.valid:
        raise FeedbackError(f"the updated soul would not validate ({describe_problems(check)})")
    return updated


# --- Proposal writers --------------------------------------------------------------


@runtime_checkable
class ProposalWriter(Protocol):
    def write(self, soul: str, summary: FeedbackSummary) -> list[SoulEdit]: ...


class MockProposalWriter:
    """Deterministic, offline. The principal's notes become bullets (a
    down-vote note under Ignore, an up-vote note under Attention triggers),
    and a show voted down at least `SHOW_RULE_MIN_VOTES` times with no
    up-votes gets an Ignore rule."""

    def write(self, soul: str, summary: FeedbackSummary) -> list[SoulEdit]:
        edits: list[SoulEdit] = []
        for note in dict.fromkeys(summary.notes_down):
            edits.append(SoulEdit(
                section="anti_interests", action="add", text=note,
                evidence="your note on a highlight you voted down",
            ))
        for note in dict.fromkeys(summary.notes_up):
            edits.append(SoulEdit(
                section="attention_triggers", action="add", text=note,
                evidence="your note on a highlight you voted up",
            ))
        for show, tally in sorted(summary.by_show.items()):
            if tally.down >= SHOW_RULE_MIN_VOTES and tally.up == 0:
                edits.append(SoulEdit(
                    section="anti_interests", action="add",
                    text=f"Routine segments from {show}",
                    evidence=f"you voted down {tally.down} of {tally.down} highlights from {show}",
                ))
        return edits[:MAX_EDITS]


PROPOSAL_SYSTEM = """You maintain a principal's curation lens (their "soul"), a markdown \
document that decides which podcast segments are worth their time. You are given the soul \
and a summary of how the principal rated recent highlights. Propose a few concrete edits to \
the soul's topic lists that would make the next digest closer to what they kept and further \
from what they rejected.

Rules:
- Edit only these sections: attention_triggers, anti_interests, core_interests.
- Each edit adds or removes ONE bullet. Text is a short topic phrase, no leading dash.
- A remove must quote an existing bullet exactly.
- Every edit cites the ratings that justify it in `evidence`, in plain words.
- Propose nothing the ratings do not support. Fewer, sharper edits beat many.

Reply with ONLY a JSON object: {"edits": [{"section": "...", "action": "add"|"remove", \
"text": "...", "evidence": "..."}]}"""


class AnthropicProposalWriter:
    MODEL = "claude-sonnet-4-6"
    MAX_TOKENS = 1500

    def __init__(self, api_key: str | None = None, client: Any | None = None) -> None:
        if client is None:
            import anthropic  # type: ignore[import-not-found]  # optional dep; only with a key

            client = anthropic.Anthropic(api_key=api_key)
        self._client: Any = client

    def write(self, soul: str, summary: FeedbackSummary) -> list[SoulEdit]:
        from chorus.script import json_object

        user = f"SOUL:\n{soul}\n\nRATINGS SUMMARY:\n{summary.model_dump_json(indent=2)}"
        msg = self._client.messages.create(
            model=self.MODEL,
            max_tokens=self.MAX_TOKENS,
            system=PROPOSAL_SYSTEM,
            messages=[{"role": "user", "content": user}],
        )
        body = "".join(b.text for b in msg.content if b.type == "text")
        data = json_object(body)
        edits: list[SoulEdit] = []
        for raw in data.get("edits", []):
            try:
                edits.append(SoulEdit.model_validate(raw))
            except ValueError as err:
                log.info("feedback: dropping unusable proposed edit (%s)", err)
        return edits[:MAX_EDITS]


def get_proposal_writer() -> ProposalWriter:
    import os

    key = os.environ.get("ANTHROPIC_API_KEY")
    return AnthropicProposalWriter(key) if key else MockProposalWriter()


def proposal_writer_for(llm: object) -> Callable[[], ProposalWriter]:
    """Follow the deployment's scorer: a mock scorer gets the mock writer, so
    a mock or test deployment never makes a billed call (chorus.pipeline.Deps)."""
    from chorus.llm import MockLLMClient

    if isinstance(llm, MockLLMClient):
        return MockProposalWriter
    return get_proposal_writer


def propose_soul_update(
    soul: str,
    ratings: list[Rating],
    owner: str,
    writer: ProposalWriter,
    now: datetime,
    min_ratings: int = MIN_RATINGS_FOR_PROPOSAL,
) -> SoulProposal:
    """A proposal from the ratings, or NotEnoughFeedback below the minimum.
    Remove edits that name a bullet the soul does not have are dropped."""
    if len(ratings) < min_ratings:
        raise NotEnoughFeedback(
            f"{len(ratings)} rating(s) so far; a proposal needs at least {min_ratings} so a "
            "couple of clicks never rewrite the lens"
        )
    summary = summarize_feedback(ratings)
    bullets = {line.strip() for line in soul.splitlines()}
    edits = [
        e for e in writer.write(soul, summary)
        if e.action == "add" or f"- {e.text.strip()}" in bullets
    ]
    return SoulProposal(
        proposal_id=uuid.uuid4().hex,
        owner=owner,
        base_soul_version=soul_version(soul),
        edits=edits,
        summary=summary,
        created_at=now,
    )


PROPOSAL_READY_NOTE = (
    "You have rated {count} highlights since your lens last changed. Ask your agent to "
    "propose updates to your soul from them; nothing changes until you accept an edit."
)


def proposal_nudge(ratings: list[Rating], latest: SoulProposal | None) -> str | None:
    """One line for the digest email once enough ratings have accumulated
    since the last proposal (or ever, when there is none). Silent otherwise,
    so it never nags week after week about the same ratings."""
    since = latest.created_at if latest else None
    fresh = [r for r in ratings if since is None or r.rated_at > since]
    if len(fresh) < MIN_RATINGS_FOR_PROPOSAL:
        return None
    return PROPOSAL_READY_NOTE.format(count=len(fresh))


# --- Signed email links ------------------------------------------------------------


def _link_secret() -> bytes:
    from chorus.subscriptions import _unsubscribe_secret

    return _unsubscribe_secret()


def rating_signature(owner: str, job_id: str, highlight_id: str, vote: str) -> str:
    message = f"chorus-rating:{owner}:{job_id}:{highlight_id}:{vote}".encode()
    return hmac.new(_link_secret(), message, hashlib.sha256).hexdigest()[:32]


def rating_link(base_url: str, job: Job, highlight_id: str, vote: Vote) -> str:
    """A one-click link that records `vote` without an API token."""
    sig = rating_signature(job.owner, job.job_id, highlight_id, vote)
    return f"{base_url.rstrip('/')}/feedback/{job.job_id}/{highlight_id}?v={vote}&sig={sig}"


# --- Storage ---------------------------------------------------------------------------


@runtime_checkable
class FeedbackStore(Protocol):
    def rate(self, rating: Rating) -> None:
        """Upsert by (owner, job_id, highlight_id)."""
        ...

    def ratings(self, owner: str) -> list[Rating]: ...

    def save_proposal(self, proposal: SoulProposal) -> None: ...

    def get_proposal(self, proposal_id: str) -> SoulProposal | None: ...

    def latest_proposal(self, owner: str) -> SoulProposal | None:
        """The owner's most recent proposal, applied or not."""
        ...

    def close(self) -> None: ...


class SqliteFeedbackStore:
    def __init__(self, db_path: Path | str = DEFAULT_DB) -> None:
        self.db_path = str(db_path)
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock:
            self._conn.execute(
                f"CREATE TABLE IF NOT EXISTS {FEEDBACK_TABLE} (owner TEXT NOT NULL, "
                "job_id TEXT NOT NULL, highlight_id TEXT NOT NULL, rated_at TEXT NOT NULL, "
                "payload TEXT NOT NULL, PRIMARY KEY (owner, job_id, highlight_id))"
            )
            self._conn.execute(
                f"CREATE TABLE IF NOT EXISTS {PROPOSALS_TABLE} (proposal_id TEXT PRIMARY KEY, "
                "owner TEXT NOT NULL, payload TEXT NOT NULL)"
            )
            self._conn.commit()

    def rate(self, rating: Rating) -> None:
        with self._lock:
            self._conn.execute(
                f"INSERT OR REPLACE INTO {FEEDBACK_TABLE} "
                "(owner, job_id, highlight_id, rated_at, payload) VALUES (?, ?, ?, ?, ?)",
                (rating.owner, rating.job_id, rating.highlight_id,
                 rating.rated_at.isoformat(), rating.model_dump_json()),
            )
            self._conn.commit()

    def ratings(self, owner: str) -> list[Rating]:
        with self._lock:
            rows = self._conn.execute(
                f"SELECT payload FROM {FEEDBACK_TABLE} WHERE owner = ? ORDER BY rated_at DESC",
                (owner,),
            ).fetchall()
        return [Rating.model_validate_json(r[0]) for r in rows]

    def save_proposal(self, proposal: SoulProposal) -> None:
        with self._lock:
            self._conn.execute(
                f"INSERT OR REPLACE INTO {PROPOSALS_TABLE} (proposal_id, owner, payload) "
                "VALUES (?, ?, ?)",
                (proposal.proposal_id, proposal.owner, proposal.model_dump_json()),
            )
            self._conn.commit()

    def get_proposal(self, proposal_id: str) -> SoulProposal | None:
        with self._lock:
            row = self._conn.execute(
                f"SELECT payload FROM {PROPOSALS_TABLE} WHERE proposal_id = ?", (proposal_id,)
            ).fetchone()
        return SoulProposal.model_validate_json(row[0]) if row else None

    def latest_proposal(self, owner: str) -> SoulProposal | None:
        with self._lock:
            rows = self._conn.execute(
                f"SELECT payload FROM {PROPOSALS_TABLE} WHERE owner = ?", (owner,)
            ).fetchall()
        proposals = [SoulProposal.model_validate_json(r[0]) for r in rows]
        return max(proposals, key=lambda p: p.created_at, default=None)

    def close(self) -> None:
        with self._lock:
            self._conn.close()


# --- Service ------------------------------------------------------------------------------

SoulSaver = Callable[[str], None]


class AppliedSoul(BaseModel):
    soul: str
    soul_version: str
    soul_origin: str
    applied: list[int]
    saved_to: str | None = Field(
        default=None, description="Where the new soul was written, if Chorus holds it."
    )


class FeedbackService:
    """Rate, propose and apply, for one deployment's stores. `subscriptions`
    lets a proposal read and update a subscription's soul; `local_soul`
    (local installs) loads and saves the principal's configured soul."""

    def __init__(
        self,
        feedback: FeedbackStore,
        jobs: JobStore,
        subscriptions: Any | None = None,
        writer_factory: Callable[[], ProposalWriter] = get_proposal_writer,
        local_soul: tuple[Callable[[], str], SoulSaver] | None = None,
        on_rating: Callable[[Rating, Job], None] | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.feedback = feedback
        self.jobs = jobs
        self.subscriptions = subscriptions
        self.writer_factory = writer_factory
        self.local_soul = local_soul
        self.clock = clock
        self.on_rating = on_rating

    def _job(self, owner: str, job_id: str) -> Job:
        job = self.jobs.get(job_id)
        if job is None or (owner != MASTER_OWNER and job.owner != owner):
            raise FeedbackError(f"unknown job {job_id}")
        return job

    def rate(self, owner: str, job_id: str, highlight_id: str, vote: Vote, note: str = "") -> Rating:
        job = self._job(owner, job_id)
        rating = make_rating(job, highlight_id, vote, note, self.clock())
        self.feedback.rate(rating)
        if self.on_rating is not None:
            try:
                self.on_rating(rating, job)
            except Exception as err:  # noqa: BLE001 - an endorsement never costs the rating
                log.warning("feedback: rating hook failed (non-fatal): %s", err)
        return rating

    def _subscription(self, owner: str, subscription_id: str) -> Any:
        if self.subscriptions is None:
            raise FeedbackError("this deployment has no subscriptions")
        sub = self.subscriptions.get(subscription_id)
        if sub is None or (owner != MASTER_OWNER and sub.owner != owner):
            raise FeedbackError(f"unknown subscription {subscription_id}")
        return sub

    def _current_soul(self, owner: str, soul: str | None, subscription_id: str | None) -> str:
        if soul:
            return soul
        if subscription_id:
            return str(self._subscription(owner, subscription_id).soul)
        if self.local_soul is not None:
            return self.local_soul[0]()
        raise FeedbackError("give the current soul, or a subscription_id whose soul to update")

    def propose(
        self, owner: str, soul: str | None = None, subscription_id: str | None = None
    ) -> SoulProposal:
        current = self._current_soul(owner, soul, subscription_id)
        proposal = propose_soul_update(
            current, self.feedback.ratings(owner), owner, self.writer_factory(), self.clock()
        )
        self.feedback.save_proposal(proposal)
        return proposal

    def apply(
        self,
        owner: str,
        proposal_id: str,
        accept: list[int],
        soul: str | None = None,
        subscription_id: str | None = None,
    ) -> AppliedSoul:
        proposal = self.feedback.get_proposal(proposal_id)
        if proposal is None or (owner != MASTER_OWNER and proposal.owner != owner):
            raise FeedbackError(f"unknown proposal {proposal_id}")
        current = self._current_soul(owner, soul, subscription_id)
        if soul_version(current) != proposal.base_soul_version:
            raise FeedbackError(
                "the soul changed since this proposal was made; ask for a new proposal"
            )
        updated = apply_soul_update(current, proposal, accept)
        origin = f"feedback:{proposal_id}"
        saved_to: str | None = None
        if subscription_id:
            sub = self._subscription(owner, subscription_id)
            self.subscriptions.save(sub.model_copy(update={"soul": updated, "soul_origin": origin}))  # type: ignore[union-attr]
            saved_to = f"subscription {subscription_id}"
        elif soul is None and self.local_soul is not None:
            self.local_soul[1](updated)
            saved_to = "your configured soul"
        proposal.applied_at = self.clock()
        self.feedback.save_proposal(proposal)
        return AppliedSoul(
            soul=updated,
            soul_version=soul_version(updated),
            soul_origin=origin,
            applied=sorted(set(accept)),
            saved_to=saved_to,
        )


# --- HTTP -------------------------------------------------------------------------------------

FEEDBACK_LINK_SUFFIX_PREFIX = "/feedback/"


class RateRequest(BaseModel):
    job_id: str
    highlight_id: str
    vote: Vote
    note: str = Field(default="", max_length=MAX_NOTE_CHARS)


class ProposeRequest(BaseModel):
    soul: str | None = None
    subscription_id: str | None = None


class ApplyRequest(BaseModel):
    accept: list[int]
    soul: str | None = None
    subscription_id: str | None = None


_THANKS = """<!doctype html><meta charset="utf-8"><title>Chorus</title>
<body style="font-family:system-ui,sans-serif;max-width:32rem;margin:3rem auto;color:#1C2434">
<p>{message}</p></body>"""


def build_feedback_router(service: FeedbackService) -> APIRouter:
    router = APIRouter()

    def _owner(request: Request) -> str:
        return getattr(request.state, "owner", MASTER_OWNER)

    def _call(fn: Callable[[], Any]) -> Any:
        try:
            return fn()
        except NotEnoughFeedback as err:
            raise HTTPException(status_code=409, detail=str(err)) from err
        except FeedbackError as err:
            raise HTTPException(status_code=404 if "unknown" in str(err) else 422,
                                detail=str(err)) from err

    @router.post("/feedback")
    def rate(body: RateRequest, request: Request) -> dict[str, Any]:
        rating = _call(lambda: service.rate(
            _owner(request), body.job_id, body.highlight_id, body.vote, body.note
        ))
        return {"recorded": rating.model_dump(mode="json")}

    @router.get("/feedback/{job_id}/{highlight_id}")
    def rate_from_email(job_id: str, highlight_id: str, v: str, sig: str) -> HTMLResponse:
        """The one-click link in the digest email: public, HMAC-signed."""
        job = service.jobs.get(job_id)
        if v not in ("up", "down") or job is None or not hmac.compare_digest(
            rating_signature(job.owner, job_id, highlight_id, v), sig
        ):
            raise HTTPException(status_code=404, detail="unknown link")
        vote: Vote = "up" if v == "up" else "down"
        _call(lambda: service.rate(job.owner, job_id, highlight_id, vote))
        message = (
            "Thanks. Chorus will look for more like this."
            if vote == "up"
            else "Thanks. Chorus will show you less like this."
        )
        return HTMLResponse(_THANKS.format(message=message))

    @router.post("/feedback/proposals")
    def propose(body: ProposeRequest, request: Request) -> dict[str, Any]:
        proposal = _call(lambda: service.propose(_owner(request), body.soul, body.subscription_id))
        return proposal.model_dump(mode="json")

    @router.post("/feedback/proposals/{proposal_id}/apply")
    def apply(proposal_id: str, body: ApplyRequest, request: Request) -> dict[str, Any]:
        applied = _call(lambda: service.apply(
            _owner(request), proposal_id, body.accept, body.soul, body.subscription_id
        ))
        return applied.model_dump(mode="json")

    return router


def is_feedback_link(method: str, path: str) -> bool:
    """GET /feedback/{job}/{highlight}: the signed email link, public."""
    parts = path.strip("/").split("/")
    return method == "GET" and len(parts) == 3 and parts[0] == "feedback"


# --- MCP ----------------------------------------------------------------------------------------


def configured_soul_accessors() -> tuple[Callable[[], str], SoulSaver]:
    """Load and save the soul named in a local install's onboarding config."""
    from chorus.onboarding import load_config
    from chorus.soul import load_soul, save_soul

    def _name() -> str:
        name = load_config().soul
        if not name:
            raise FeedbackError("no soul is configured yet; run `chorus onboard`")
        return name

    def _save(markdown: str) -> None:
        save_soul(_name(), markdown)

    return (lambda: load_soul(_name())), _save


def register_feedback_tools(
    server: FastMCP, get_service: Callable[[], FeedbackService]
) -> None:
    """rate_highlight, propose_soul_update, apply_soul_update. The service
    is built on first use, so a server that never rates opens no store."""
    from chorus.mcp_server import _owner_from_context

    def _run(fn: Callable[[], Any]) -> dict[str, Any]:
        try:
            result = fn()
        except FeedbackError as err:
            return {"error": str(err)}
        return result.model_dump(mode="json") if isinstance(result, BaseModel) else dict(result)

    def rate_highlight(
        job_id: str, highlight_id: str, vote: Vote, note: str = "", ctx: Context | None = None
    ) -> dict[str, Any]:
        """Record whether a highlight was worth the principal's time ("up" or
        "down"), with an optional note in their words ("too much fundraising
        gossip"). Notes become proposed soul edits; ratings are never applied
        to the soul without the principal accepting a proposal."""
        return _run(
            lambda: get_service().rate(_owner_from_context(ctx), job_id, highlight_id, vote, note)
        )

    def propose_soul_update_tool(
        soul: str | None = None, subscription_id: str | None = None, ctx: Context | None = None
    ) -> dict[str, Any]:
        """Propose edits to the soul from the principal's ratings. Pass the
        current soul, or a subscription_id whose soul to update (local
        installs may pass neither to use the configured soul). Show the
        principal each edit and its evidence, then call apply_soul_update with
        the indexes they accept."""
        return _run(lambda: get_service().propose(_owner_from_context(ctx), soul, subscription_id))

    def apply_soul_update_tool(
        proposal_id: str,
        accept: list[int],
        soul: str | None = None,
        subscription_id: str | None = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Apply the accepted edits (indexes into the proposal's edits). With a
        subscription_id the subscription's soul is updated; otherwise the new
        soul is returned for you to use (and saved, on a local install)."""
        return _run(lambda: get_service().apply(
            _owner_from_context(ctx), proposal_id, accept, soul, subscription_id
        ))

    server.add_tool(rate_highlight)
    server.add_tool(propose_soul_update_tool, name="propose_soul_update")
    server.add_tool(apply_soul_update_tool, name="apply_soul_update")


def summary_json(summary: FeedbackSummary) -> str:
    return json.dumps(summary.model_dump(mode="json"), indent=2)
