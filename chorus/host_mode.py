"""The "host" brain: the principal's own agent does the thinking.

When the principal chose `brain = host`, Chorus calls no model. The agent
that installed it (Claude Code, Cowork, Codex, Grok Build, ...) supplies the
two judgment calls, and Chorus keeps everything that makes the output
trustworthy:

    Chorus                                  the host agent
    ------                                  --------------
    ingest transcripts, cut 90 s windows -> score each window against the soul
    threshold, quote, refuse honestly    <- (scores only; never quotes)
    hand over the surfaced highlights    -> write takes / dialogue turns,
    drop any beat that cites nothing     <-   each citing a highlight by ref
    render audio, finish the job

Grounding holds no matter which model scored, by construction:
- A highlight's quote is cut from the window text by `chorus.curation`, never
  supplied by the agent. The agent's scores pass through the same
  `curate_episode` code the Haiku path uses, via `_ProvidedScores`.
- A script beat names a highlight by index (`ref`), and Chorus fills in the
  episode and timestamp. A beat without a valid ref is dropped.

The agent drives the run with `next_task(job_id)`, which always says what to
do now: wait, score an episode, write the script, or deliver the result. The
instructions travel in that response (`SCORING_INSTRUCTIONS`,
`SCRIPT_INSTRUCTIONS`, tagged `RUBRIC_VERSION`), so they improve when Chorus
updates and every host gets the same ones.

Run state lives in `~/.chorus/runs/<job_id>/` beside the job row: the request,
the ingest result, and one file per scored episode. Separate files let
parallel subagents submit different episodes without contending on one
document.
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from chorus import paths
from chorus.artifacts import LocalArtifactStore, artifact_stem
from chorus.audio import MockAudioRenderer
from chorus.brains import BrainConfigError, build_provider, build_thinking, build_voice
from chorus.curation import RELEVANCE_THRESHOLD, curate_episode, soul_version, window_segments
from chorus.ingest import AllEpisodesFailed
from chorus.jobs import JobStore, SqliteJobStore
from chorus.llm import LLMError, Scored, TokenUsage, _parse_batch
from chorus.local_run import HIGHLIGHTS_PER_EPISODE, read_context
from chorus.models import (
    MONOLOGUE_PROFILE,
    TAKE_TYPES,
    TWO_HOST_PROFILE,
    Digest,
    DigestRequest,
    EpisodeDigest,
    EpisodeInput,
    EpisodeProfile,
    IngestResult,
    Job,
    JobStatus,
    JobUsage,
    Script,
    Take,
    Turn,
)
from chorus.onboarding import (
    Brain,
    OnboardingConfig,
    OnboardingError,
    Voice,
    load_config,
    status,
    voice_overrides,
)
from chorus.pipeline import PLACEHOLDER_AUDIO_WARNING, Deps, run_job, stage_audio, stage_ingest
from chorus.render_plan import AudioChunkError, RenderPlan, build_plan, join_mp3, read_chunk
from chorus.script import _max_turns, _speaker_persona, _turns_transcript
from chorus.soul import load_soul

log = logging.getLogger("chorus.host_mode")

HOST_PROTOCOL = 1
RUBRIC_VERSION = "host-1"
RUNS_DIRNAME = "runs"
WAIT_SECONDS = 3
SHARING_RETRIES = 20
SHARING_RETRY_SECONDS = 0.01
EMPTY_EPISODE_TEXT = "Nothing cleared the bar this week."

Phase = Literal["ingesting", "thinking", "scoring", "scripting", "rendering", "done", "failed"]
TaskKind = Literal["wait", "score", "script", "render", "done", "failed"]

SCORING_INSTRUCTIONS = f"""\
Score how strongly each transcript window below matches the principal's lens.

- The lens is the soul. Honor its Curation Guidance above everything else, and use
  the principal context to judge what matters this week.
- Be strict. 1.0 means this window is exactly what the principal wants surfaced;
  0.0 means irrelevant. Windows below {RELEVANCE_THRESHOLD} are dropped.
