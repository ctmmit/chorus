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
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass

from chorus.audio import AudioRenderer, get_audio_renderer
from chorus.curation import build_digest
from chorus.ingest import AllEpisodesFailed, ingest
from chorus.jobs import JobStore
from chorus.llm import LLMClient, get_llm_client
from chorus.models import DigestRequest, Job, JobStatus, JobUsage, LLMTokens
from chorus.script import ScriptComposer, get_script_composer
from chorus.transcript_cache import CachingTranscriptProvider, SqliteTranscriptCache
from chorus.transcripts import (
    ChainTranscriptProvider,
    DeepgramTranscriptProvider,
    FixtureTranscriptProvider,
    ManagedCaptionsProvider,
    RssTranscriptProvider,
    TranscriptProvider,
)

log = logging.getLogger("chorus.pipeline")

TRANSCRIPT_API_KEY_ENV = "TRANSCRIPT_API_KEY"  # Supadata managed captions
DEEPGRAM_API_KEY_ENV = "DEEPGRAM_API_KEY"


@dataclass
class Deps:
    provider: TranscriptProvider
    llm: LLMClient
    composer: ScriptComposer
    renderer: AudioRenderer


def default_deps() -> Deps:
    """Build the transcript provider ladder from the environment: fixtures
    first (so tests/dev never depend on a live third party), then whichever
    live providers have keys configured, all wrapped in a SQLite-backed cache
    so a repeat request for the same episode never re-hits a paid API."""
    providers: list[TranscriptProvider] = [FixtureTranscriptProvider()]
    active = ["fixture"]

    transcript_api_key = os.environ.get(TRANSCRIPT_API_KEY_ENV)
    if transcript_api_key:
        providers.append(ManagedCaptionsProvider(transcript_api_key))
        active.append("supadata")

    rss_provider = RssTranscriptProvider()
    providers.append(rss_provider)
    active.append("rss")

    deepgram_api_key = os.environ.get(DEEPGRAM_API_KEY_ENV)
    if deepgram_api_key:
        providers.append(DeepgramTranscriptProvider(deepgram_api_key, rss_provider=rss_provider))
        active.append("deepgram")

    log.info("transcripts: provider chain = %s", " -> ".join(active))
    chain = ChainTranscriptProvider(providers)
    cached_provider = CachingTranscriptProvider(chain, SqliteTranscriptCache())

    return Deps(
        provider=cached_provider,
        llm=get_llm_client(),
        composer=get_script_composer(),
        renderer=get_audio_renderer(),
    )


def _token_snapshot(llm: LLMClient) -> LLMTokens | None:
    """Clients that meter tokens expose a `usage` accumulator (AnthropicLLMClient).
    It is shared across jobs, so per-job numbers are a delta of two snapshots."""
    meter = getattr(llm, "usage", None)
    if meter is None:
        return None
    return LLMTokens(
        calls=meter.calls,
        input_tokens=meter.input_tokens,
        output_tokens=meter.output_tokens,
        cache_read_tokens=meter.cache_read_tokens,
        cache_write_tokens=meter.cache_write_tokens,
    )


def _token_delta(before: LLMTokens | None, after: LLMTokens | None) -> LLMTokens | None:
    if before is None or after is None:
        return None
    return LLMTokens(**{k: getattr(after, k) - getattr(before, k) for k in LLMTokens.model_fields})


def run_job(job_id: str, request: DigestRequest, store: JobStore, deps: Deps) -> None:
    job = store.get(job_id)
    if job is None:
        log.error("run_job: unknown job_id %s", job_id)
        return

    try:
        _run(job, request, store, deps)
    except Exception as err:  # noqa: BLE001 - last line of defense: terminal state, never stuck
        log.exception("job %s failed", job_id)
        job.status = JobStatus.failed
        job.error = f"{type(err).__name__}: {err}"
        try:
            store.save(job)
        except Exception:  # noqa: BLE001 - store itself is broken; nothing more to do
            log.exception("job %s: could not persist failed status", job_id)


def _run(job: Job, request: DigestRequest, store: JobStore, deps: Deps) -> None:
    usage = job.usage or JobUsage()
    job.usage = usage

    t0 = time.perf_counter()
    try:
        ingested = ingest(request.episodes, deps.provider)
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
    tokens_before = _token_snapshot(deps.llm)
    job.digest = build_digest(
        ingested,
        request.soul,
        request.context,
        deps.llm,
        request.highlight_count,
        soul_origin=request.soul_origin,
    )
    usage.stage_seconds["curate"] = time.perf_counter() - t0
    usage.llm_tokens = _token_delta(tokens_before, _token_snapshot(deps.llm))
    job.status = JobStatus.digest_ready
    store.save(job)

    # From here the digest exists and is the deliverable; the layers degrade.
    t0 = time.perf_counter()
    try:
        job.script = deps.composer.write_script(job.digest, request.soul, request.context)
    except Exception as err:  # noqa: BLE001 - non-fatal: digest is still valid
        job.script = None
        job.warnings.append(f"script synthesis failed: {type(err).__name__}: {err}")
        log.warning("job %s: script synthesis failed (non-fatal): %s", job.job_id, err)
    usage.stage_seconds["script"] = time.perf_counter() - t0

    if job.script is not None:
        t0 = time.perf_counter()
        try:
            path = deps.renderer.render(job.script, request.soul, job.job_id)
            job.audio_url = f"/artifacts/{path.name}"
        except Exception as err:  # noqa: BLE001 - audio failure is non-fatal (failure-mode table)
            job.audio_url = None
            job.warnings.append(f"audio render failed: {type(err).__name__}: {err}")
            log.warning("job %s: audio render failed (non-fatal): %s", job.job_id, err)
        usage.stage_seconds["audio"] = time.perf_counter() - t0

    job.status = JobStatus.done
    store.save(job)
