"""Distribution: the Claude Code and Codex plugins, registering Chorus with the
agents on this machine, and the weekly OS schedule.

Nothing here touches the real machine. Agent CLIs and schedulers go to fake
runners, config files live under tmp_path, and `Path.home()` is redirected
where a module reads it.
"""
from __future__ import annotations

import filecmp
import json
import re
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from chorus import __version__, agent_setup, os_schedule, paths, registration
from chorus.cli import EXIT_OK
from chorus.cli import main as cli_main
from chorus.local_run import write_digest_markdown
from chorus.models import EpisodeInput
from chorus.onboarding import Brain, OnboardingConfig, Voice, load_config
from chorus.registration import Host, RegistrationResult, Target
from chorus.wizard import Wizard, WizardIO

pytestmark = pytest.mark.usefixtures("chorus_home")

ROOT = paths.REPO_ROOT
UVX_ARGS = ["--from", "chorus-agent@latest", "chorus-mcp"]


def _proc(code: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], code, stdout=stdout, stderr=stderr)


class FakeRunner:
    def __init__(self, replies: dict[str, subprocess.CompletedProcess[str]] | None = None) -> None:
        self.replies = replies or {}
        self.commands: list[list[str]] = []

    def __call__(self, cmd: Sequence[str]) -> subprocess.CompletedProcess[str]:
        self.commands.append(list(cmd))
        joined = " ".join(cmd)
        for key, reply in self.replies.items():
            if key in joined:
                return reply
        return _proc()


# --- plugins ---------------------------------------------------------------


def _json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def test_claude_plugin_and_marketplace_agree() -> None:
    plugin = _json(ROOT / ".claude-plugin" / "plugin.json")
    market = _json(ROOT / ".claude-plugin" / "marketplace.json")
    assert plugin["mcpServers"]["chorus"] == {"command": "uvx", "args": UVX_ARGS}
    entry = market["plugins"][0]
    assert entry["name"] == plugin["name"] == "chorus"
    assert "version" not in plugin  # unpinned: installs follow the repository
    for skill in entry["skills"]:
        assert (ROOT / skill / "SKILL.md").is_file()


def test_codex_plugin_matches_the_package() -> None:
    root = ROOT / "plugins" / "codex" / "chorus"
    manifest = _json(root / ".codex-plugin" / "plugin.json")
    servers = _json(root / ".mcp.json")["mcpServers"]
    market = _json(ROOT / ".agents" / "plugins" / "marketplace.json")
    assert manifest["version"] == __version__
    assert servers["chorus"]["command"] == "uvx" and servers["chorus"]["args"] == UVX_ARGS
    assert (ROOT / market["plugins"][0]["source"]["path"]).resolve() == root.resolve()
    assert filecmp.cmp(
        root / "skills" / "chorus-onboard" / "SKILL.md",
        ROOT / "skills" / "chorus-onboard" / "SKILL.md",
        shallow=False,
    ), "plugins/codex/chorus/skills/chorus-onboard drifted from skills/chorus-onboard; copy it over"


# --- registration ----------------------------------------------------------


@pytest.fixture
def hosts(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, Path]:
    """Every agent 'installed', with config files under tmp_path."""
    desktop = tmp_path / "Claude" / "claude_desktop_config.json"
    desktop.parent.mkdir()
    codex_home = tmp_path / "codex"
    codex_home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setattr(registration, "claude_desktop_config", lambda: desktop)
    monkeypatch.setattr(registration.shutil, "which", lambda name: f"/bin/{name}")
    monkeypatch.setattr(registration, "launch", lambda: ("uvx", UVX_ARGS))
    return {"desktop": desktop, "codex": codex_home / "config.toml"}


def test_detect_reports_each_host(hosts: dict[str, Path]) -> None:
    run = FakeRunner({"claude mcp get": _proc(1), "grok mcp list": _proc(0, stdout="chorus  uvx")})
    found = {t.host: t for t in registration.detect(run)}
    assert all(t.found for t in found.values())
    assert not found[Host.claude_code].registered
    assert found[Host.grok].registered
    assert "claude mcp add --scope user chorus -- uvx --from" in found[Host.claude_code].change


def test_cli_hosts_register_through_their_own_command(hosts: dict[str, Path]) -> None:
    run = FakeRunner()
    assert registration.register(Host.claude_code, run).ok
    assert registration.register(Host.grok, run).ok
    assert run.commands == [
        ["claude", "mcp", "add", "--scope", "user", "chorus", "--", "uvx", *UVX_ARGS],
        ["grok", "mcp", "add", "chorus", "--", "uvx", *UVX_ARGS],
    ]