- If nothing in the episode clears the bar, Chorus tells the principal so. That
  refusal is correct and valuable: never raise scores to avoid it.
- Score every window index exactly once.
- `reason` is one line naming the specific claim, number or mechanism that earned
  the score. The principal sees it as "why this was surfaced".
- Do not quote the transcript back. Chorus cuts the quote from the window itself,
  so every highlight stays verifiable.

Submit with host_submit_scores (shell: `chorus setup host-scores`) as
  {{"scores": [{{"i": 0, "score": 0.0, "reason": "..."}}, ...]}}

If you can run subagents, give each pending episode to its own subagent along with
the lens; fetch an episode's windows with host_episode(job_id, episode_id).
"""

SCRIPT_INSTRUCTIONS = """\
Write a short, opinionated podcast script in the principal's lens.

- Take a position, push back, and connect episodes. A thought partner with a point
  of view, not a summary.
- EVERY beat must be grounded in one of the highlights below and cite it by `ref`.
  A beat with a missing or invalid ref is dropped. Do not introduce claims the
  highlights don't support.
- take_type is one of: {take_types}.
{format_rules}
Submit with host_submit_script (shell: `chorus setup host-script`) as
  {schema}
"""

RENDER_INSTRUCTIONS = """\
Voice this episode with your own text-to-speech tool: for example the ElevenLabs MCP
server's text_to_speech, or an ElevenLabs connector or plugin.

- Voice every chunk in render_plan.chunks, in index order, with that chunk's voice_id
  and the plan's model_id. Ask for MP3 output (output_format {output_format}).
- If your tool takes an output directory, use {staging}
- Then call host_submit_audio (shell: `chorus setup host-audio`) with every chunk:
  [{{"index": 0, "path": "<the file your tool saved>"}}, ...]
  Use "base64" instead of "path" if your tool hands back audio bytes.
