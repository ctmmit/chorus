"""Is there a newer Chorus, and how does this install get it?

Releases are GitHub Releases on the public repository, tagged `vX.Y.Z` and
published to PyPI as `chorus-agent` by `.github/workflows/release.yml`. The
check is cached for a day in `~/.chorus/update_check.json`, so the agent's
`onboarding_status` costs one network call a day at most. A failed check is
reported in `UpdateInfo.error`, never raised: an offline machine still runs.

Chorus never updates itself in the background. `chorus update` applies an
update when the principal (or their agent, under the `auto` policy) runs it.
"""
from __future__ import annotations

import json
import os
import re
import sys
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from enum import StrEnum

import httpx
from pydantic import BaseModel, Field

from chorus import __version__, paths

DIST_NAME = "chorus-agent"
GITHUB_REPO = "ctmmit/chorus"
RELEASES_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases"
CHECK_TTL = timedelta(hours=24)
CHECK_TIMEOUT_SECONDS = 4.0
RELEASES_PER_PAGE = 30
CACHE_FILENAME = "update_check.json"
NO_CHECK_ENV = "CHORUS_NO_UPDATE_CHECK"
UNKNOWN_VERSION = "0.0.0"
_VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)")


class InstallKind(StrEnum):
    clone = "clone"  # a git checkout, editable install
    uvx = "uvx"  # an ephemeral `uvx` environment, refreshed on launch
    uv_tool = "uv-tool"  # `uv tool install chorus-agent`
    pip = "pip"  # any other installed distribution


class Release(BaseModel):
    version: str
    notes: str = ""
    url: str = ""
    published_at: str | None = None


class UpdateInfo(BaseModel):
    installed: str
    latest: str | None = None
    update_available: bool = False
    breaking: bool = False
    changes: list[Release] = Field(default_factory=list)
    install_kind: InstallKind
    update_command: str
    checked_at: str | None = None
    error: str | None = None
    agent_note: str | None = None


def parse_version(text: str) -> tuple[int, int, int] | None:
    match = _VERSION_RE.match(text.strip())
    return (int(match[1]), int(match[2]), int(match[3])) if match else None


def is_breaking(installed: str, latest: str) -> bool:
    """A major bump, or a minor bump while still on 0.x (semver's rule that
    anything may change before 1.0)."""
    old, new = parse_version(installed), parse_version(latest)
    if old is None or new is None:
        return True
    if new[0] != old[0]:
        return True
    return new[0] == 0 and new[1] != old[1]


def installed_version() -> str:
    """The version of the code that is actually running. `chorus.__version__`
    is the single source (pyproject reads it), so it is right even when an
    editable install's metadata is stale."""
    return __version__ or UNKNOWN_VERSION


def install_kind() -> InstallKind:
    if (paths.REPO_ROOT / ".git").exists():
        return InstallKind.clone
    prefix = sys.prefix.replace("\\", "/").lower()
    if "/uv/tools/" in prefix:
        return InstallKind.uv_tool
    if "/uv/" in prefix and ("/archive-v" in prefix or "/builds-v" in prefix):
        return InstallKind.uvx
    return InstallKind.pip


def update_command(kind: InstallKind) -> str:
    if kind is InstallKind.uvx:
        return "restart your agent: uvx fetches the newest chorus-agent when it launches Chorus"
    if kind is InstallKind.uv_tool:
        return f"uv tool upgrade {DIST_NAME}"
    return "chorus update"


def fetch_releases(client: httpx.Client | None = None) -> list[Release]:
    owned = client is None
    http = client or httpx.Client(timeout=CHECK_TIMEOUT_SECONDS)
    try:
        resp = http.get(
            RELEASES_URL,
            params={"per_page": RELEASES_PER_PAGE},
            headers={"Accept": "application/vnd.github+json"},
        )
        resp.raise_for_status()
        items = resp.json()
    finally:
        if owned:
            http.close()
    releases = []
    for item in items:
        if item.get("draft") or item.get("prerelease"):
            continue
        tag = str(item.get("tag_name") or "")
        if parse_version(tag) is None:
            continue
        releases.append(
            Release(
                version=tag.lstrip("v"),
                notes=str(item.get("body") or "").strip(),
                url=str(item.get("html_url") or ""),
                published_at=item.get("published_at"),
            )
        )
    return releases


def _cache_path() -> str:
    return str(paths.home() / CACHE_FILENAME)


def _cached(now: datetime) -> tuple[list[Release], str] | None:
    try:
        with open(_cache_path(), encoding="utf-8") as fh:
            data = json.load(fh)
        checked = datetime.fromisoformat(data["checked_at"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if now - checked > CHECK_TTL:
        return None
    return [Release.model_validate(r) for r in data.get("releases", [])], data["checked_at"]


def _store(releases: list[Release], checked_at: str) -> None:
    payload = {"checked_at": checked_at, "releases": [r.model_dump() for r in releases]}
    paths.home().mkdir(parents=True, exist_ok=True)
    with open(_cache_path(), "w", encoding="utf-8") as fh:
        json.dump(payload, fh)


def check(
    *,
    force: bool = False,
    fetch: Callable[[], list[Release]] = fetch_releases,
    now: datetime | None = None,
    installed: str | None = None,
) -> UpdateInfo:
    current = installed or installed_version()
    kind = install_kind()
    info = UpdateInfo(installed=current, install_kind=kind, update_command=update_command(kind))
    moment = now or datetime.now(UTC)
    cached = None if force else _cached(moment)
    if cached is not None:
        releases, info.checked_at = cached
    else:
        try:
            releases = fetch()
        except (httpx.HTTPError, ValueError) as err:
            info.error = f"could not check for updates: {type(err).__name__}: {err}"
            return info
        info.checked_at = moment.isoformat()
        _store(releases, info.checked_at)
    installed_key = parse_version(current) or (0, 0, 0)
    newer = sorted(
        (r for r in releases if (parse_version(r.version) or (0, 0, 0)) > installed_key),
        key=lambda r: parse_version(r.version) or (0, 0, 0),
        reverse=True,
    )
    if newer:
        info.latest = newer[0].version
        info.update_available = True
        info.breaking = is_breaking(current, newer[0].version)
        info.changes = newer
    elif releases:
        info.latest = max(releases, key=lambda r: parse_version(r.version) or (0, 0, 0)).version
    return info


def status_check(policy: str | None) -> UpdateInfo | None:
    """The check `onboarding_status` and `chorus status` run: none until the
    principal has chosen an update policy, none when it is `off` or
    `CHORUS_NO_UPDATE_CHECK` is set; otherwise cached, with a note telling
    the agent what the policy lets it do."""
    if policy in (None, "off") or os.environ.get(NO_CHECK_ENV):
        return None
    info = check()
    if not info.update_available:
        return info
    if policy == "auto" and not info.breaking:
        info.agent_note = (
            f"Chorus {info.latest} is available and the principal chose automatic updates. "
            f"Apply it now ({info.update_command}), then tell them what changed."
        )
    else:
        kind = "a breaking update" if info.breaking else "an update"
        info.agent_note = (
            f"Chorus {info.latest} is {kind}. Tell the principal what changed (`changes`) and "
            f"ask before applying it ({info.update_command})."
        )
    return info
