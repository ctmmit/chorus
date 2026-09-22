"""Shared exception base classes: whether a failure is worth retrying.

Every module that raises an exception a caller (the Inngest runner, the
in-process pipeline) might want to retry should raise a `RetryableError`
subclass; failures that will never succeed on retry (bad input, a poisoned
identity, a malformed request) should raise a `TerminalError` subclass.
Two agents may each need this file; identical content merges cleanly.
"""
from __future__ import annotations


class RetryableError(Exception):
    """A failure that may succeed if retried (timeout, outage, transient 5xx,
    a database connection blip) — never terminal on its own."""


class TerminalError(Exception):
    """A failure that will not succeed on retry (invalid input, a request
    that can never be satisfied) — retrying it wastes work and money."""
