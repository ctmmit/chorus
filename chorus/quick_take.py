"""Brief me now: a verdict on one episode in about a minute.

The weekly digest answers "what mattered this week". A quick take answers a
different moment: a new episode just dropped, and the principal wants to
know whether it is worth three hours. It runs the same curation on that one
episode, then decides:

- `skip` when nothing cleared the principal's bar (curation refused);
- `listen` when at least `LISTEN_MIN_HIGHLIGHTS` moments cleared it and the
  best scored at least `LISTEN_SCORE`: worth the whole episode;
- `skim` otherwise: worth the cited moments, not the whole thing.

The verdict is a pure rule on the scores, so it is the same whoever writes
the words. A writer then gives up to `MAX_REASONS` reasons, each resting on
one highlight (a reason citing anything else is dropped, and the mock's
reasons stand in if none survive), and a 60 to 90 second spoken take.

A quick take is stored as an ordinary job (`Job.quick_take`), so ownership,
the artifact route, the private feed and chapters all apply: with audio,
the episode has a chapter for the verdict and one per reason.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any, Literal, Protocol, runtime_checkable

from fastapi import APIRouter, HTTPException, Request
from mcp.server.fastmcp import Context, FastMCP  # type: ignore[import-not-found,import-untyped]
from pydantic import BaseModel, Field

from chorus.curation import curate_episode, soul_version
from chorus.jobs import MASTER_OWNER, JobStore
from chorus.models import (
    MAX_CONTEXT_CHARS,
    MAX_SOUL_CHARS,
    Digest,
    DigestRequest,
    EpisodeDigest,
    EpisodeInput,
    EpisodeOutline,
    Job,
    JobStatus,
    OutlineSegment,
    QuickReason,
    QuickTake,
    ResolvedEpisode,
    Script,
    Turn,
)
from chorus.pipeline import Deps, stage_audio

log = logging.getLogger("chorus.quick_take")

Verdict = Literal["listen", "skim", "skip"]
LISTEN_SCORE = 0.7
LISTEN_MIN_HIGHLIGHTS = 2
MAX_REASONS = 3
QUICK_HIGHLIGHTS = 4
# A spoken minute is about 150 words; a quick take is 60 to 90 seconds.
TAKE_MIN_WORDS = 150
TAKE_MAX_WORDS = 225
SKIP_LINE = "Nothing in it cleared your bar, so this one is a skip."


# --- Pure logic ----------------------------------------------------------------


def verdict_for(digest: EpisodeDigest) -> Verdict:
    if digest.refused or not digest.highlights:
        return "skip"
    best = max(h.relevance_score for h in digest.highlights)
    if len(digest.highlights) >= LISTEN_MIN_HIGHLIGHTS and best >= LISTEN_SCORE:
        return "listen"
    return "skim"


class DraftReason(BaseModel):
    text: str
    highlight_id: str


class QuickDraft(BaseModel):
    reasons: list[DraftReason] = Field(default_factory=list)
    monologue: str = ""


def ground_reasons(drafts: list[DraftReason], digest: EpisodeDigest) -> list[QuickReason]:
    """Reasons citing a real highlight, each highlight once, at most MAX_REASONS."""
    by_id = {h.highlight_id: h for h in digest.highlights}
    kept: list[QuickReason] = []
    for draft in drafts:
        h = by_id.pop(draft.highlight_id, None)
        if h is None or not draft.text.strip():
            continue
        kept.append(QuickReason(
            text=draft.text.strip(), highlight_id=h.highlight_id,
            segment_timestamp=h.segment_timestamp, quote=h.quote,
        ))
        if len(kept) == MAX_REASONS:
            break
    return kept


def _label(digest: EpisodeDigest) -> str:
    title = digest.episode_title or digest.episode_id
    return f"{digest.show}: {title}" if digest.show else title


def opening_line(digest: EpisodeDigest, verdict: Verdict) -> str:
    return {
        "listen": f"{_label(digest)} is worth your full listen.",
        "skim": f"{_label(digest)} is worth a skim: jump to the moments below.",
        "skip": f"{_label(digest)}: {SKIP_LINE}",
    }[verdict]


# --- Writers ----------------------------------------------------------------------


@runtime_checkable
class QuickTakeWriter(Protocol):
    def write(self, digest: EpisodeDigest, soul: str, context: str, verdict: Verdict) -> QuickDraft: ...


class MockQuickTakeWriter:
    """Deterministic, offline: one reason per top highlight, built from why
    it surfaced and what was said."""

    def write(self, digest: EpisodeDigest, soul: str, context: str, verdict: Verdict) -> QuickDraft:
        top = sorted(digest.highlights, key=lambda h: -h.relevance_score)[:MAX_REASONS]
        reasons = [
            DraftReason(text=f'{h.why_surface}: "{h.quote}"', highlight_id=h.highlight_id)
            for h in top
        ]
        lines = [opening_line(digest, verdict)] + [r.text for r in reasons]
        return QuickDraft(reasons=reasons, monologue=" ".join(lines))


QUICK_SYSTEM = f"""You give a principal a quick take on one podcast episode: whether it is \
worth their time, and why, in a 60 to 90 second spoken monologue ({TAKE_MIN_WORDS} to \
{TAKE_MAX_WORDS} words). The verdict is already decided; explain it. Each reason must rest on \
one of the highlights you are given, cited by its id. Speak plainly, to them, in second person. \
Do not invent anything the highlights do not say.

