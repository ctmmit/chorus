"""Run `chorus run` weekly with the operating system's own scheduler.

For a local-agent setup where Chorus does all the work itself (an Anthropic
or demo brain, and an ElevenLabs-key or text voice). When the principal's
agent is the brain or the voice, a weekly run needs that agent awake, so
the schedule belongs in the agent's own scheduler instead (see
`agent_schedule_note`); an OS task here could only fail.

- Windows: a Task Scheduler task (`schtasks`), named `TASK_NAME`.
- macOS: a LaunchAgent plist loaded with `launchctl`.
- Linux and other POSIX: one crontab line tagged with `CRON_MARKER`.

Every builder is pure (it returns commands or file text), so the plans are
tested without touching the machine; `apply` runs them.
"""
from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape

from pydantic import BaseModel

from chorus import paths
from chorus.version import DIST_NAME, InstallKind, install_kind

TASK_NAME = "Chorus weekly digest"
LAUNCHD_LABEL = "com.chorus.weekly"
CRON_MARKER = "# chorus-weekly"
DEFAULT_DAY = "mon"
DEFAULT_TIME = "08:00"
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")

Runner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]


class ScheduleError(ValueError):
    """An invalid schedule, or a scheduler that refused the change."""


class SchedulePlan(BaseModel):
    platform: str
    day: str
    time: str
    run_command: list[str]
    commands: list[list[str]]
    file_path: str | None = None
    file_text: str | None = None
    description: str


SCHEDULER_TIMEOUT_SECONDS = 60
LAUNCH_FAILED = 127