- If you have no text-to-speech tool, or it fails, call host_submit_audio with a
  skip_reason. The digest is still delivered, with the script as text.
{dialogue_note}"""
_DIALOGUE_RENDER_NOTE = (
    "- This is a two-host episode voiced turn by turn with alternating voices. It sounds "
    "less natural than a native dialogue model; mention that if the principal asks.\n"
)

_MONOLOGUE_RULES = "- Single voice: write takes only, in the order they should be spoken.\n"
_DIALOGUE_RULES = """\
- Two hosts. First write the takes (the beats), then turn them into dialogue: the host
  raises each beat in their own voice, the cohost reacts in theirs (push back, ask for
  the number, agree when it's warranted), honoring the personas and style below.
- Every turn also cites a highlight by `ref`. At most {max_turns} turns.
"""
_MONOLOGUE_SCHEMA = '{"takes": [{"ref": 0, "take_type": "idea", "text": "..."}]}'
_DIALOGUE_SCHEMA = (
    '{"takes": [{"ref": 0, "take_type": "idea", "text": "..."}], '
    '"turns": [{"ref": 0, "speaker": "host", "text": "..."}]}'
)


class HostModeError(ValueError):
    """A host-brain call that cannot be applied; the message says what to fix."""


class HostRunState(BaseModel):
    job_id: str
    phase: Phase
    request: DigestRequest
    episode_ids: list[str] = Field(default_factory=list)
    brain_model: str | None = None
    rubric_version: str = RUBRIC_VERSION
    error: str | None = None


class HostTask(BaseModel):
    protocol: int = HOST_PROTOCOL
    rubric_version: str = RUBRIC_VERSION
    job_id: str
    kind: TaskKind
    instructions: str
    wait_seconds: int | None = None
    lens: dict[str, str] | None = None
    episode: dict[str, Any] | None = None
    pending_episodes: list[str] = Field(default_factory=list)
    highlights: list[dict[str, Any]] | None = None
    episode_format: str | None = None
    speakers: list[dict[str, str]] | None = None
    style: dict[str, Any] | None = None
    max_turns: int | None = None
    render_plan: dict[str, Any] | None = None
    result: dict[str, Any] | None = None
    error: str | None = None


class _ProvidedScores:
    """An `LLMClient` whose 'model' is the host agent: it returns the scores
    the agent submitted, so curation runs through the one code path."""

    def __init__(self, scores: list[Scored]) -> None:
        self._scores = scores

    def score_segment(self, text: str, soul: str, context: str) -> Scored:
        raise HostModeError("host scores are submitted per episode, not per segment")

    def score_windows(
        self, windows: list[str], soul: str, context: str, meter: TokenUsage | None = None
    ) -> list[Scored]:
        if len(windows) != len(self._scores):
            raise HostModeError(f"{len(self._scores)} scores for {len(windows)} windows")
        return self._scores


# --- storage ---------------------------------------------------------------

_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def _lock(job_id: str) -> threading.Lock:
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(job_id, threading.Lock())


def run_dir(job_id: str) -> Path:
    if not job_id.isalnum():
        raise HostModeError(f"invalid job_id {job_id!r}")
    return paths.home() / RUNS_DIRNAME / job_id


def _episode_key(episode_id: str) -> str:
    return hashlib.sha1(episode_id.encode("utf-8")).hexdigest()[:16]


def _retry_sharing[T](action: Callable[[], T]) -> T:
    """Retry briefly on Windows sharing violations. `os.replace` onto a file
    another thread has open for reading (an agent polling host_next while the
    background ingest saves state) fails with PermissionError on Windows,
    where POSIX would just swap the inode. The window is microseconds wide."""
    for attempt in range(SHARING_RETRIES):
        try:
            return action()
        except PermissionError:
            if attempt == SHARING_RETRIES - 1:
                raise
            time.sleep(SHARING_RETRY_SECONDS * (attempt + 1))
    raise AssertionError("unreachable")


def _write(path: Path, model: BaseModel) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # A unique temp name per write: two writers never share one temp file.
    tmp = path.with_name(f"{path.stem}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(model.model_dump_json(), encoding="utf-8")
    try:
        _retry_sharing(lambda: tmp.replace(path))
    finally:
        tmp.unlink(missing_ok=True)


def _read(path: Path) -> str:
    return _retry_sharing(lambda: path.read_text(encoding="utf-8"))


def _load_state(job_id: str) -> HostRunState:
    target = run_dir(job_id) / "state.json"
    if not target.is_file():
        raise HostModeError(f"no host-brain run {job_id}")
    return HostRunState.model_validate_json(_read(target))


def _save_state(state: HostRunState) -> None:
    _write(run_dir(state.job_id) / "state.json", state)


def _load_ingest(job_id: str) -> IngestResult:
    return IngestResult.model_validate_json(
        _read(run_dir(job_id) / "ingest.json")
    )


def _score_path(job_id: str, episode_id: str) -> Path:
    return run_dir(job_id) / "scores" / f"{_episode_key(episode_id)}.json"


def _scored(job_id: str, episode_id: str) -> EpisodeDigest | None:
    target = _score_path(job_id, episode_id)
    if not target.is_file():
        return None
    return EpisodeDigest.model_validate_json(_read(target))


def _job(store: JobStore, job_id: str) -> Job:
    job = store.get(job_id)
    if job is None:
        raise HostModeError(f"unknown job_id {job_id}")
    return job


def _fail(store: JobStore, state: HostRunState, error: str) -> None:
    state.phase, state.error = "failed", error
    _save_state(state)
    job = _job(store, state.job_id)
    job.status, job.error = JobStatus.failed, error
    store.save(job)


# --- start -----------------------------------------------------------------


def build_request(
    config: OnboardingConfig, episodes: list[EpisodeInput], episode_format: str
) -> DigestRequest:
    if episode_format not in {"monologue", "dialogue"}:
        raise HostModeError("format must be 'monologue' or 'dialogue'")
    return DigestRequest(
        soul=load_soul(config.soul or ""),
        context=read_context(),
        episodes=episodes,
        highlight_count=HIGHLIGHTS_PER_EPISODE,
        profile=TWO_HOST_PROFILE if episode_format == "dialogue" else None,
    )


def start(
    store: JobStore,
    config: OnboardingConfig,
    episodes: list[EpisodeInput],
    *,
    episode_format: str = "monologue",
    brain_model: str | None = None,
    require_ready: bool = True,
    background: bool = True,
) -> str:
    """Create the job and ingest its transcripts. `background=True` (MCP)
    returns at once and the agent polls `next_task`; the CLI ingests inline
    because its process exits after the command."""
    agent_brain = config.brain is Brain.host
    if not agent_brain and config.voice is not Voice.host_plugin:
        raise HostModeError(
            "neither the brain nor the voice is your agent in this setup; use run_my_digest"
        )
    if require_ready and not status(config).ready:
        raise OnboardingError("setup is incomplete; call onboarding_status")
    if not episodes:
        raise HostModeError("no episodes to digest")
    request = build_request(config, episodes, episode_format)
    job_id = store.create()
    state = HostRunState(
        job_id=job_id,
        phase="ingesting" if agent_brain else "thinking",
        request=request,
        brain_model=brain_model if agent_brain else None,
    )
    _save_state(state)
    job = _job(store, job_id)
    job.usage = (
        JobUsage(brain="host", brain_model=brain_model, rubric_version=RUBRIC_VERSION)
        if agent_brain
        else JobUsage(brain=config.brain.value if config.brain else None)
    )
    store.save(job)

    def think_work(store: JobStore) -> None:
        """Chorus's own brain curates and scripts; the agent voices it."""
        try:
            llm, composer = build_thinking(config)
        except BrainConfigError as err:
            _fail(store, state, str(err))
            return
        deps = Deps(
            build_provider(config),
            llm,
            composer,
            MockAudioRenderer(),  # never called: the run stops before audio
            LocalArtifactStore(paths.artifacts_dir()),
        )
        try:
            run_job(job_id, request, store, deps, stop_before_audio=True)
        finally:
            deps.close()
        finished = _job(store, job_id)
        if finished.status is JobStatus.failed:
            state.phase, state.error = "failed", finished.error
        elif finished.status is JobStatus.done:
            state.phase = "done"  # script synthesis failed; the digest stands without audio
        else:
            state.phase = "rendering"
        _save_state(state)

    def ingest_work(store: JobStore) -> None:
        provider = build_provider(config)
        started = time.perf_counter()
        try:
            ingested = stage_ingest(request, provider)
        except AllEpisodesFailed as err:
            _fail(store, state, str(err))
            return
        except Exception as err:  # noqa: BLE001 - any ingest failure ends the run explicitly
            log.exception("host run %s: ingest failed", job_id)
            _fail(store, state, f"{type(err).__name__}: {err}")
            return
        finally:
            close = getattr(provider, "close", None)
            if callable(close):
                close()
        _write(run_dir(job_id) / "ingest.json", ingested)
        current = _job(store, job_id)
        usage = current.usage or JobUsage()
        usage.stage_seconds["ingest"] = time.perf_counter() - started
        usage.skipped = ingested.skipped
        usage.transcript_sources = {
            r.episode.resolved_id(): r.transcript.source or "unknown" for r in ingested.resolved
        }
        current.usage = usage
        store.save(current)
        state.episode_ids = [r.transcript.video_id for r in ingested.resolved]
        state.phase = "scoring"
        _save_state(state)

    work = ingest_work if agent_brain else think_work
    if not background:
        work(store)
        return job_id

    # The caller may close its store as soon as this returns (the smoke test
    # does), so the background ingest gets its own connection to the same
    # database. Host runs are local-only, so that database is SQLite.
    db_path = Path(getattr(store, "db_path", paths.db_path()))

    def ingest_in_background() -> None:
        own = SqliteJobStore(db_path)
        try:
            work(own)
        except Exception as err:  # noqa: BLE001 - a dead thread must never strand the run
            log.exception("host run %s: background work failed", job_id)
            try:
                _fail(own, state, f"{type(err).__name__}: {err}")
            except Exception:  # noqa: BLE001 - nothing more can be recorded
                log.exception("host run %s: could not record the failure", job_id)
        finally:
            own.close()

    threading.Thread(
        target=ingest_in_background, name=f"chorus-host-ingest-{job_id}", daemon=True
    ).start()
    return job_id


# --- tasks -----------------------------------------------------------------


def _lens(request: DigestRequest) -> dict[str, str]:
    return {"soul": request.soul, "context": request.context}


def episode_windows(job_id: str, episode_id: str) -> dict[str, Any]:
    """One episode's windows, for a subagent scoring it in parallel."""
    state = _load_state(job_id)
    if state.phase in {"ingesting", "failed"}:
        raise HostModeError(f"run {job_id} is {state.phase}; nothing to score")
    for resolved in _load_ingest(job_id).resolved:
        if resolved.transcript.video_id == episode_id:
            spans = window_segments(resolved.transcript.segments)
            return {
                "episode_id": episode_id,
                "title": resolved.episode.title,
                "show": resolved.episode.show,
                "window_count": len(spans),
                "windows": [
                    {"i": i, "start_seconds": w.start, "text": w.text} for i, w in enumerate(spans)
                ],
                "already_scored": _scored(job_id, episode_id) is not None,
            }
    raise HostModeError(f"episode {episode_id} is not part of run {job_id}")


def _profile(request: DigestRequest) -> EpisodeProfile:
    return request.profile or MONOLOGUE_PROFILE


def _highlight_refs(digest: Digest) -> list[dict[str, Any]]:
    return [
        {
            "ref": i,
            "episode_id": h.episode_id,
            "episode_title": h.episode_title,
            "at_seconds": h.segment_timestamp,
            "quote": h.quote,
            "why": h.why_surface,
            "score": h.relevance_score,
        }
        for i, h in enumerate(digest.highlights)
    ]


def _script_task(state: HostRunState, digest: Digest) -> HostTask:
    profile = _profile(state.request)
    dialogue = profile.format == "dialogue"
    max_turns = _max_turns(profile.style.target_minutes) if dialogue else None
    rules = _DIALOGUE_RULES.format(max_turns=max_turns) if dialogue else _MONOLOGUE_RULES
    instructions = SCRIPT_INSTRUCTIONS.format(
        take_types=", ".join(TAKE_TYPES),
        format_rules=rules,
        schema=_DIALOGUE_SCHEMA if dialogue else _MONOLOGUE_SCHEMA,
    )
    speakers = [
        {
            "role": s.role,
            "name": s.name,
            "persona": _speaker_persona(s, state.request.soul),
        }
        for s in profile.speakers
    ]
    return HostTask(
        job_id=state.job_id,
        kind="script",
        instructions=instructions,
        lens=_lens(state.request),
        highlights=_highlight_refs(digest),
        episode_format=profile.format,
        speakers=speakers,
        style=profile.style.model_dump(),
        max_turns=max_turns,
    )


def next_task(store: JobStore, job_id: str) -> HostTask:
    state = _load_state(job_id)
    if state.phase == "ingesting":
        return HostTask(
            job_id=job_id,
            kind="wait",
            instructions="Chorus is fetching transcripts. Call host_next again shortly.",
            wait_seconds=WAIT_SECONDS,
        )
    if state.phase == "thinking":
        return HostTask(
            job_id=job_id,
            kind="wait",
            instructions="Chorus is curating and writing the script. Call host_next again "
            "shortly.",
            wait_seconds=WAIT_SECONDS,
        )
    if state.phase == "rendering":
        return _render_task(store, state)
    if state.phase == "failed":
        return HostTask(
            job_id=job_id,
            kind="failed",
            instructions="Tell the principal the run failed and why.",
            error=state.error,
        )
    if state.phase == "done":
        from chorus.agent_setup import job_summary

        return HostTask(
            job_id=job_id,
            kind="done",
            instructions="Deliver each highlight with its timestamp and quote, any refused "
            "episodes as refused (do not pad them), and the episode file.",
            result=job_summary(_job(store, job_id)),
        )
    if state.phase == "scripting":
        return _script_task(state, _job_digest(store, job_id))
    pending = [e for e in state.episode_ids if _scored(job_id, e) is None]
    if not pending:
        return _finalize_scoring(store, state)
    return HostTask(
        job_id=job_id,
        kind="score",
        instructions=SCORING_INSTRUCTIONS,
        lens=_lens(state.request),
        episode=episode_windows(job_id, pending[0]),
        pending_episodes=pending,
    )


def _job_digest(store: JobStore, job_id: str) -> Digest:
    digest = _job(store, job_id).digest
    if digest is None:
        raise HostModeError(f"run {job_id} has no digest yet")
    return digest


# --- submissions -----------------------------------------------------------


def _parse_scores(scores: list[dict[str, Any]], window_count: int) -> list[Scored]:
    """Same tolerance as the Haiku path: clamp to [0, 1], reject non-finite,
    treat a few missing indices as 0.0, and refuse when most are missing."""
    try:
        return _parse_batch(json.dumps(scores), window_count)
    except (LLMError, TypeError, ValueError) as err:
        raise HostModeError(f"scores rejected: {err}") from err


def submit_scores(
    store: JobStore, job_id: str, episode_id: str, scores: list[dict[str, Any]]
) -> dict[str, Any]:
    with _lock(job_id):
        state = _load_state(job_id)
        if state.phase != "scoring":
            raise HostModeError(f"run {job_id} is {state.phase}, not accepting scores")
        resolved = next(
            (r for r in _load_ingest(job_id).resolved if r.transcript.video_id == episode_id),
            None,
        )
        if resolved is None:
            raise HostModeError(f"episode {episode_id} is not part of run {job_id}")
        window_count = len(window_segments(resolved.transcript.segments))
        parsed = _parse_scores(scores, window_count)
        result = curate_episode(
            resolved,
            state.request.soul,
            state.request.context,
            _ProvidedScores(parsed),
            max_highlights=state.request.highlight_count,
        )
        _write(_score_path(job_id, episode_id), result)
    outcome = {
        "episode_id": episode_id,
        "refused": result.refused,
        "refusal_reason": result.refusal_reason,
        "highlights": len(result.highlights),
    }
    return {"episode": outcome, "next": next_task(store, job_id).model_dump(mode="json")}


def _finalize_scoring(store: JobStore, state: HostRunState) -> HostTask:
    with _lock(state.job_id):
        state = _load_state(state.job_id)
        if state.phase != "scoring":
            return next_task(store, state.job_id)
        episodes = [_scored(state.job_id, e) for e in state.episode_ids]
        digest = Digest(
            soul_version=soul_version(state.request.soul),
            soul_origin=state.request.soul_origin,
            episodes=[e for e in episodes if e is not None],
        )
        job = _job(store, state.job_id)
        job.digest = digest
        job.status = JobStatus.digest_ready
        store.save(job)
        state.phase = "scripting"
        _save_state(state)
    if not digest.highlights:
        # Every episode was refused: nothing to write about, which is the
        # honest outcome. Voice the one-line episode and finish.
        script = Script(
            soul_version=digest.soul_version,
            takes=[],
            monologue=EMPTY_EPISODE_TEXT,
            format=_profile(state.request).format,
        )
        _complete(store, state, script, [])
        return next_task(store, state.job_id)
    return _script_task(state, digest)


def _grounded(
    items: list[dict[str, Any]], digest: Digest, build: Callable[[dict[str, Any], Any], Any]
) -> tuple[list[Any], list[str]]:
    highlights = digest.highlights
    kept: list[Any] = []
    dropped: list[str] = []
    for n, item in enumerate(items):
        ref = item.get("ref")
        text = str(item.get("text") or "").strip()
        if not isinstance(ref, int) or isinstance(ref, bool) or not 0 <= ref < len(highlights):
            dropped.append(f"#{n}: ref {ref!r} is not a highlight (0..{len(highlights) - 1})")
            continue
        if not text:
            dropped.append(f"#{n}: empty text")
            continue
        try:
            kept.append(build(item, highlights[ref]))
        except (ValueError, TypeError) as err:
            dropped.append(f"#{n}: {err}")
    return kept, dropped


def _take(item: dict[str, Any], highlight: Any) -> Take:
    take_type = str(item.get("take_type") or "")
    if take_type not in TAKE_TYPES:
        raise ValueError(f"take_type {take_type!r} is not one of {', '.join(TAKE_TYPES)}")
    return Take(
        text=str(item["text"]).strip(),
        take_type=take_type,
        episode_id=highlight.episode_id,
        segment_timestamp=highlight.segment_timestamp,
    )


def _turn(item: dict[str, Any], highlight: Any) -> Turn:
    speaker = item.get("speaker")
    if speaker not in {"host", "cohost"}:
        raise ValueError(f"speaker {speaker!r} must be 'host' or 'cohost'")
    return Turn(
        speaker=speaker,
        text=str(item["text"]).strip(),
        episode_id=highlight.episode_id,
        segment_timestamp=highlight.segment_timestamp,
    )


def submit_script(
    store: JobStore,
    config: OnboardingConfig,
    job_id: str,
    takes: list[dict[str, Any]],
    turns: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    with _lock(job_id):
        state = _load_state(job_id)
        if state.phase != "scripting":
            raise HostModeError(f"run {job_id} is {state.phase}, not accepting a script")
        digest = _job_digest(store, job_id)
        profile = _profile(state.request)
        kept_takes, dropped = _grounded(takes, digest, _take)
        if not kept_takes:
            raise HostModeError(
                "no grounded takes: every take needs a valid highlight ref, a take_type and "
                "text. Problems: " + "; ".join(dropped)
            )
        kept_turns: list[Turn] = []
        if profile.format == "dialogue":
            kept_turns, dropped_turns = _grounded(turns or [], digest, _turn)
            dropped += [f"turn {d}" for d in dropped_turns]
            max_turns = _max_turns(profile.style.target_minutes)
            if len(kept_turns) > max_turns:
                dropped.append(f"{len(kept_turns) - max_turns} turn(s) over the {max_turns} cap")
                kept_turns = kept_turns[:max_turns]
            if not kept_turns:
                raise HostModeError(
                    "no grounded turns for this two-host episode. Problems: " + "; ".join(dropped)
                )
        script = Script(
            soul_version=digest.soul_version,
            takes=kept_takes,
            monologue=(
                _turns_transcript(kept_turns)
                if profile.format == "dialogue"
                else "\n\n".join(t.text for t in kept_takes)
            ),
            turns=kept_turns,
            format=profile.format,
            voices={s.role: s.voice_id for s in profile.speakers},
        )
        _complete(store, state, script, dropped, config)
    return {"dropped": dropped, "next": next_task(store, job_id).model_dump(mode="json")}


def _complete(
    store: JobStore,
    state: HostRunState,
    script: Script,
    dropped: list[str],
    config: OnboardingConfig | None = None,
) -> None:
    """Render the audio with the principal's chosen voice and finish the job.
    Audio failure degrades (warning), exactly as in the in-process pipeline."""
    from chorus.onboarding import load_config

    principal = config or load_config()
    job = _job(store, state.job_id)
    job.script = script
    job.warnings += [f"dropped ungrounded script item {d}" for d in dropped]
    if principal.voice is Voice.host_plugin:
        job.status = JobStatus.digest_ready
        store.save(job)
        state.phase = "rendering"
        _save_state(state)
        return
    try:
        renderer = build_voice(principal)
        audio = stage_audio(
            script,
            state.request,
            state.job_id,
            renderer,
            LocalArtifactStore(paths.artifacts_dir()),
        )
        job.audio_url = audio.url
        if audio.placeholder:
            job.warnings.append(PLACEHOLDER_AUDIO_WARNING)
    except Exception as err:  # noqa: BLE001 - audio failure is non-fatal
        job.audio_url = None
        job.warnings.append(f"audio render failed: {type(err).__name__}: {err}")
    job.status = JobStatus.done
    store.save(job)
    state.phase = "done"
    _save_state(state)


# --- agent-rendered audio ("host-plugin" voice) ----------------------------


def principal_voices() -> dict[str, str]:
    """The voices chosen in onboarding, else ELEVENLABS_*VOICE_ID, per role."""
    return voice_overrides(load_config())


def _plan_for(store: JobStore, job_id: str) -> RenderPlan:
    script = _job(store, job_id).script
    if script is None:
        raise HostModeError(f"run {job_id} has no script to voice")
    return build_plan(script, principal_voices())


def _render_task(store: JobStore, state: HostRunState) -> HostTask:
    plan = _plan_for(store, state.job_id)
    staging = run_dir(state.job_id) / "audio"
    staging.mkdir(parents=True, exist_ok=True)
    return HostTask(
        job_id=state.job_id,
        kind="render",
        instructions=RENDER_INSTRUCTIONS.format(
            output_format=plan.output_format,
            staging=staging,
            dialogue_note=_DIALOGUE_RENDER_NOTE if plan.format == "dialogue" else "",
        ),
        render_plan=plan.model_dump(),
    )


def submit_audio(
    store: JobStore,
    job_id: str,
    chunks: list[dict[str, Any]] | None = None,
    skip_reason: str | None = None,
) -> dict[str, Any]:
    """Join the agent's voiced chunks into the episode, or record why it could
    not voice them. Either way the run finishes and the digest is delivered."""
    with _lock(job_id):
        state = _load_state(job_id)
        if state.phase != "rendering":
            raise HostModeError(f"run {job_id} is {state.phase}, not waiting for audio")
        job = _job(store, job_id)
        artifacts = LocalArtifactStore(paths.artifacts_dir())
        stem = artifact_stem(job_id)
        if skip_reason is not None:
            text = (job.script.monologue if job.script else EMPTY_EPISODE_TEXT).encode("utf-8")
            job.audio_url = artifacts.put(f"{stem}.txt", text, "text/plain")
            reason = skip_reason.strip() or "no reason given"
            job.warnings.append(f"audio skipped by the agent: {reason}")
        else:
            plan = _plan_for(store, job_id)
            try:
                data = join_mp3(_ordered_chunks(chunks or [], len(plan.chunks)))
            except AudioChunkError as err:
                raise HostModeError(str(err)) from err
            job.audio_url = artifacts.put(f"{stem}.mp3", data, "audio/mpeg")
        job.status = JobStatus.done
        store.save(job)
        state.phase = "done"
        _save_state(state)
    return {"next": next_task(store, job_id).model_dump(mode="json")}


def _ordered_chunks(chunks: list[dict[str, Any]], expected: int) -> list[bytes]:
    by_index: dict[int, bytes] = {}
    for item in chunks:
        index = item.get("index")
        if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < expected:
            raise HostModeError(f"chunk index {index!r} is not in the plan (0..{expected - 1})")
        if index in by_index:
            raise HostModeError(f"chunk {index} was submitted twice")
        try:
            by_index[index] = read_chunk(item.get("path"), item.get("base64"))
        except AudioChunkError as err:
            raise HostModeError(f"chunk {index}: {err}") from err
    missing = [i for i in range(expected) if i not in by_index]
    if missing:
        raise HostModeError(f"missing chunks {missing}; voice every chunk in the plan")
    return [by_index[i] for i in range(expected)]
