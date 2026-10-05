"""Ask the episode: questions answered only from a digest's own transcripts.

A digest is one-way. The principal will want to ask "what did she say about
pricing?" and get an answer grounded the same way the digest is. `answer`
retrieves the transcript windows of the job's episodes that share the most
content words with the question, then a writer answers from those windows
alone. Every sentence of the answer must cite a retrieved window and quote
it verbatim; a sentence that cites a window that was not retrieved, or
quotes words the window does not contain, is dropped. When nothing in the
episodes speaks to the question, or nothing grounded survives, the answer
is an honest refusal, the same way curation refuses rather than pads.

Transcripts come from the transcript cache the digest filled, or by id for a
YouTube or fixture episode. An RSS episode can be asked about while its
transcript is cached; one that is not is reported as unavailable, never
guessed at.
"""
from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Callable
from typing import Any, Protocol, runtime_checkable

from fastapi import APIRouter, HTTPException, Request
from mcp.server.fastmcp import Context, FastMCP  # type: ignore[import-not-found,import-untyped]
from pydantic import BaseModel, Field

from chorus.artifacts import ArtifactStore, artifact_stem
from chorus.audio import AudioRenderer
from chorus.curation import window_segments
from chorus.jobs import MASTER_OWNER, JobStore
from chorus.models import EpisodeInput, Job, Script, Transcript, Turn
from chorus.threads import content_words
from chorus.transcripts import TranscriptProvider

log = logging.getLogger("chorus.ask")

MAX_QUESTION_CHARS = 500
MAX_WINDOWS = 6
MIN_SHARED_WORDS = 1
MAX_SENTENCES = 6
MIN_QUOTE_WORDS = 4
ASK_KEY_CHARS = 12
REFUSAL = "nothing in this digest's episodes speaks to that"
UNGROUNDED = "no grounded answer survived the citation check"
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=MAX_QUESTION_CHARS)
    speak: bool = Field(default=False, description="Also voice the answer (slower).")


class Window(BaseModel):
    episode_id: str
    title: str | None = None
    start: float
    text: str
    starts: list[float] = Field(description="Segment start times, aligned with `pieces`.")
    pieces: list[str] = Field(description="Segment texts, whitespace-normalised.")


class AnswerCitation(BaseModel):
    episode_id: str
    segment_timestamp: float
    quote: str = Field(description="Verbatim transcript text supporting the sentence.")


class AnswerSentence(BaseModel):
    text: str
    citations: list[AnswerCitation]


class AskAnswer(BaseModel):
    question: str
    sentences: list[AnswerSentence] = Field(default_factory=list)
    refused: bool = False
    refusal_reason: str | None = None
    unavailable: list[str] = Field(
        default_factory=list, description="Episodes whose transcript could not be read again."
    )
    audio_url: str | None = Field(
        default=None, description="The voiced answer, when `speak` was asked and it rendered."
    )
    warnings: list[str] = Field(default_factory=list)


class DraftSentence(BaseModel):
    """What a writer returns before grounding: the sentence, the index of the
    retrieved window it rests on, and the words it quotes from that window."""

    text: str
    window: int
    quote: str


# --- Retrieval ---------------------------------------------------------------------


def _normalise(text: str) -> str:
    return " ".join(text.split())


def transcript_for(episode_id: str, provider: TranscriptProvider) -> Transcript | None:
    """The transcript the digest was curated from, if it can be read again."""
    cache = getattr(provider, "cache", None)
    if cache is not None:
        cached = cache.get(episode_id)
        if cached is not None:
            return cached  # type: ignore[no-any-return]
    if episode_id.startswith("rss-"):
        return None  # needs its feed to fetch again; only the cache has it
    try:
        return provider.get(EpisodeInput(video_id=episode_id))
    except Exception as err:  # noqa: BLE001 - reported as unavailable, never guessed
        log.info("ask: transcript for %s unavailable (%s)", episode_id, err)
        return None


def retrieve(
    question: str, transcripts: dict[str, Transcript], titles: dict[str, str | None]
) -> list[Window]:
    """The windows sharing the most content words with the question, best
    first (ties to the earlier window), at most MAX_WINDOWS."""
    asked = content_words(question)
    scored: list[tuple[int, str, float, Window]] = []
    for episode_id, transcript in transcripts.items():
        for w in window_segments(transcript.segments):
            shared = len(asked & content_words(w.text))
            if shared >= MIN_SHARED_WORDS:
                pieces = [(s.start, _normalise(s.text)) for s in w.segments if s.text.strip()]
                scored.append((shared, episode_id, w.start, Window(
                    episode_id=episode_id,
                    title=titles.get(episode_id),
                    start=w.start,
                    text=_normalise(w.text),
                    starts=[p[0] for p in pieces],
                    pieces=[p[1] for p in pieces],
                )))
    scored.sort(key=lambda t: (-t[0], t[1], t[2]))
    return [w for *_, w in scored[:MAX_WINDOWS]]


