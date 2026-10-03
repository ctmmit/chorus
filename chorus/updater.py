"""Apply a Chorus update for whichever way it was installed.

- clone: refuse on uncommitted changes, `git pull --ff-only`, then reinstall
  the pinned requirements and the editable package. A branch that has
  diverged from upstream (someone customized core code) is never merged or
  rebased automatically; the principal or their agent gets the exact steps.
- uv tool: `uv tool upgrade chorus-agent`.
- pip: `python -m pip install --upgrade chorus-agent`.
- uvx: nothing to run; uvx resolves the newest release when it next starts
  Chorus, so the answer is "restart your agent".

The principal's state in `~/.chorus/` is never touched here. Config
migrations run when the updated code next starts (`chorus.migrations`).
"""
from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable, Sequence

from pydantic import BaseModel

from chorus import paths
from chorus.version import DIST_NAME, InstallKind

Runner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]


class UpdateOutcome(BaseModel):
    applied: bool
    steps: list[str]
    message: str


def _run(cmd: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(cmd), capture_output=True, text=True, check=False)


def _rebase_help(branch: str) -> str:
    return (
        "Your checkout has commits that are not on the upstream branch, so Chorus will not "
        "merge them for you. To keep your changes and take the update:\n"
        f"  git -C {paths.REPO_ROOT} fetch origin\n"
        f"  git -C {paths.REPO_ROOT} rebase origin/{branch}\n"
        "Resolve any conflicts, then run `chorus update` again. Customizations that live in "
        f"{paths.extensions_dir()} instead of the core code never need this."
    )


def apply_update(kind: InstallKind, run: Runner = _run) -> UpdateOutcome:
    steps: list[str] = []

    def step(cmd: Sequence[str]) -> subprocess.CompletedProcess[str]:
        steps.append(" ".join(cmd))
        return run(cmd)

    if kind is InstallKind.uvx:
        return UpdateOutcome(
            applied=False,
            steps=[],
            message="Nothing to run: uvx fetches the newest Chorus when your agent next starts "
            "it. Restart your agent.",
        )
    if kind is InstallKind.uv_tool:
        result = step(["uv", "tool", "upgrade", DIST_NAME])
        return _finish(result, steps, "uv tool upgrade failed")
    if kind is InstallKind.pip:
        result = step([sys.executable, "-m", "pip", "install", "--upgrade", DIST_NAME])
        return _finish(result, steps, "pip upgrade failed")

    root = str(paths.REPO_ROOT)
    dirty = step(["git", "-C", root, "status", "--porcelain", "--untracked-files=no"])
    if dirty.returncode != 0:
        detail = dirty.stderr.strip()
        return UpdateOutcome(applied=False, steps=steps, message=f"git status failed: {detail}")
    if dirty.stdout.strip():
        return UpdateOutcome(
            applied=False,
            steps=steps,
            message="Your checkout has uncommitted changes to tracked files. Commit or stash "
            "them, then run `chorus update` again.",
        )
    branch = step(["git", "-C", root, "rev-parse", "--abbrev-ref", "HEAD"]).stdout.strip() or "main"
    pyproject_before = _read_pyproject()
    pulled = step(["git", "-C", root, "pull", "--ff-only"])
    if pulled.returncode != 0:
        detail = (pulled.stderr or pulled.stdout).strip()
        lowered = detail.lower()
        diverged = "not possible to fast-forward" in lowered or "diverg" in lowered
        message = _rebase_help(branch) if diverged else f"git pull failed: {detail}"
        return UpdateOutcome(applied=False, steps=steps, message=message)
    commands = [
        [sys.executable, "-m", "pip", "install", "-r", str(paths.REPO_ROOT / "requirements.txt")]
    ]
    # An editable install already runs the pulled code. Reinstalling the
    # package itself (new commands, a renamed distribution) is only needed
    # when pyproject.toml changed, and it rewrites the `chorus` launcher,
    # which Windows locks while that launcher is running this update.
    if _read_pyproject() != pyproject_before:
        commands.append([sys.executable, "-m", "pip", "install", "-e", root, "--no-deps"])
    for cmd in commands:
        installed = step(cmd)
        if installed.returncode != 0:
            return _finish(installed, steps, "reinstall failed")
    return UpdateOutcome(
        applied=True, steps=steps, message="Updated. Restart your agent so it loads the new Chorus."
    )


def _read_pyproject() -> str:
    try:
        return (paths.REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    except OSError:
        return ""


def _finish(
    result: subprocess.CompletedProcess[str], steps: list[str], failure: str
) -> UpdateOutcome:
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        if sys.platform == "win32" and "Access is denied" in detail:
            detail += (
                "\nWindows locks the running `chorus` launcher. Run "
                f"`{sys.executable} -m chorus.cli update` instead, with your agent closed."
            )
        return UpdateOutcome(applied=False, steps=steps, message=f"{failure}: {detail}")
    return UpdateOutcome(
        applied=True, steps=steps, message="Updated. Restart your agent so it loads the new Chorus."
    )
