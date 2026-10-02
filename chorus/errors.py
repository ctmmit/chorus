"""Retryable vs terminal failure classification (docs/REVIEW_WAVE1.md #7).

`chorus.inngest_app.run_digest_body` uses `is_retryable` to decide, per
pipeline-stage exception: persist a diagnostic and RE-RAISE (so Inngest
retries the function, per its `retries=3` configuration) for a plausibly
transient failure, or mark the job `failed` and return normally (Inngest
acknowledges — no retry) for a deterministic one. The in-process
`BackgroundRunner` path (chorus.pipeline) has no retry mechanism to re-raise
into, so it keeps today's behavior (always terminal `failed`) but records
`is_retryable`'s verdict in the error string as a diagnostic — so a human
reading `Job.error` can tell "this would have been retried on Inngest" from
"this never would have been", even on the runner that doesn't act on it.

`RetryableError`/`TerminalError` are provided as explicit marker base
classes for any NEW exception a caller wants to be unambiguous about; nothing
in this codebase is required to raise them; `is_retryable` also recognizes
today's existing exception types (LLMError, TranscriptProviderError,
AllEpisodesFailed, httpx transport/timeout/5xx, psycopg OperationalError)
without requiring they be rewritten to subclass these markers.
"""
from __future__ import annotations

import httpx


class RetryableError(Exception):
    """Marker: a failure plausibly transient enough that retrying the same
    job later might succeed."""


class TerminalError(Exception):
    """Marker: a failure that will not resolve itself on retry (bad input, a
    deterministic bug, every provider genuinely and permanently has
    nothing)."""


def _is_transcript_provider_error(err: BaseException) -> bool:
    # chorus/transcripts.py is being edited concurrently by another agent in
    # this remediation pass, so import the name lazily and by name only —
    # never hold a module-level reference to anything else from it.
    try:
        from chorus.transcripts import TranscriptProviderError
    except ImportError:  # pragma: no cover - the module is always present
        return False
    return isinstance(err, TranscriptProviderError)


def _is_all_episodes_failed(err: BaseException) -> bool:
    # chorus/ingest.py is likewise being edited concurrently; import lazily.
    try:
        from chorus.ingest import AllEpisodesFailed
    except ImportError:  # pragma: no cover - the module is always present
        return False
    return isinstance(err, AllEpisodesFailed)


def _is_psycopg_operational_error(err: BaseException) -> bool:
    try:
        import psycopg
    except ImportError:  # pragma: no cover - psycopg is a hard dependency
        return False
    return isinstance(err, psycopg.OperationalError)


def is_retryable(err: BaseException) -> bool:
    """True if `err` is plausibly transient and worth retrying; False if it
    is deterministic and retrying cannot help.

    Order matters: the explicit markers and known-terminal shapes
    (AllEpisodesFailed, any other ValueError — covers pydantic
    ValidationError, a ValueError subclass) are checked before the
    known-retryable shapes, so a hypothetical exception that is both (e.g. a
    ValueError raised by an LLM client for a genuinely malformed but
    non-transient reason) is treated conservatively as terminal rather than
    burning retries on something retrying cannot fix.
    """
    from chorus.llm import LLMError

    if isinstance(err, RetryableError):
        return True
    if isinstance(err, TerminalError):
        return False
    if _is_all_episodes_failed(err):
        return False
    if isinstance(err, ValueError):
        return False
    if isinstance(err, LLMError):
        return True
    if _is_transcript_provider_error(err):
        return True
    if isinstance(err, httpx.HTTPStatusError):
        return err.response.status_code >= 500
    if isinstance(err, (httpx.TimeoutException, httpx.TransportError)):
        return True
    return bool(_is_psycopg_operational_error(err))


__all__ = ["RetryableError", "TerminalError", "is_retryable"]
