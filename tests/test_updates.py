"""Updates: version checks, migrations, applying an update per install kind,
and the bundled data that lets a wheel install run without a checkout.

Offline throughout: releases come from injected fetchers or an httpx
MockTransport, and git/pip/uv commands go to a recording fake runner.
"""
from __future__ import annotations

import filecmp
import subprocess
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from chorus import __version__, agent_setup, paths, updater, version
from chorus.cli import EXIT_OK
from chorus.cli import main as cli_main
from chorus.migrations import migrate_config
from chorus.onboarding import CONFIG_SCHEMA_VERSION, OnboardingConfig, OnboardingError, save_config
from chorus.version import InstallKind, Release, check, is_breaking, parse_version, status_check

pytestmark = pytest.mark.usefixtures("chorus_home")

NOW = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)


def _releases(*versions: str) -> list[Release]:
    return [Release(version=v, notes=f"notes for {v}") for v in versions]


class Fetcher:
    def __init__(self, releases: list[Release] | Exception) -> None:
        self.releases = releases
        self.calls = 0

    def __call__(self) -> list[Release]:
        self.calls += 1
        if isinstance(self.releases, Exception):
            raise self.releases
        return self.releases


# --- version check ---------------------------------------------------------


def test_parse_and_breaking_rules() -> None:
    assert parse_version("v1.2.3") == (1, 2, 3)
    assert parse_version("0.4.0rc1") == (0, 4, 0)
    assert parse_version("latest") is None
    assert is_breaking("0.2.0", "0.3.0")  # a minor bump before 1.0
    assert not is_breaking("0.2.0", "0.2.5")
    assert is_breaking("1.4.0", "2.0.0")
    assert not is_breaking("1.4.0", "1.9.0")


def test_check_lists_newer_releases_newest_first() -> None:
    info = check(fetch=Fetcher(_releases("0.1.0", "0.2.1", "0.3.0", "0.2.0")), installed="0.2.0", now=NOW)
    assert info.update_available and info.latest == "0.3.0" and info.breaking
    assert [r.version for r in info.changes] == ["0.3.0", "0.2.1"]


def test_check_up_to_date() -> None:
    info = check(fetch=Fetcher(_releases("0.1.0", "0.2.0")), installed="0.2.0", now=NOW)
    assert not info.update_available and info.latest == "0.2.0" and info.changes == []


def test_check_is_cached_for_a_day() -> None:
    fetch = Fetcher(_releases("0.3.0"))
    check(fetch=fetch, installed="0.2.0", now=NOW)
    check(fetch=fetch, installed="0.2.0", now=NOW + timedelta(hours=23))
    assert fetch.calls == 1
    check(fetch=fetch, installed="0.2.0", now=NOW + timedelta(hours=25))
    check(fetch=fetch, installed="0.2.0", now=NOW + timedelta(hours=25), force=True)
    assert fetch.calls == 3


def test_offline_check_reports_instead_of_raising() -> None:
    info = check(fetch=Fetcher(httpx.ConnectError("offline")), installed="0.2.0", now=NOW)
    assert info.error and "offline" in info.error
    assert not info.update_available


def test_fetch_releases_skips_drafts_prereleases_and_odd_tags() -> None:
    payload = [
        {"tag_name": "v0.3.0", "body": "Faster.", "html_url": "u3", "published_at": "x"},
        {"tag_name": "v0.4.0", "draft": True},
        {"tag_name": "v0.5.0-beta", "prerelease": True},
        {"tag_name": "nightly"},
    ]
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
    with httpx.Client(transport=transport) as client:
        releases = version.fetch_releases(client)
    assert [(r.version, r.notes) for r in releases] == [("0.3.0", "Faster.")]


def test_status_check_follows_the_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(version.NO_CHECK_ENV)
    assert status_check(None) is None
    assert status_check("off") is None
    patch = Fetcher(_releases(_bump_patch(__version__)))
    monkeypatch.setattr(version, "fetch_releases", patch)
    monkeypatch.setattr(version.check, "__kwdefaults__", {**version.check.__kwdefaults__, "fetch": patch})
    notify = status_check("notify")
    assert notify is not None and notify.agent_note and "ask before" in notify.agent_note
    auto = status_check("auto")
    assert auto is not None and auto.agent_note and "Apply it now" in auto.agent_note


def test_onboarding_status_carries_the_update(monkeypatch: pytest.MonkeyPatch) -> None:
    agent_setup.set_choice("updates", "notify")
    fake = version.UpdateInfo(
        installed=__version__,
        latest="9.0.0",
        update_available=True,
        install_kind=InstallKind.clone,
        update_command="chorus update",
        agent_note="ask",
    )
    monkeypatch.setattr(agent_setup, "status_check", lambda policy: fake if policy else None)
    assert agent_setup.agent_status().update == fake


def test_this_checkout_is_a_clone() -> None:
    assert version.install_kind() is InstallKind.clone
    assert version.installed_version() == __version__


def _bump_patch(text: str) -> str:
    major, minor, patch = parse_version(text) or (0, 0, 0)
    return f"{major}.{minor}.{patch + 1}"


# --- migrations ------------------------------------------------------------


def _write_raw(text: str) -> Path:
    target = paths.config_path()
    target.write_text(text, encoding="utf-8")
    return target


def test_current_config_needs_no_migration() -> None:
    save_config(OnboardingConfig())
    assert migrate_config() is None


