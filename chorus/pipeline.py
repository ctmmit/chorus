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
from dataclasses import dataclass

from chorus.audio import AudioRenderer, get_audio_renderer
from chorus.curation import build_digest
from chorus.ingest import AllEpisodesFailed, ingest
from chorus.jobs import JobStore
from chorus.llm import LLMClient, get_llm_client
from chorus.models import DigestRequest, Job, JobStatus
from chorus.script import ScriptComposer, get_script_composer
from chorus.transcripts import FixtureTranscriptProvider, TranscriptProvider

log = logging.getLogger("chorus.pipeline")


@dataclass
class Deps:
    provider: TranscriptProvider
    llm: LLMClient
    composer: ScriptComposer
    renderer: AudioRenderer


def default_deps() -> Deps:
    # Offline default: fixtures for transcripts; mock LLM/TTS unless keys present.
    # The managed-API transcript provider slots in here behind TranscriptProvider.
    return Deps(
        provider=FixtureTranscriptProvider(),
        llm=get_llm_client(),
        composer=get_script_composer(),
        renderer=get_audio_renderer(),
    )


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
    try:
        ingested = ingest(request.episodes, deps.provider)
    except AllEpisodesFailed as err:
        job.status = JobStatus.failed
        job.error = str(err)
        store.save(job)
        return

    # Any exception here propagates to run_job's handler -> failed with reason.
    job.digest = build_digest(
        ingested,
        request.soul,
        request.context,
        deps.llm,
        request.highlight_count,
        soul_origin=request.soul_origin,
    )
    job.status = JobStatus.digest_ready
    store.save(job)

    # From here the digest exists and is the deliverable; the layers degrade.
    try:
        job.script = deps.composer.write_script(job.digest, request.soul, request.context)
    except Exception as err:  # noqa: BLE001 - non-fatal: digest is still valid
        job.script = None
        job.warnings.append(f"script synthesis failed: {type(err).__name__}: {err}")
        log.warning("job %s: script synthesis failed (non-fatal): %s", job.job_id, err)

    if job.script is not None:
        try:
            path = deps.renderer.render(job.script, request.soul, job.job_id)
            job.audio_url = f"/artifacts/{path.name}"
        except Exception as err:  # noqa: BLE001 - audio failure is non-fatal (failure-mode table)
            job.audio_url = None
            job.warnings.append(f"audio render failed: {type(err).__name__}: {err}")
            log.warning("job %s: audio render failed (non-fatal): %s", job.job_id, err)

    job.status = JobStatus.done
    store.save(job)