Reply with ONLY a JSON object: {{"reasons": [{{"text": "...", "highlight_id": "..."}}], \
"monologue": "..."}}"""


class AnthropicQuickTakeWriter:
    MODEL = "claude-sonnet-4-6"
    MAX_TOKENS = 1200

    def __init__(self, api_key: str | None = None, client: Any | None = None) -> None:
        if client is None:
            import anthropic  # type: ignore[import-not-found]  # optional dep; only with a key

            client = anthropic.Anthropic(api_key=api_key)
        self._client: Any = client

    def write(self, digest: EpisodeDigest, soul: str, context: str, verdict: Verdict) -> QuickDraft:
        from chorus.script import json_object

        highlights = "\n".join(
            f'- id {h.highlight_id} at {int(h.segment_timestamp)}s: "{h.quote}" (why: {h.why_surface})'
            for h in digest.highlights
        )
        user = (
            f"LISTENER LENS:\n{soul}\n\nTHEIR CONTEXT:\n{context or '(none)'}\n\n"
            f"EPISODE: {_label(digest)}\nVERDICT: {verdict}\n\nHIGHLIGHTS:\n{highlights}"
        )
        msg = self._client.messages.create(
            model=self.MODEL, max_tokens=self.MAX_TOKENS, system=QUICK_SYSTEM,
            messages=[{"role": "user", "content": user}],
        )
        body = "".join(b.text for b in msg.content if b.type == "text")
        try:
            return QuickDraft.model_validate(json_object(body))
        except ValueError as err:
            log.info("quick take: unusable reply (%s); using the deterministic take", err)
            return MockQuickTakeWriter().write(digest, soul, context, verdict)


def quick_writer_for(llm: object) -> Callable[[], QuickTakeWriter]:
    """Follow the deployment's scorer, as chorus.feedback.proposal_writer_for does."""
    import os

    from chorus.llm import MockLLMClient

    if isinstance(llm, MockLLMClient):
        return MockQuickTakeWriter
    key = os.environ.get("ANTHROPIC_API_KEY")
    return (lambda: AnthropicQuickTakeWriter(key)) if key else MockQuickTakeWriter


# --- The take ---------------------------------------------------------------------------


def build_take(
    digest: EpisodeDigest, soul: str, context: str, writer: QuickTakeWriter
) -> QuickTake:
    verdict = verdict_for(digest)
    if verdict == "skip":
        return QuickTake(
            episode_id=digest.episode_id, title=digest.episode_title, show=digest.show,
            verdict="skip", monologue=opening_line(digest, "skip"),
        )
    draft = writer.write(digest, soul, context, verdict)
    reasons = ground_reasons(draft.reasons, digest)
    monologue = draft.monologue.strip()
    if not reasons:
        fallback = MockQuickTakeWriter().write(digest, soul, context, verdict)
        reasons = ground_reasons(fallback.reasons, digest)
        monologue = fallback.monologue
    return QuickTake(
        episode_id=digest.episode_id, title=digest.episode_title, show=digest.show,
        verdict=verdict, reasons=reasons, monologue=monologue or opening_line(digest, verdict),
    )


def take_script(take: QuickTake, version: str) -> Script:
    """The spoken take as a script with an outline, so chorus/chapters.py
    gives it a chapter for the verdict and one per reason."""
    segments = [OutlineSegment(name=f"Verdict: {take.verdict}", kind="intro",
                               description="the verdict")]
    turns = [Turn(speaker="host", text=take.monologue, segment_index=0)]
    for i, reason in enumerate(take.reasons, start=1):
        segments.append(OutlineSegment(name=f"Reason {i}", kind="body",
                                       source_ids=[take.episode_id], description=reason.text))
        turns.append(Turn(
            speaker="host", text=f'At {int(reason.segment_timestamp)} seconds: "{reason.quote}"',
            episode_id=take.episode_id, segment_timestamp=reason.segment_timestamp,
            segment_index=i,
        ))
    return Script(
        soul_version=version, takes=[], monologue="\n\n".join(t.text for t in turns),
        turns=turns, outline=EpisodeOutline(segments=segments),
    )