def test_older_config_is_migrated_with_a_backup() -> None:
    target = _write_raw('schema_version = 0\nbrain_choice = "mock"\n')

    def v0_to_v1(raw: dict[str, Any]) -> dict[str, Any]:
        raw["brain"] = raw.pop("brain_choice")
        return raw

    result = migrate_config(migrations={0: v0_to_v1}, target=CONFIG_SCHEMA_VERSION)
    assert result is not None and result.from_version == 0
    assert 'brain = "mock"' in target.read_text(encoding="utf-8")
    backup = Path(result.backup or "")
    assert "brain_choice" in backup.read_text(encoding="utf-8")


def test_unmigratable_config_is_left_untouched() -> None:
    original = 'schema_version = 0\nbrain = "telepathy"\n'
    target = _write_raw(original)
    with pytest.raises(OnboardingError, match="no migration"):
        migrate_config(migrations={})
    with pytest.raises(ValueError):
        migrate_config(migrations={0: lambda raw: raw})  # result fails validation
    assert target.read_text(encoding="utf-8") == original
    assert list(paths.backups_dir().glob("config-*")) == []


def test_newer_config_is_refused() -> None:
    _write_raw(f"schema_version = {CONFIG_SCHEMA_VERSION + 1}\n")
    with pytest.raises(OnboardingError, match="newer than this Chorus"):
        migrate_config()


# --- applying updates ------------------------------------------------------


class FakeRunner:
    def __init__(self, replies: dict[str, subprocess.CompletedProcess[str]] | None = None) -> None:
        self.replies = replies or {}
        self.commands: list[list[str]] = []

    def __call__(self, cmd: Sequence[str]) -> subprocess.CompletedProcess[str]:
        self.commands.append(list(cmd))
        for key, reply in self.replies.items():
            if key in " ".join(cmd):
                return reply
        return subprocess.CompletedProcess(list(cmd), 0, stdout="", stderr="")


def _proc(code: int, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], code, stdout=stdout, stderr=stderr)


def test_clone_update_pulls_fast_forward_and_reinstalls_requirements() -> None:
    run = FakeRunner({"rev-parse": _proc(0, stdout="main\n")})
    outcome = updater.apply_update(InstallKind.clone, run)
    assert outcome.applied
    joined = [" ".join(c) for c in run.commands]
    assert any("pull --ff-only" in c for c in joined)
    assert any("install -r" in c and "requirements.txt" in c for c in joined)
    assert not any("install -e" in c for c in joined)  # pyproject.toml did not change


def test_clone_update_refuses_uncommitted_changes() -> None:
    run = FakeRunner({"status --porcelain": _proc(0, stdout=" M chorus/curation.py\n")})
    outcome = updater.apply_update(InstallKind.clone, run)
    assert not outcome.applied and "uncommitted" in outcome.message
    assert not any("pull" in " ".join(c) for c in run.commands)


def test_diverged_clone_gets_rebase_steps_not_a_merge() -> None:
    run = FakeRunner(
        {
            "rev-parse": _proc(0, stdout="main\n"),
            "pull --ff-only": _proc(128, stderr="fatal: Not possible to fast-forward, aborting."),
        }
    )
    outcome = updater.apply_update(InstallKind.clone, run)
    assert not outcome.applied
    assert "rebase origin/main" in outcome.message
    assert str(paths.extensions_dir()) in outcome.message


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        (InstallKind.uv_tool, ["uv", "tool", "upgrade", "chorus-agent"]),
        (InstallKind.pip, ["-m", "pip", "install", "--upgrade", "chorus-agent"]),
    ],
)
def test_package_installs_upgrade_with_their_tool(kind: InstallKind, expected: list[str]) -> None:
    run = FakeRunner()
    assert updater.apply_update(kind, run).applied
    assert run.commands[0][-len(expected) :] == expected


def test_uvx_needs_only_a_restart() -> None:
    run = FakeRunner()
    outcome = updater.apply_update(InstallKind.uvx, run)
    assert not outcome.applied and run.commands == [] and "Restart" in outcome.message


def test_cli_update_check_reports_changes(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = version.UpdateInfo(
        installed="0.2.0",
        latest="0.2.1",
        update_available=True,
        changes=_releases("0.2.1"),
        install_kind=InstallKind.clone,
        update_command="chorus update",
    )
    monkeypatch.setattr(version, "check", lambda force=False: fake)
    assert cli_main(["update", "--check"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "0.2.1 is an update" in out and "notes for 0.2.1" in out


# --- bundled data (wheel installs) -----------------------------------------


BUNDLED = [
    "episodes.json",
    "context.md",
    "souls/soul_investor.md",
    "souls/soul_popculture.md",
    "transcripts/sample_public.json",
]


@pytest.mark.parametrize("name", BUNDLED)
def test_bundled_data_matches_fixtures(name: str) -> None:
    checkout = paths.REPO_ROOT / "fixtures" / name
    assert filecmp.cmp(checkout, paths.BUNDLED_DIR / name, shallow=False), (
        f"chorus/_bundled/{name} drifted from fixtures/{name}; copy it over"
    )


def test_without_a_checkout_fixtures_come_from_the_package(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(paths, "REPO_ROOT", tmp_path / "site-packages")
    assert paths.fixtures_dir() == paths.BUNDLED_DIR
    from chorus.soul import load_preset

    assert "Curation Guidance" in load_preset("investor")