def _run(cmd: Sequence[str]) -> subprocess.CompletedProcess[str]:
    """A missing or hung scheduler command reads as a failure, not a crash."""
    try:
        return subprocess.run(
            list(cmd),
            capture_output=True,
            text=True,
            check=False,
            timeout=SCHEDULER_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as err:
        return subprocess.CompletedProcess(list(cmd), LAUNCH_FAILED, stdout="", stderr=str(err))


def run_command() -> list[str]:
    """The command the scheduler runs: `chorus run`, launched the same way
    this install launches everything else."""
    if install_kind() is InstallKind.uvx:
        return ["uvx", "--from", f"{DIST_NAME}@latest", "chorus", "run"]
    scripts = Path(sys.executable).parent
    launcher = scripts / ("chorus.exe" if os.name == "nt" else "chorus")
    if launcher.is_file():
        return [str(launcher), "run"]
    return [sys.executable, "-m", "chorus.cli", "run"]


def _validate(day: str, time: str) -> tuple[int, int]:
    if day not in DAYS:
        raise ScheduleError(f"day must be one of {', '.join(DAYS)}")
    match = _TIME_RE.match(time)
    if not match:
        raise ScheduleError("time must be 24-hour HH:MM, e.g. 08:00")
    return int(match[1]), int(match[2])


def _quote_windows(arg: str) -> str:
    return f'"{arg}"' if " " in arg or not arg else arg


def plan(
    day: str = DEFAULT_DAY,
    time: str = DEFAULT_TIME,
    platform: str | None = None,
    command: list[str] | None = None,
) -> SchedulePlan:
    hour, minute = _validate(day, time)
    target = platform or sys.platform
    cmd = command or run_command()
    log = paths.home() / "weekly.log"
    if target == "win32":
        task_run = " ".join(_quote_windows(a) for a in cmd)
        return SchedulePlan(
            platform=target,
            day=day,
            time=time,
            run_command=cmd,
            commands=[
                [
                    "schtasks",
                    "/Create",
                    "/F",
                    "/SC",
                    "WEEKLY",
                    "/D",
                    day.upper(),
                    "/ST",
                    time,
                    "/TN",
                    TASK_NAME,
                    "/TR",
                    task_run,
                ]
            ],
            description=f'Task Scheduler task "{TASK_NAME}", every {day} at {time}',
        )
    if target == "darwin":
        plist = Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"
        weekday = (DAYS.index(day) + 1) % 7  # launchd: 0 and 7 are Sunday, 1 is Monday
        args = "\n".join(f"    <string>{xml_escape(a)}</string>" for a in cmd)
        log_xml = xml_escape(str(log))
        text = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>{LAUNCHD_LABEL}</string>
  <key>ProgramArguments</key>
  <array>
{args}
  </array>
  <key>StartCalendarInterval</key>
  <dict>
    <key>Weekday</key>
    <integer>{weekday}</integer>
    <key>Hour</key>
    <integer>{hour}</integer>
    <key>Minute</key>
    <integer>{minute}</integer>
  </dict>
  <key>StandardOutPath</key>
  <string>{log_xml}</string>
  <key>StandardErrorPath</key>
  <string>{log_xml}</string>
</dict>
</plist>
"""
        return SchedulePlan(
            platform=target,
            day=day,
            time=time,
            run_command=cmd,
            commands=[
                ["launchctl", "unload", str(plist)],
                ["launchctl", "load", "-w", str(plist)],
            ],
            file_path=str(plist),
            file_text=text,
            description=f"LaunchAgent {LAUNCHD_LABEL}, every {day} at {time}",
        )
    cron_day = (DAYS.index(day) + 1) % 7  # cron: 0 is Sunday
    redirect = f">> {shlex.quote(str(log))} 2>&1"
    line = f"{minute} {hour} * * {cron_day} {shlex.join(cmd)} {redirect} {CRON_MARKER}"
    return SchedulePlan(
        platform=target,
        day=day,
        time=time,
        run_command=cmd,
        commands=[["crontab", "-"]],
        file_text=line,
        description=f"crontab line tagged {CRON_MARKER}, every {day} at {time}",
    )


def _crontab(run: Runner) -> list[str]:
    current = run(["crontab", "-l"])
    lines = current.stdout.splitlines() if current.returncode == 0 else []
    return [line for line in lines if CRON_MARKER not in line]


def _pipe(cmd: Sequence[str], text: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            list(cmd),
            input=text,
            capture_output=True,
            text=True,
            check=False,
            timeout=SCHEDULER_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as err:
        return subprocess.CompletedProcess(list(cmd), LAUNCH_FAILED, stdout="", stderr=str(err))


def apply(
    schedule: SchedulePlan,
    run: Runner | None = None,
    pipe: Callable[[Sequence[str], str], subprocess.CompletedProcess[str]] | None = None,
) -> str:
    run = run or _run
    pipe = pipe or _pipe
    if schedule.platform == "win32":
        result = run(schedule.commands[0])
    elif schedule.platform == "darwin":
        plist = Path(schedule.file_path or "")
        plist.parent.mkdir(parents=True, exist_ok=True)
        plist.write_text(schedule.file_text or "", encoding="utf-8")
        run(schedule.commands[0])  # unloading a job that isn't loaded fails harmlessly
        result = run(schedule.commands[1])
    else:
        lines = [*_crontab(run), schedule.file_text or ""]
        result = pipe(schedule.commands[0], "\n".join(lines) + "\n")
    if result.returncode != 0:
        raise ScheduleError((result.stderr or result.stdout).strip() or "the scheduler refused")
    return f"Scheduled: {schedule.description}."


def remove(
    platform: str | None = None,
    run: Runner | None = None,
    pipe: Callable[[Sequence[str], str], subprocess.CompletedProcess[str]] | None = None,
) -> str:
    run = run or _run
    pipe = pipe or _pipe
    target = platform or sys.platform
    if target == "win32":
        result = run(["schtasks", "/Delete", "/F", "/TN", TASK_NAME])
        if result.returncode != 0 and "cannot find" not in (result.stderr or "").lower():
            raise ScheduleError((result.stderr or result.stdout).strip())
        return "Weekly digest unscheduled."
    if target == "darwin":
        plist = Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"
        run(["launchctl", "unload", str(plist)])
        plist.unlink(missing_ok=True)
        return "Weekly digest unscheduled."
    lines = _crontab(run)
    result = pipe(["crontab", "-"], "\n".join(lines) + ("\n" if lines else ""))
    if result.returncode != 0:
        raise ScheduleError((result.stderr or result.stdout).strip())
    return "Weekly digest unscheduled."


def is_scheduled(platform: str | None = None, run: Runner | None = None) -> bool:
    run = run or _run
    target = platform or sys.platform
    if target == "win32":
        return run(["schtasks", "/Query", "/TN", TASK_NAME]).returncode == 0
    if target == "darwin":
        return (Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist").is_file()
    listing = run(["crontab", "-l"])
    return listing.returncode == 0 and CRON_MARKER in listing.stdout


AGENT_SCHEDULE_NOTE = (
    "Your agent does part of this setup's work, so the weekly run belongs in your agent's own "
    "scheduler (for example a scheduled task in Claude Code or Cowork, or a Codex automation) "
    "with the instruction 'run my Chorus digest'. The operating system can't wake your agent."
)