# --- Grounding ---------------------------------------------------------------------


def ground_quote(window: Window, quote: str) -> AnswerCitation | None:
    """A citation if `quote` occurs verbatim in the window, timestamped at
    the segment it starts in; else None."""
    needle = _normalise(quote.strip().strip("\"'“”"))
    if len(needle.split()) < MIN_QUOTE_WORDS:
        return None
    joined = " ".join(window.pieces)
    match = re.search(r"(?<!\S)" + re.escape(needle) + r"(?!\w)", joined)
    if match is None:
        return None
    offset = 0
    for start, piece in zip(window.starts, window.pieces, strict=True):
        offset += len(piece) + 1
        if match.start() < offset:
            return AnswerCitation(
                episode_id=window.episode_id, segment_timestamp=start, quote=needle
            )
    return None


def ground(drafts: list[DraftSentence], windows: list[Window]) -> list[AnswerSentence]:
    """Keep only sentences citing a retrieved window with a verbatim quote."""
    kept: list[AnswerSentence] = []
    for draft in drafts[:MAX_SENTENCES]:
        if not 0 <= draft.window < len(windows):
            continue
        citation = ground_quote(windows[draft.window], draft.quote)
        if citation is None or not draft.text.strip():
            continue
        kept.append(AnswerSentence(text=draft.text.strip(), citations=[citation]))
    return kept


# --- Writers -------------------------------------------------------------------------


@runtime_checkable
class AnswerWriter(Protocol):
    def write(self, question: str, windows: list[Window]) -> list[DraftSentence]: ...


class MockAnswerWriter:
    """Deterministic, offline: from each of the top two windows, the
    sentence sharing the most words with the question, quoted."""

    def write(self, question: str, windows: list[Window]) -> list[DraftSentence]:
        asked = content_words(question)
        drafts: list[DraftSentence] = []
        for index, window in enumerate(windows[:2]):
            sentences = [s for s in _SENTENCE_RE.split(window.text) if s.strip()]
            best = max(sentences, key=lambda s: len(asked & content_words(s)), default="")
            if best:
                source = window.title or window.episode_id
                drafts.append(DraftSentence(
                    text=f'{source} says: "{best}"', window=index, quote=best
                ))
        return drafts


ASK_SYSTEM = """You answer a principal's question about podcast episodes using ONLY the \
transcript windows provided. Each sentence of your answer must rest on one window: give that \
window's index and quote the exact words from it that support the sentence (at least four \
words, copied verbatim). If the windows do not answer the question, return no sentences; \
never answer from general knowledge.

Reply with ONLY a JSON object: {"sentences": [{"text": "...", "window": <index>, \
"quote": "..."}]}"""


class AnthropicAnswerWriter:
    MODEL = "claude-sonnet-4-6"
    MAX_TOKENS = 1200

    def __init__(self, api_key: str | None = None, client: Any | None = None) -> None:
        if client is None:
            import anthropic  # type: ignore[import-not-found]  # optional dep; only with a key

            client = anthropic.Anthropic(api_key=api_key)
        self._client: Any = client

    def write(self, question: str, windows: list[Window]) -> list[DraftSentence]:
        from chorus.script import json_object

        blocks = "\n\n".join(
            f"[{i}] {w.title or w.episode_id} at {int(w.start)}s:\n{w.text}"
            for i, w in enumerate(windows)
        )
        msg = self._client.messages.create(
            model=self.MODEL,
            max_tokens=self.MAX_TOKENS,
            system=ASK_SYSTEM,
            messages=[{"role": "user", "content": f"QUESTION: {question}\n\nWINDOWS:\n{blocks}"}],
        )
        body = "".join(b.text for b in msg.content if b.type == "text")
        drafts: list[DraftSentence] = []
        for raw in json_object(body).get("sentences", []):
            try:
                drafts.append(DraftSentence.model_validate(raw))
            except ValueError as err:
                log.info("ask: dropping unusable sentence (%s)", err)
        return drafts


def answer_writer_for(llm: object) -> Callable[[], AnswerWriter]:
    """Follow the deployment's scorer, as chorus.feedback.proposal_writer_for does."""
    import os

    from chorus.llm import MockLLMClient

    if isinstance(llm, MockLLMClient):
        return MockAnswerWriter
    key = os.environ.get("ANTHROPIC_API_KEY")
    return (lambda: AnthropicAnswerWriter(key)) if key else MockAnswerWriter


# --- The question ----------------------------------------------------------------------


