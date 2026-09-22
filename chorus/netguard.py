"""SSRF guard: one chokepoint for every outbound URL a caller can influence
(feed_url, audio_url, a transcript URL advertised inside an RSS feed, an
enclosure URL). R10 (docs/REVIEW_WAVE1.md #10/#11): without this, a caller
can point Chorus's server-side fetches at loopback, private, link-local, or
cloud-metadata addresses and use response timing/content as an internal
network probe.

`safe_url()` is the only thing callers need: it validates scheme, resolves
the hostname, and rejects any disallowed address — raising `UnsafeURLError`
(a `TerminalError`; a URL that resolves to a blocked target will resolve to
the same target on retry, so retrying it is never useful) otherwise
returning the URL unchanged. DNS resolution goes through an injectable
`resolver` so tests can simulate hostile DNS answers without touching the
network.

Callers that follow redirects must re-validate the `Location` header through
`safe_url` on every hop (a first-hop-safe URL can redirect to an unsafe one)
and cap the hop count; see chorus/transcripts.py's `_fetch_bounded`.
"""
from __future__ import annotations

import ipaddress
import os
from typing import Callable
from urllib.parse import urlsplit

from chorus.errors import TerminalError

# Caller-controlled URLs (feed_url, audio_url, guid...) share MAX_URL_CHARS
# with chorus.models' request-field limits; kept as its own constant here so
# netguard has no import dependency on chorus.models.
MAX_URL_CHARS = 2_048

# ElevenLabs/Deepgram-style single-hop redirects are normal; more than this
# on a caller-controlled URL is treated as evasion, not a real redirect chain.
MAX_REDIRECT_HOPS = 3

_ALLOW_HTTP_ENV = "CHORUS_ALLOW_HTTP"

# Hostname patterns blocked by name regardless of what they resolve to today
# (cloud metadata endpoints in particular are not always caught by address
# range alone, and *.internal/*.local are a common private-DNS convention).
_BLOCKED_HOSTNAMES = {"localhost", "metadata.google.internal"}
_BLOCKED_HOST_SUFFIXES = (".internal", ".local", ".localhost")

# (hostname) -> [ip address, ...]; swap in tests via safe_url(url, resolver=...)
# so DNS is never actually touched off of a unit test.
Resolver = Callable[[str], list[str]]


class UnsafeURLError(TerminalError):
    """A URL is not permitted: disallowed scheme, blocked hostname, or an
    address that resolves into a loopback/private/link-local/multicast/
    reserved/unspecified range. Never retryable — the same URL resolves to
    the same disallowed target every time."""


def _default_resolver(host: str) -> list[str]:
    import socket

    infos = socket.getaddrinfo(host, None)
    return [str(info[4][0]) for info in infos]


def _is_blocked_address(addr: str) -> bool:
    ip = ipaddress.ip_address(addr)
    return (
        ip.is_loopback
        or ip.is_private
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def _is_blocked_hostname(host: str) -> bool:
    lowered = host.lower().rstrip(".")
    if lowered in _BLOCKED_HOSTNAMES:
        return True
    return any(lowered.endswith(suffix) for suffix in _BLOCKED_HOST_SUFFIXES)


def _literal_ip(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """`host` as an IP literal (e.g. a raw `127.0.0.1` or bracketed IPv6),
    or None when it is a name that needs DNS resolution."""
    try:
        return ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return None


def safe_url(url: str, *, resolver: Resolver | None = None) -> str:
    """Validate that `url` is safe for the server to fetch itself.

    Allows only https, unless `CHORUS_ALLOW_HTTP=1` (set for tests/local dev
    only — see DEPLOY.md). Rejects a blocked-by-name hostname, a raw IP
    literal in a disallowed range, and a hostname whose DNS answers include
    any disallowed address (loopback/private/link-local/multicast/reserved/
    unspecified, IPv4 and IPv6 alike). Returns `url` unchanged on success.

    `resolver(host) -> [addr, ...]` overrides DNS resolution — tests pass a
    fixed hostname->address map so this function (and everything built on
    it) never needs real network access to exercise both the allow and deny
    paths.
    """
    if len(url) > MAX_URL_CHARS:
        raise UnsafeURLError(f"url exceeds {MAX_URL_CHARS} characters")

    parts = urlsplit(url)
    allow_http = os.environ.get(_ALLOW_HTTP_ENV) == "1"
    allowed_schemes = {"https"} | ({"http"} if allow_http else set())
    if parts.scheme not in allowed_schemes:
        raise UnsafeURLError(f"scheme {parts.scheme!r} not permitted for {url!r}")

    host = parts.hostname
    if not host:
        raise UnsafeURLError(f"url has no host: {url!r}")

    if _is_blocked_hostname(host):
        raise UnsafeURLError(f"host {host!r} is not permitted")

    literal = _literal_ip(host)
    if literal is not None:
        if _is_blocked_address(str(literal)):
            raise UnsafeURLError(f"host {host!r} is a disallowed address literal")
        return url

    resolve = resolver or _default_resolver
    try:
        addresses = resolve(host)
    except OSError as err:
        raise UnsafeURLError(f"could not resolve host {host!r}: {err}") from err
    if not addresses:
        raise UnsafeURLError(f"host {host!r} did not resolve to any address")
    for addr in addresses:
        if _is_blocked_address(addr):
            raise UnsafeURLError(f"host {host!r} resolves to disallowed address {addr}")

    return url