def test_codex_registration_appends_once_with_a_backup(hosts: dict[str, Path]) -> None:
    hosts["codex"].write_text('model = "o3"\n', encoding="utf-8")
    first = registration.register(Host.codex)
    second = registration.register(Host.codex)
    text = hosts["codex"].read_text(encoding="utf-8")
    assert first.ok and first.backup and second.message == "already registered"
    assert text.startswith('model = "o3"')
    assert text.count("[mcp_servers.chorus]") == 1
    import tomllib

    assert tomllib.loads(text)["mcp_servers"]["chorus"] == {"command": "uvx", "args": UVX_ARGS}


def test_desktop_registration_keeps_other_servers(hosts: dict[str, Path]) -> None:
    hosts["desktop"].write_text(
        json.dumps({"mcpServers": {"other": {"command": "x"}}, "theme": "dark"}), encoding="utf-8"
    )
    result = registration.register(Host.claude_desktop)
    data = _json(hosts["desktop"])
    assert result.ok and result.backup
    assert data["theme"] == "dark" and data["mcpServers"]["other"] == {"command": "x"}
    assert data["mcpServers"]["chorus"] == {"command": "uvx", "args": UVX_ARGS}


def test_corrupt_desktop_config_is_never_overwritten(hosts: dict[str, Path]) -> None:
    hosts["desktop"].write_text("{ not json", encoding="utf-8")
    result = registration.register(Host.claude_desktop)
    assert not result.ok and "not valid JSON" in result.message
    assert hosts["desktop"].read_text(encoding="utf-8") == "{ not json"