def answer(
    question: str, job: Job, provider: TranscriptProvider, writer: AnswerWriter
) -> AskAnswer:
    """A grounded answer from the job's episodes, or an honest refusal."""
    question = _normalise(question)
    episodes = job.digest.episodes if job.digest else []
    titles = {e.episode_id: e.episode_title for e in episodes}
    transcripts: dict[str, Transcript] = {}
    unavailable: list[str] = []
    for episode in episodes:
        transcript = transcript_for(episode.episode_id, provider)
        if transcript is None:
            unavailable.append(episode.episode_id)
        else:
            transcripts[episode.episode_id] = transcript
    windows = retrieve(question, transcripts, titles)
    if not windows:
        return AskAnswer(
            question=question, refused=True, refusal_reason=REFUSAL, unavailable=unavailable
        )
    sentences = ground(writer.write(question, windows), windows)
    if not sentences:
        return AskAnswer(
            question=question, refused=True, refusal_reason=UNGROUNDED, unavailable=unavailable
        )
    return AskAnswer(question=question, sentences=sentences, unavailable=unavailable)


# --- Voicing -----------------------------------------------------------------------------

ANSWER_INTRO = "Here is what the episodes say."


def answer_script(result: AskAnswer, version: str) -> Script:
    """The answer as a monologue: each sentence, then the words it rests on."""
    lines = [ANSWER_INTRO]
    for sentence in result.sentences:
        lines.append(sentence.text)
        for citation in sentence.citations:
            lines.append(f"In their words: {citation.quote}")
    turns = [Turn(speaker="host", text=line) for line in lines]
    return Script(soul_version=version, takes=[], monologue="\n\n".join(lines), turns=turns)


def voice_answer(
    result: AskAnswer, job: Job, renderer: AudioRenderer, artifacts: ArtifactStore
) -> AskAnswer:
    """Render a grounded answer and store it beside the job's own audio.
    A refusal is not voiced; a render failure is a warning, never an error."""
    if result.refused:
        return result
    try:
        version = job.digest.soul_version if job.digest else ""
        rendered = renderer.render(answer_script(result, version), "", job.job_id)
        if rendered.placeholder:
            return result.model_copy(update={"warnings": ["no voice is configured; text only"]})
        key = hashlib.sha1(result.question.encode("utf-8")).hexdigest()[:ASK_KEY_CHARS]
        name = f"{artifact_stem(job.job_id)}__ask_{key}.{rendered.extension}"
        url = artifacts.put(name, rendered.data, rendered.media_type)
        return result.model_copy(update={"audio_url": url})
    except Exception as err:  # noqa: BLE001 - the text answer stands
        log.warning("ask: could not voice the answer (non-fatal): %s", err)
        return result.model_copy(update={"warnings": [f"could not voice the answer: {err}"]})


# --- HTTP and MCP ------------------------------------------------------------------------


def _owned_job(store: JobStore, owner: str, job_id: str) -> Job | None:
    job = store.get(job_id)
    if job is None or (owner != MASTER_OWNER and job.owner != owner) or job.digest is None:
        return None
    return job


def build_ask_router(
    store: JobStore,
    provider: TranscriptProvider,
    llm: object,
    renderer: AudioRenderer | None = None,
    artifacts: ArtifactStore | None = None,
) -> APIRouter:
    router = APIRouter()
    writer = answer_writer_for(llm)

    @router.post("/digest/{job_id}/ask")
    def ask(job_id: str, body: AskRequest, request: Request) -> dict[str, Any]:
        owner = getattr(request.state, "owner", MASTER_OWNER)
        job = _owned_job(store, owner, job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="unknown job, or no digest yet")
        result = answer(body.question, job, provider, writer())
        if body.speak and renderer is not None and artifacts is not None:
            result = voice_answer(result, job, renderer, artifacts)
        return result.model_dump(mode="json")

    return router


def register_ask_tools(
    server: FastMCP,
    store: JobStore,
    provider: TranscriptProvider,
    llm: object,
    renderer: AudioRenderer | None = None,
    artifacts: ArtifactStore | None = None,
) -> None:
    """ask_digest: a grounded answer from one digest's episodes."""
    from chorus.mcp_server import _owner_from_context

    writer = answer_writer_for(llm)

    def ask_digest(
        job_id: str, question: str, speak: bool = False, ctx: Context | None = None
    ) -> dict[str, Any]:
        """Answer a question using only the transcripts of this digest's
        episodes. Every sentence quotes the transcript at a timestamp; when
        the episodes do not speak to the question the answer is refused, not
        guessed. Relay the quotes and timestamps with the answer. With
        `speak`, the answer is also voiced and `audio_url` returned."""
        job = _owned_job(store, _owner_from_context(ctx), job_id)
        if job is None:
            return {"error": f"unknown job {job_id}, or no digest yet"}
        request = AskRequest(question=question, speak=speak)
        result = answer(request.question, job, provider, writer())
        if request.speak and renderer is not None and artifacts is not None:
            result = voice_answer(result, job, renderer, artifacts)
        return result.model_dump(mode="json")

    server.add_tool(ask_digest)