def quick_take(
    episode: EpisodeInput,
    soul: str,
    context: str,
    deps: Deps,
    store: JobStore,
    owner: str = MASTER_OWNER,
    *,
    writer: QuickTakeWriter | None = None,
    audio: bool = False,
) -> Job:
    """Judge one episode and store the result as a done job. A transcript
    that cannot be read fails the job with the reason, never a guessed take."""
    job_id = store.create(owner=owner)
    job = store.get(job_id)
    assert job is not None
    try:
        transcript = deps.provider.get(episode)
    except Exception as err:  # noqa: BLE001 - the reason is the answer
        job.status = JobStatus.failed
        job.error = f"no transcript for this episode: {type(err).__name__}: {err}"
        store.save(job)
        return job
    resolved = ResolvedEpisode(episode=episode, transcript=transcript)
    digest = curate_episode(resolved, soul, context, deps.llm, max_highlights=QUICK_HIGHLIGHTS)
    take = build_take(digest, soul, context, (writer or quick_writer_for(deps.llm)()))
    version = soul_version(soul)
    job.digest = Digest(soul_version=version, soul_origin="supplied", episodes=[digest])
    job.quick_take = take
    job.script = take_script(take, version)
    if audio:
        request = DigestRequest(soul=soul, context=context, episodes=[episode])
        try:
            rendered = stage_audio(job.script, request, job_id, deps.renderer, deps.artifacts,
                                   job.digest)
            job.audio_url = rendered.url
            job.chapters = rendered.chapters
        except Exception as err:  # noqa: BLE001 - the take stands without audio
            job.warnings.append(f"audio render failed: {type(err).__name__}: {err}")
    job.status = JobStatus.done
    store.save(job)
    return job


# --- HTTP and MCP ---------------------------------------------------------------------------


class QuickTakeRequest(BaseModel):
    episode: EpisodeInput
    soul: str = Field(min_length=1, max_length=MAX_SOUL_CHARS)
    context: str = Field(default="", max_length=MAX_CONTEXT_CHARS)
    audio: bool = Field(default=False, description="Also render the spoken take (slower).")


def build_quick_take_router(store: JobStore, deps: Deps) -> APIRouter:
    router = APIRouter()

    @router.post("/quick-take")
    def quick(body: QuickTakeRequest, request: Request) -> dict[str, Any]:
        owner = getattr(request.state, "owner", MASTER_OWNER)
        job = quick_take(body.episode, body.soul, body.context, deps, store, owner,
                         audio=body.audio)
        if job.status is JobStatus.failed:
            raise HTTPException(status_code=422, detail=job.error)
        return job.model_dump(mode="json")

    return router


def share_quick_taker(
    store: JobStore, deps: Deps, subscriptions: Any | None
) -> Callable[[str, Any, EpisodeInput], dict[str, Any]]:
    """The library share route's quick mode: the lens comes from the request's
    soul or from the owner's subscription (chorus/library_api.py)."""

    def take(owner: str, share: Any, episode: EpisodeInput) -> dict[str, Any]:
        soul, context = share.soul, ""
        if share.subscription_id:
            sub = subscriptions.get(share.subscription_id) if subscriptions else None
            if sub is None or (owner != MASTER_OWNER and sub.owner != owner):
                raise ValueError(f"unknown subscription {share.subscription_id}")
            soul, context = sub.soul, sub.context
        if not soul:
            raise ValueError('mode "quick" needs a soul or a subscription_id')
        job = quick_take(episode, soul, context, deps, store, owner)
        if job.status is JobStatus.failed:
            raise ValueError(job.error or "the quick take failed")
        return job.model_dump(mode="json", include={"job_id", "quick_take"})

    return take


def register_quick_take_tools(server: FastMCP, store: JobStore, deps: Deps) -> None:
    """quick_take: listen, skim or skip, with reasons, for one episode."""
    from chorus.mcp_server import _owner_from_context

    def quick_take_tool(
        episode: EpisodeInput,
        soul: str,
        context: str = "",
        audio: bool = False,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Judge one episode right now: `listen`, `skim` or `skip`, with up to
        three reasons that each cite a moment in the episode, and a 60 to 90
        second spoken take (rendered when `audio` is true). Use when a new
        episode drops and the principal asks whether it is worth their time."""
        request = QuickTakeRequest(episode=episode, soul=soul, context=context, audio=audio)
        job = quick_take(request.episode, request.soul, request.context, deps, store,
                         _owner_from_context(ctx), audio=request.audio)
        if job.status is JobStatus.failed:
            return {"error": job.error, "job_id": job.job_id}
        return job.model_dump(mode="json", include={"job_id", "quick_take", "audio_url",
                                                    "chapters", "warnings"})

    server.add_tool(quick_take_tool, name="quick_take")