def test_cli_register_list_changes_nothing(
    hosts: dict[str, Path], capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    run = FakeRunner({"claude mcp get": _proc(1), "grok mcp list": _proc(0, stdout="")})
    monkeypatch.setattr(registration, "_run", run)
    assert cli_main(["register", "--list"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "Claude Code: claude mcp add" in out and "Codex: append [mcp_servers.chorus]" in out
    assert not hosts["codex"].exists() and not hosts["desktop"].exists()
    assert not any("add" in c for cmd in run.commands for c in cmd)


# --- schedule --------------------------------------------------------------


CMD = ["C:/Chorus/.venv/Scripts/chorus.exe", "run"]


def test_windows_plan_is_a_weekly_task() -> None:
    plan = os_schedule.plan("wed", "07:30", platform="win32", command=CMD)
    cmd = plan.commands[0]
    assert cmd[:3] == ["schtasks", "/Create", "/F"]
    assert cmd[cmd.index("/D") + 1] == "WED" and cmd[cmd.index("/ST") + 1] == "07:30"
    assert cmd[cmd.index("/TR") + 1] == "C:/Chorus/.venv/Scripts/chorus.exe run"


def test_macos_plan_is_a_launch_agent() -> None:
    plan = os_schedule.plan("sun", "21:05", platform="darwin", command=["/u/chorus", "run"])
    assert plan.file_path and plan.file_path.endswith("com.chorus.weekly.plist")
    text = plan.file_text or ""
    assert "<integer>0</integer>" in text  # Sunday
    assert "<integer>21</integer>" in text and "<integer>5</integer>" in text
    assert "<string>/u/chorus</string>" in text


def test_linux_plan_replaces_only_its_own_cron_line() -> None:
    plan = os_schedule.plan("mon", "08:00", platform="linux", command=["/u/chorus", "run"])
    assert (plan.file_text or "").startswith("0 8 * * 1 /u/chorus run")
    existing = "5 4 * * * backup.sh\n0 9 * * 2 old-chorus # chorus-weekly\n"
    run = FakeRunner({"crontab -l": _proc(0, stdout=existing)})
    written: list[str] = []

    def pipe(cmd: Sequence[str], text: str) -> subprocess.CompletedProcess[str]:
        written.append(text)
        return _proc()

    os_schedule.apply(plan, run, pipe)
    assert written[0].splitlines() == ["5 4 * * * backup.sh", plan.file_text]
    os_schedule.remove("linux", run, pipe)
    assert written[1].splitlines() == ["5 4 * * * backup.sh"]


@pytest.mark.parametrize(("day", "time"), [("someday", "08:00"), ("mon", "8am"), ("mon", "24:00")])
def test_bad_schedules_are_refused(day: str, time: str) -> None:
    with pytest.raises(os_schedule.ScheduleError):
        os_schedule.plan(day, time, platform="linux", command=CMD)


def test_scheduler_refusal_is_reported() -> None:
    plan = os_schedule.plan(platform="win32", command=CMD)
    run = FakeRunner({"schtasks": _proc(1, stderr="ERROR: Access is denied.")})
    with pytest.raises(os_schedule.ScheduleError, match="Access is denied"):
        os_schedule.apply(plan, run)


def _setup(brain: str, voice: str) -> None:
    agent_setup.set_choice("mode", "host-agent")
    agent_setup.set_choice("brain", brain)
    agent_setup.set_choice("voice", voice)


def test_agent_driven_setups_schedule_in_the_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    _setup("host", "text-only")
    applied: list[Any] = []
    monkeypatch.setattr(os_schedule, "apply", lambda plan, *a: applied.append(plan) or "ok")
    out = agent_setup.schedule(on=True)
    assert not out["scheduled"] and "own scheduler" in out["message"]
    assert applied == []
    prompt = agent_setup.step_options("shows")
    assert any("your own scheduler" in n for n in prompt.agent_notes)


def test_self_contained_setups_get_an_os_task(monkeypatch: pytest.MonkeyPatch) -> None:
    _setup("mock", "text-only")
    applied: list[Any] = []
    monkeypatch.setattr(os_schedule, "apply", lambda plan, *a: applied.append(plan) or "Scheduled.")
    out = agent_setup.schedule(on=True, day="fri", time="06:00")
    assert out["scheduled"] and applied[0].day == "fri" and applied[0].time == "06:00"
    assert load_config().weekly


# --- wizard ----------------------------------------------------------------


class ScriptedIO:
    def __init__(self, answers: list[str]) -> None:
        self.answers = list(answers)
        self.output: list[str] = []

    def ask(self, prompt: str) -> str:
        self.output.append(prompt)
        return self.answers.pop(0)

    def say(self, text: str) -> None:
        self.output.append(text)

    def io(self) -> WizardIO:
        return WizardIO(ask=self.ask, secret=self.ask, say=self.say)


def test_wizard_offers_the_os_schedule() -> None:
    scheduled: list[Any] = []
    scripted = ScriptedIO(["", "", "y", "y"])  # no shows, no feeds, weekly yes, add it
    wizard = Wizard(io=scripted.io(), scheduler=lambda plan: scheduled.append(plan) or "Scheduled.")
    config = wizard._shows(OnboardingConfig(brain=Brain.mock, voice=Voice.text_only))
    assert config.weekly and len(scheduled) == 1


def test_wizard_offers_registration_per_agent() -> None:
    targets = [
        Target(host=Host.claude_code, label="Claude Code", found=True, registered=False, change="c"),
        Target(host=Host.codex, label="Codex", found=True, registered=True, change="x"),
        Target(host=Host.grok, label="Grok Build", found=False, registered=False, change="g"),
    ]
    registered: list[Host] = []

    def registrar(host: Host) -> RegistrationResult:
        registered.append(host)
        return RegistrationResult(host=host, ok=True, message="registered")

    scripted = ScriptedIO(["y"])
    Wizard(io=scripted.io(), targets=lambda: targets, registrar=registrar)._offer_registration()
    assert registered == [Host.claude_code]
    assert any("Codex: already connected" in line for line in scripted.output)


# --- digest file -----------------------------------------------------------


def test_each_run_leaves_a_readable_digest() -> None:
    from chorus.local_run import run_digest

    agent_setup.set_choice("mode", "local-agent")
    agent_setup.set_choice("brain", "mock")
    agent_setup.set_choice("voice", "text-only")
    agent_setup.set_choice("transcripts", "free")
    agent_setup.soul_save("me", agent_setup.soul_draft("preset", preset="investor").markdown)
    agent_setup.set_shows([], [], weekly=False)
    agent_setup.set_choice("updates", "off")
    job = run_digest(load_config(), [EpisodeInput(video_id="sample_public")])
    digest = write_digest_markdown(job).read_text(encoding="utf-8")
    assert digest.startswith("# Chorus digest")
    # Highlights are stamped where the quoted claim starts, not at the window start.
    assert re.search(r"\*\*\d+:\d{2}\*\*", digest) or "Nothing surfaced" in digest
    assert "Episode:" in digest


def test_cli_schedule_status_reads_the_scheduler(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(os_schedule, "is_scheduled", lambda: False)
    assert cli_main(["schedule", "status"]) == EXIT_OK
    assert "not scheduled" in capsys.readouterr().out


def test_an_agent_cli_that_cannot_start_is_not_fatal(hosts: dict[str, Path]) -> None:
    # `which` finds /bin/claude, but there is no such executable to launch.
    found = {t.host: t for t in registration.detect()}
    assert found[Host.claude_code].found and not found[Host.claude_code].registered


def test_plist_arguments_are_xml_escaped() -> None:
    plan = os_schedule.plan(platform="darwin", command=["/Users/a&b/<x>/chorus", "run"])
    assert "<string>/Users/a&amp;b/&lt;x&gt;/chorus</string>" in (plan.file_text or "")
