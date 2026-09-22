"""Ingest: resolve each episode to a transcript, skipping failures gracefully.

The §6 specified failure case lives here: a missing/removed/no-caption episode
is skipped with a structured reason and the run continues on the rest. The
flagged critical gap also lives here: if NOTHING resolves, raise
AllEpisodesFailed so the job ends as `failed` (never an empty `done`).

R16 (docs/REVIEW_WAVE1.md #16): `TranscriptNotFound` (terminal — the source
genuinely has nothing) and `TranscriptProviderError` (retryable — we
couldn't reach/trust a source that might have it) are no longer treated
identically. A `ProviderError` skip is recorded with a `"transient: "`-
prefixed reason, and if *every* episode in the request failed that way, the
whole ingest raises `TranscriptProviderError` instead of `AllEpisodesFailed`
— so run_job (chorus/pipeline.py) and the Inngest runner re-raise it and get
retried, rather than the job settling into a terminal "failed" that a retry
could have fixed.
"""
from __future__ import annotations

import logging

from chorus.errors import TerminalError
from chorus.models import EpisodeInput, IngestResult, ResolvedEpisode, SkippedEpisode
from chorus.transcripts import TranscriptNotFound, TranscriptProvider, TranscriptProviderError

log = logging.getLogger("chorus.ingest")

# Prefix applied to a skip reason when the cause was a TranscriptProviderError
# (transient: the source might have resolved given a working connection/auth)
# rather than a TranscriptNotFound (the source genuinely has nothing).
TRANSIENT_REASON_PREFIX = "transient: "


class AllEpisodesFailed(TerminalError):
    """No episode resolved a transcript, and not every failure was transient
    (see: TranscriptProviderError is raised instead, retryable, when every
    episode failed transiently)."""


def ingest(episodes: list[EpisodeInput], provider: TranscriptProvider) -> IngestResult:
    resolved: list[ResolvedEpisode] = []
    skipped: list[SkippedEpisode] = []
    transient_count = 0

    for episode in episodes:
        try:
            transcript = provider.get(episode)
        except TranscriptProviderError as err:
            reason = f"{TRANSIENT_REASON_PREFIX}{err}"
            log.warning("ingest: skipping episode (%s)", reason)
            skipped.append(SkippedEpisode(episode=episode, reason=reason))
            transient_count += 1
            continue
        except (TranscriptNotFound, ValueError) as err:
            reason = str(err)
            log.warning("ingest: skipping episode (%s)", reason)
            skipped.append(SkippedEpisode(episode=episode, reason=reason))
            continue
        resolved.append(ResolvedEpisode(episode=episode, transcript=transcript))

    if episodes and not resolved:
        if skipped and transient_count == len(skipped):
            raise TranscriptProviderError(
                f"all {len(episodes)} episode(s) failed transiently: "
                f"{'; '.join(s.reason for s in skipped)}"
            )
        raise AllEpisodesFailed(
            f"all {len(episodes)} episode(s) failed transcript resolution"
        )

    return IngestResult(resolved=resolved, skipped=skipped)
