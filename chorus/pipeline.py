"""Job orchestration: ingest -> curate -> script -> audio, with status updates.

Lifecycle (Q3): queued -> digest_ready (digest set) -> done (audio set) | failed.
Failure semantics from the failure-mode table:
- all transcripts fail  -> status=failed with a reason (never empty done)
- audio render fails     -> status=done with audio_url=None (non-fatal)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from chorus.audio import AudioRenderer, get_audio_renderer
from chorus.curation import build_digest
from chorus.ingest import AllEpisodesFailed, ingest
from chorus.jobs import JobStore
from chorus.llm import LLMClient, get_llm_client
from chorus.models import DigestRequest, JobStatus
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
        ingested = ingest(request.episodes, deps.provider)
    except AllEpisodesFailed as err:
        job.status = JobStatus.failed
        job.error = str(err)
        store.save(job)
        return
    except Exception as err:  # noqa: BLE001 - defensive: any ingest failure -> failed, not crash
        job.status = JobStatus.failed
        job.error = f"ingest error: {err}"
        store.save(job)
        return

    digest = build_digest(
        ingested, request.soul, request.context, deps.llm, request.highlight_count
    )
    job.digest = digest
    job.status = JobStatus.digest_ready
    store.save(job)

    script = deps.composer.write_script(digest, request.soul, request.context)
    job.script = script
    try:
        path = deps.renderer.render(script, request.soul)
        job.audio_url = f"/artifacts/{path.name}"
    except Exception as err:  # noqa: BLE001 - audio failure is non-fatal (failure-mode table)
        job.audio_url = None
        log.warning("audio render failed (non-fatal): %s", err)

    job.status = JobStatus.done
    store.save(job)
