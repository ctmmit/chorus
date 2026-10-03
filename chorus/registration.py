"""Register the local Chorus MCP server with the agents installed here.

`chorus register` (and the last step of `chorus onboard` in host-agent mode)
finds Claude Code, Claude Desktop (whose servers Cowork sessions also get),
Codex and Grok Build, shows the exact change for each, and applies it only
after the principal says yes. CLI hosts are changed through their own
`mcp add` commands. Config files are edited only after a timestamped backup
in `~/.chorus/backups/`, and an unreadable file is never overwritten.

The launch command matches the install: `uvx --from chorus-agent@latest
chorus-mcp` for a uvx install (which keeps it current), else the absolute
path of the `chorus-mcp` launcher next to this Python.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel

from chorus import paths
from chorus.version import DIST_NAME, InstallKind, install_kind

SERVER_NAME = "chorus"
CODEX_TABLE = f"[mcp_servers.{SERVER_NAME}]"
_CODEX_TABLE_RE = re.compile(rf"^\s*\[mcp_servers\.{SERVER_NAME}\]\s*$", re.MULTILINE)

Runner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]


class Host(StrEnum):
    claude_code = "claude-code"
    claude_desktop = "claude-desktop"
    codex = "codex"
    grok = "grok"


HOST_LABELS: dict[Host, str] = {
    Host.claude_code: "Claude Code",
    Host.claude_desktop: "Claude Desktop and Cowork",
    Host.codex: "Codex",
    Host.grok: "Grok Build",
}


class Target(BaseModel):
    host: Host
    label: str
    found: bool
    registered: bool
    change: str
    location: str | None = None


class RegistrationResult(BaseModel):
    host: Host
    ok: bool
    message: str
    backup: str | None = None


class RegistrationError(RuntimeError):
    """A change that was refused or failed; the message says why."""


LAUNCH_FAILED = 127
CLI_TIMEOUT_SECONDS = 60


def _run(cmd: Sequence[str]) -> subprocess.CompletedProcess[str]:
    """Run an agent CLI. One that is on PATH but cannot start, or hangs, reads
    as a failed command, so detection never crashes `chorus register`."""
    try:
        return subprocess.run(
            list(cmd), capture_output=True, text=True, check=False, timeout=CLI_TIMEOUT_SECONDS
        )
    except (OSError, subprocess.TimeoutExpired) as err:
        return subprocess.CompletedProcess(list(cmd), LAUNCH_FAILED, stdout="", stderr=str(err))


def launch() -> tuple[str, list[str]]:
    if install_kind() is InstallKind.uvx:
        return "uvx", ["--from", f"{DIST_NAME}@latest", "chorus-mcp"]
    scripts = Path(sys.executable).parent
    launcher = scripts / ("chorus-mcp.exe" if os.name == "nt" else "chorus-mcp")
    if launcher.is_file():
        return str(launcher), []
    return sys.executable, ["-m", "chorus.mcp_server"]


def _display(cmd: str, args: list[str]) -> str:
    return " ".join([cmd, *args])


def claude_desktop_config() -> Path | None:
    """Where Claude Desktop reads MCP servers. The Windows Store (MSIX) build
    reads a virtualized copy under its package folder instead of %APPDATA%."""
    home = Path.home()
    if sys.platform == "win32":
        local = Path(os.environ.get("LOCALAPPDATA", home / "AppData" / "Local"))
        for package in sorted(local.glob("Packages/Claude_*")):
            msix = package / "LocalCache" / "Roaming" / "Claude"
            if msix.is_dir():
                return msix / "claude_desktop_config.json"
        root = Path(os.environ.get("APPDATA", home / "AppData" / "Roaming")) / "Claude"
    elif sys.platform == "darwin":
        root = home / "Library" / "Application Support" / "Claude"
    else:
        root = home / ".config" / "Claude"
    return root / "claude_desktop_config.json" if root.is_dir() else None


def codex_config() -> Path:
    return Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "config.toml"


def _codex_block(cmd: str, args: list[str]) -> str:
    return (
        f"\n{CODEX_TABLE}\n"
        f"command = {json.dumps(cmd)}\n"
        f"args = [{', '.join(json.dumps(a) for a in args)}]\n"
    )


def detect(run: Runner | None = None) -> list[Target]:
    run = run or _run
    cmd, args = launch()
    shown = _display(cmd, args)
    targets: list[Target] = []

    claude = shutil.which("claude")
    targets.append(
        Target(
            host=Host.claude_code,
            label=HOST_LABELS[Host.claude_code],
            found=claude is not None,
            registered=bool(claude) and run(["claude", "mcp", "get", SERVER_NAME]).returncode == 0,
            change=f"claude mcp add --scope user {SERVER_NAME} -- {shown}",
        )
    )

    desktop = claude_desktop_config()
    desktop_registered = False
    if desktop is not None and desktop.is_file():
        try:
            servers = json.loads(desktop.read_text(encoding="utf-8")).get("mcpServers", {})
            desktop_registered = SERVER_NAME in servers
        except (ValueError, AttributeError):
            desktop_registered = False
    targets.append(
        Target(
            host=Host.claude_desktop,
            label=HOST_LABELS[Host.claude_desktop],
            found=desktop is not None,
            registered=desktop_registered,
            change=f'add "{SERVER_NAME}": {{"command": ..., "args": ...}} to mcpServers '
            "(restart Claude Desktop afterwards)",
            location=str(desktop) if desktop else None,
        )
    )

    codex_file = codex_config()
    codex_found = shutil.which("codex") is not None or codex_file.parent.is_dir()
    codex_text = codex_file.read_text(encoding="utf-8") if codex_file.is_file() else ""
    targets.append(
        Target(
            host=Host.codex,
            label=HOST_LABELS[Host.codex],
            found=codex_found,
            registered=bool(_CODEX_TABLE_RE.search(codex_text)),
            change=f"append {CODEX_TABLE} with command = {json.dumps(cmd)}",
            location=str(codex_file),
        )
    )

    grok = shutil.which("grok")
    grok_listed = False
    if grok:
        listing = run(["grok", "mcp", "list"])
        grok_listed = listing.returncode == 0 and SERVER_NAME in listing.stdout
    targets.append(
        Target(
            host=Host.grok,
            label=HOST_LABELS[Host.grok],
            found=grok is not None,
            registered=grok_listed,
            change=f"grok mcp add {SERVER_NAME} -- {shown}",
        )
    )
    return targets


def _backup(file: Path, label: str) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    target = paths.backups_dir() / f"{label}-{stamp}{file.suffix}"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(file, target)
    return target


def register(host: Host, run: Runner | None = None) -> RegistrationResult:
    run = run or _run
    cmd, args = launch()
    if host in (Host.claude_code, Host.grok):
        base = ["claude", "mcp", "add", "--scope", "user"] if host is Host.claude_code else [
            "grok",
            "mcp",
            "add",
        ]
        result = run([*base, SERVER_NAME, "--", cmd, *args])
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            return RegistrationResult(host=host, ok=False, message=f"failed: {detail}")
        return RegistrationResult(host=host, ok=True, message="registered")

    if host is Host.codex:
        file = codex_config()
        text = file.read_text(encoding="utf-8") if file.is_file() else ""
        if _CODEX_TABLE_RE.search(text):
            return RegistrationResult(host=host, ok=True, message="already registered")
        backup = _backup(file, "codex-config") if file.is_file() else None
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(text.rstrip("\n") + "\n" + _codex_block(cmd, args), encoding="utf-8")
        return RegistrationResult(
            host=host, ok=True, message="registered", backup=str(backup) if backup else None
        )

    desktop = claude_desktop_config()
    if desktop is None:
        return RegistrationResult(host=host, ok=False, message="Claude Desktop is not installed")
    data: dict[str, object] = {}
    backup = None
    if desktop.is_file():
        try:
            loaded = json.loads(desktop.read_text(encoding="utf-8") or "{}")
        except ValueError as err:
            return RegistrationResult(
                host=host,
                ok=False,
                message=f"{desktop} is not valid JSON ({err}); fix it, then run chorus register",
            )
        if not isinstance(loaded, dict):
            message = f"{desktop} is not a JSON object"
            return RegistrationResult(host=host, ok=False, message=message)
        data = loaded
        backup = _backup(desktop, "claude-desktop-config")
    servers = data.get("mcpServers")
    if servers is None:
        servers = {}
        data["mcpServers"] = servers
    if not isinstance(servers, dict):
        return RegistrationResult(host=host, ok=False, message="mcpServers is not a JSON object")
    servers[SERVER_NAME] = {"command": cmd, "args": args}
    desktop.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return RegistrationResult(
        host=host,
        ok=True,
        message="registered; restart Claude Desktop",
        backup=str(backup) if backup else None,
    )
