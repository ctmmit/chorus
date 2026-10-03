"""Job orchestration: ingest -> curate -> script -> audio, with status updates.

Lifecycle (Q3): queued -> digest_ready (digest set) -> done | failed.
Failure semantics from the failure-mode table:
- all transcripts fail        -> status=failed with a reason (never empty done)
- anything fails before the
  digest exists               -> status=failed with a reason (never stuck)
- script synthesis fails      -> status=done, script=None, audio skipped, warning
- audio render fails          -> status=done, audio_url=None, warning (non-fatal)

Invariant: run_job always leaves the job in a terminal state. A background
task that raises would otherwise strand the job at queued/digest_ready and a
caller following SKILL.md would poll forever.

Phase B (docs/DEVELOPMENT_PLAN.md §5): the work below is four stage functions
(`stage_ingest`, `stage_curate_episode`, `stage_script`, `stage_audio`) that
take/return JSON-serializable (Pydantic) values and hold no state of their
own. `run_job`/`_run` remain the in-process orchestrator (FastAPI
BackgroundTasks via chorus.runners.BackgroundRunner) that calls them in
sequence; chorus/inngest_app.py's `run_digest` calls the SAME stage functions
one per Inngest `step.run`, so the two execution modes can never drift apart.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from pydantic import BaseModel

from chorus import catalog
from chorus.artifacts import ArtifactStore, LocalArtifactStore, artifact_stem
from chorus.audio import AudioRenderer, get_audio_renderer
from chorus.curation import curate_episode, soul_version
from chorus.errors import is_retryable
from chorus.ingest import AllEpisodesFailed, ingest
from chorus.jobs import JobStore
from chorus.llm import LLMClient, TokenUsage, get_llm_client
from chorus.models import (
    MONOLOGUE_PROFILE,
    CurateResult,
    Digest,
    DigestRequest,
    IngestResult,
    Job,
    JobStatus,
    JobUsage,
    LLMTokens,
    ResolvedEpisode,
    Script,
)
from chorus.script import ScriptComposer, get_script_composer
from chorus.transcript_cache import SqliteTranscriptCache
from chorus.transcripts import TranscriptProvider

log = logging.getLogger("chorus.pipeline")

TRANSCRIPT_API_KEY_ENV = "TRANSCRIPT_API_KEY"  # Supadata native YouTube captions
DEEPGRAM_API_KEY_ENV = "DEEPGRAM_API_KEY"
ASSEMBLYAI_API_KEY_ENV = "ASSEMBLYAI_API_KEY"


@dataclass
class Deps:
    provider: TranscriptProvider
    llm: LLMClient
    composer: ScriptComposer
    renderer: AudioRenderer
    artifacts: ArtifactStore = field(default_factory=LocalArtifactStore)

    def close(self) -> None:
        """R24 (docs/REVIEW_WAVE1.md #24): close every owned provider/cache/
        store that exposes a `close()` — most importantly `provider.cache`
        (chorus.transcript_cache.CachingTranscriptProvider's wrapped
        TranscriptCache), which in Postgres mode is
        chorus.stores.postgres.PostgresTranscriptCache and owns its own
        connection pool. Previously nothing in chorus.app's lifespan ever
        called this, so repeated local lifespan cycles (or graceful worker
        recycling) leaked pool connections until process teardown. Anything
        without a `close` attribute (MockLLMClient, LocalArtifactStore, ...)
        is silently skipped — this is deliberately duck-typed rather than a
        fixed list, so a future Deps field with its own `close()` is picked
        up automatically."""
        seen: set[int] = set()
        for candidate in (
            self.provider,
            getattr(self.provider, "cache", None),
            self.llm,
            self.composer,
            self.renderer,
            self.artifacts,
        ):
            if candidate is None or id(candidate) in seen:
                continue
            seen.add(id(candidate))
            close = getattr(candidate, "close", None)
            if callable(close):
                close()


def default_deps() -> Deps:
    """Build the transcript provider ladder from the environment (fixtures,
    publisher RSS transcript, AssemblyAI, Deepgram, Supadata native captions;
    order and rationale in `chorus.config_env.build_transcript_chain`, the
    single shared builder), wrapped in a SQLite-backed cache so a repeat
    request for the same episode never re-hits a paid API.

    This is the local/dev/test builder (SQLite cache, local artifact
    directory). chorus.config_env.build_deps builds the Vercel-environment
    equivalent (Postgres cache, Vercel Blob) that chorus.app.create_app uses by
    default; default_deps() stays SQLite/local so `python -m chorus.app` and
    every test keep working with zero configuration.
    """
    from chorus.config_env import build_transcript_chain

    return Deps(
        provider=build_transcript_chain(SqliteTranscriptCache()),
        llm=get_llm_client(),
        composer=get_script_composer(),
        renderer=get_audio_renderer(),
        artifacts=LocalArtifactStore(),
    )


def sum_llm_tokens(tokens: list[LLMTokens]) -> LLMTokens | None:
    """R14: each `CurateResult.tokens` is already isolated to one episode's
    calls (a fresh TokenUsage() per stage_curate_episode call, not a
    snapshot/delta of a client-level counter shared across concurrent jobs —
    those helpers are gone). Summing them here is a plain per-job total, safe
    under concurrency. None (not an all-zero LLMTokens) when nothing made any
    metered calls, matching a mock/unmetered client's previous behavior."""
    if not tokens or all(t.calls == 0 for t in tokens):
        return None
    total = dict.fromkeys(LLMTokens.model_fields, 0)
    for t in tokens:
        for key in total:
            total[key] += getattr(t, key)
    return LLMTokens(**total)


# --- Stage functions ------------------------------------------------------
#
# Each stage is JSON-in/JSON-out (Pydantic models or their dumps) and holds no
# state beyond its arguments, so it can run equally as a plain function call
# (BackgroundRunner / run_job below) or as the body of an Inngest step.run
# (chorus/inngest_app.py) — retried and memoized independently of the others.


def stage_ingest(request: DigestRequest, provider: TranscriptProvider) -> IngestResult:
    """Resolve every requested episode to a transcript. Raises
    AllEpisodesFailed if none resolved (never an empty digest). Bare ids that
    are in the catalog pick up their show/title first."""
    return ingest(catalog.enrich(request.episodes), provider)


def stage_curate_episode(
    resolved: ResolvedEpisode, request: DigestRequest, llm: LLMClient
) -> CurateResult:
    """Score one resolved episode against the soul/context and surface its
    highlights (or an honest refusal). One Inngest step per episode
    (`curate:<episode_id>`) so a single episode's failure retries alone.

    Returns the digest AND this call's isolated token spend (R14: a fresh
    TokenUsage() meter per episode, not a snapshot/delta of a client-level
    counter that concurrent jobs would otherwise corrupt each other's numbers
    through)."""
    meter = TokenUsage()
    digest = curate_episode(
        resolved,
        request.soul,
        request.context,
        llm,
        max_highlights=request.highlight_count,
        meter=meter,
    )
    tokens = LLMTokens(
        calls=meter.calls,
        input_tokens=meter.input_tokens,
        output_tokens=meter.output_tokens,
        cache_read_tokens=meter.cache_read_tokens,
        cache_write_tokens=meter.cache_write_tokens,
    )
    return CurateResult(digest=digest, tokens=tokens)


def stage_script(digest: Digest, request: DigestRequest, composer: ScriptComposer) -> Script:
    """Compose the opinionated script from the finished digest. Callers treat
    a raised exception as non-fatal (digest still stands; script degrades).
    `request.profile` (None -> MONOLOGUE_PROFILE, today's single-voice
    behavior) picks monologue vs. two-host dialogue (docs/DEVELOPMENT_PLAN.md
    §4)."""
    profile = request.profile or MONOLOGUE_PROFILE
    return composer.write_script(digest, request.soul, request.context, profile)


PLACEHOLDER_AUDIO_WARNING = (
    "audio is a text placeholder (no TTS key configured); audio_url holds the script text"
)


class AudioResult(BaseModel):
    """What stage_audio hands back: where the artifact lives, and whether it
    is real audio or the offline mock's text placeholder."""

    url: str
    placeholder: bool = False


def stage_audio(
    script: Script,
    request: DigestRequest,
    job_id: str,
    renderer: AudioRenderer,
    artifacts: ArtifactStore,
) -> AudioResult:
    """Render the script to audio and hand the bytes to the artifact store.
    Returns the downloadable URL (+ placeholder flag). Callers treat a raised
    exception as non-fatal (audio_url stays None, digest/script still stand)."""
    rendered = renderer.render(script, request.soul, job_id)
    name = f"{artifact_stem(job_id)}.{rendered.extension}"
    url = artifacts.put(name, rendered.data, rendered.media_type)
    return AudioResult(url=url, placeholder=rendered.placeholder)


# --- In-process orchestrator (BackgroundRunner) ----------------------------


def run_job(job_id: str, request: DigestRequest, store: JobStore, deps: Deps) -> None:
    job = store.get(job_id)
    if job is None:
        log.error("run_job: unknown job_id %s", job_id)
        return

    try:
        _run(job, request, store, deps)
    except Exception as err:
        # R7: BackgroundRunner has no retry mechanism to re-raise into (unlike
        # chorus.inngest_app.run_digest_body), so this stays a terminal
        # `failed` either way — but the classification is still recorded in
        # the error string as a diagnostic, so a human reading Job.error can
        # tell "this would have been retried on Inngest" from "never would
        # have been".
        retryable = is_retryable(err)
        log.exception("job %s failed (retryable=%s)", job_id, retryable)
        job.status = JobStatus.failed
        job.error = f"{type(err).__name__}: {err} (retryable={retryable})"
        try:
            store.save(job)
        except Exception:
            log.exception("job %s: could not persist failed status", job_id)


def _run(job: Job, request: DigestRequest, store: JobStore, deps: Deps) -> None:
    usage = job.usage or JobUsage()
    job.usage = usage

    t0 = time.perf_counter()
    try:
        ingested = stage_ingest(request, deps.provider)
    except AllEpisodesFailed as err:
        usage.stage_seconds["ingest"] = time.perf_counter() - t0
        job.status = JobStatus.failed
        job.error = str(err)
        store.save(job)
        return
    usage.stage_seconds["ingest"] = time.perf_counter() - t0
    usage.skipped = ingested.skipped
    usage.transcript_sources = {
        r.episode.resolved_id(): r.transcript.source or "unknown" for r in ingested.resolved
    }

    # Any exception here propagates to run_job's handler -> failed with reason.
    t0 = time.perf_counter()
    curated = [stage_curate_episode(r, request, deps.llm) for r in ingested.resolved]
    job.digest = Digest(
        soul_version=soul_version(request.soul),
        soul_origin=request.soul_origin,
        episodes=[c.digest for c in curated],
    )
    usage.stage_seconds["curate"] = time.perf_counter() - t0
    usage.llm_tokens = sum_llm_tokens([c.tokens for c in curated])
    job.status = JobStatus.digest_ready
    store.save(job)

    # From here the digest exists and is the deliverable; the layers degrade.
    t0 = time.perf_counter()
    try:
        job.script = stage_script(job.digest, request, deps.composer)
    except Exception as err:  # noqa: BLE001 - non-fatal: digest is still valid
        job.script = None
        job.warnings.append(f"script synthesis failed: {type(err).__name__}: {err}")
        log.warning("job %s: script synthesis failed (non-fatal): %s", job.job_id, err)
    usage.stage_seconds["script"] = time.perf_counter() - t0

    if job.script is not None:
        t0 = time.perf_counter()
        try:
            audio = stage_audio(job.script, request, job.job_id, deps.renderer, deps.artifacts)
            job.audio_url = audio.url
            if audio.placeholder:
                job.warnings.append(PLACEHOLDER_AUDIO_WARNING)
        except Exception as err:  # noqa: BLE001 - audio failure is non-fatal (failure-mode table)
            job.audio_url = None
            job.warnings.append(f"audio render failed: {type(err).__name__}: {err}")
            log.warning("job %s: audio render failed (non-fatal): %s", job.job_id, err)
        usage.stage_seconds["audio"] = time.perf_counter() - t0

    job.status = JobStatus.done
    store.save(job)
