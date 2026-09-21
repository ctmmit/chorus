"""Ingest: resolve each episode to a transcript, skipping failures gracefully.

The §6 specified failure case lives here: a missing/removed/no-caption episode
is skipped with a structured reason and the run continues on the rest. The
flagged critical gap also lives here: if NOTHING resolves, raise
AllEpisodesFailed so the job ends as `failed` (never an empty `done`).
"""
from __future__ import annotations

import logging

from chorus.models import EpisodeInput, IngestResult, ResolvedEpisode, SkippedEpisode
from chorus.transcripts import TranscriptNotFound, TranscriptProvider, TranscriptProviderError

log = logging.getLogger("chorus.ingest")


class AllEpisodesFailed(Exception):
    """No episode resolved a transcript. The job must end failed, not empty-done."""


def ingest(episodes: list[EpisodeInput], provider: TranscriptProvider) -> IngestResult:
    resolved: list[ResolvedEpisode] = []
    skipped: list[SkippedEpisode] = []

    for episode in episodes:
        try:
            transcript = provider.get(episode)
        except (TranscriptNotFound, TranscriptProviderError, ValueError) as err:
            reason = str(err)
            log.warning("ingest: skipping episode (%s)", reason)
            skipped.append(SkippedEpisode(episode=episode, reason=reason))
            continue
        resolved.append(ResolvedEpisode(episode=episode, transcript=transcript))

    if episodes and not resolved:
        raise AllEpisodesFailed(
            f"all {len(episodes)} episode(s) failed transcript resolution"
        )

    return IngestResult(resolved=resolved, skipped=skipped)
