"""The goal function for docs/PLAN_0.3.md: is the plan done, and what is next.

The plan is ten phases, one pull request each. This script turns each
phase's exit criterion into a contract a machine can check, evaluates every
phase, and answers three questions:

1. Where does each phase stand? `landed` (its contract holds and its tests
   pass in `--root`, which should be a checkout of `origin/main`), `review`
   (its branch is pushed with an open pull request), `merged` (the pull
   request merged but `--root` predates it, so pull first), or `todo`.
2. What should be built next, and from which base? The lowest-numbered phase
   that is neither landed nor in review and whose dependencies are landed or
   in review. A phase whose dependencies are all landed starts from
   `origin/main`; one with a single dependency still in review stacks on that
   dependency's branch; two dependencies still in review on different
   branches means waiting for a merge.
3. Is the goal met? `--target review` (the default) is met when every phase
   is landed or in review, which is as far as an agent may take the plan,
   because merging to `main` is the principal's call. `--target landed` is
   met only when every phase is on `main` and, with `--gate`, the four
   repository checks pass.

A phase's contract is the module names, test files and exit commands the
plan specifies for it (`PHASES` below; docs/PLAN_0.3.md §2 lists the same
names). The test files must exist and pass, and every named symbol must be
defined at module top level, so an empty test file cannot satisfy a phase.

Usage (from a checkout of origin/main):

    python scripts/plan_goal.py                # table, exit 0 if goal met
    python scripts/plan_goal.py --json         # machine-readable report
    python scripts/plan_goal.py --next         # the next phase and its base
    python scripts/plan_goal.py --phase 3      # evaluate one phase only
    python scripts/plan_goal.py --target landed --gate
    python scripts/plan_goal.py --offline      # skip git/gh remote queries

Exit codes: 0 goal met, 1 goal not met, 2 waiting (nothing buildable until
a pull request merges).
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent.parent
BASE_REF = "origin/main"
EXIT_MET = 0
EXIT_NOT_MET = 1
EXIT_WAITING = 2
COMMAND_TIMEOUT_S = 900
SUBPROCESS_TIMEOUT_S = 60


# --- The plan as contracts -------------------------------------------------


@dataclass(frozen=True)
class Symbol:
    """A name that must be defined at top level of `path` (def, class, or
    assignment), or, for a non-Python file, a file that must exist."""

    path: str
    name: str | None = None


@dataclass(frozen=True)
class Phase:
    number: int
    title: str
    tier: int
    branch: str
    depends: tuple[int, ...]
    symbols: tuple[Symbol, ...]
    tests: tuple[str, ...]
    commands: tuple[tuple[str, ...], ...] = ()


PHASES: tuple[Phase, ...] = (
    Phase(
        1, "Chapters and source deep links", 1, "feature/chapters", (),
        (
            Symbol("chorus/chapters.py", "tag_episode"),
            Symbol("chorus/chapters.py", "build_chapters"),
            Symbol("chorus/chapters.py", "write_id3_chapters"),
            Symbol("chorus/chapters.py", "chapters_json"),
            Symbol("chorus/models.py", "Chapter"),
        ),
        ("tests/test_chapters.py",),
    ),
    Phase(
        2, "Private podcast feed", 1, "feature/podcast-feed", (1,),
        (
            Symbol("chorus/podcast_feed.py", "render_feed"),
            Symbol("chorus/podcast_feed.py", "feed_token"),
            Symbol("chorus/podcast_feed.py", "register_feed_tools"),
        ),
        ("tests/test_podcast_feed.py",),
    ),
    Phase(
        3, "Curation eval harness", 2, "feature/curation-evals", (),
        (
            Symbol("chorus/evals.py", "score_case"),
            Symbol("scripts/eval_curation.py"),
            Symbol("fixtures/evals/baseline.json"),
        ),
        ("tests/test_evals.py",),
        (("{python}", "scripts/eval_curation.py"),),
    ),
    Phase(
        4, "Highlight feedback and soul proposals", 1, "feature/soul-feedback", (3,),
        (
            Symbol("chorus/feedback.py", "FeedbackStore"),
            Symbol("chorus/feedback.py", "summarize_feedback"),
            Symbol("chorus/feedback.py", "propose_soul_update"),
            Symbol("chorus/feedback.py", "apply_soul_update"),
            Symbol("chorus/feedback.py", "MIN_RATINGS_FOR_PROPOSAL"),
        ),
        ("tests/test_feedback.py",),
    ),
    Phase(
        5, "Cross-source threads", 1, "feature/threads", (3,),
        (
            Symbol("chorus/threads.py", "build_threads"),
            Symbol("chorus/threads.py", "validate_threads"),
            Symbol("chorus/models.py", "Thread"),
        ),
        ("tests/test_threads.py",),
    ),
    Phase(
        6, "Running memory across weeks", 2, "feature/claim-memory", (5,),
        (
            Symbol("chorus/memory.py", "ClaimStore"),
            Symbol("chorus/memory.py", "REPEAT_PENALTY"),
            Symbol("chorus/memory.py", "REPEAT_WINDOW_WEEKS"),
        ),
        ("tests/test_memory.py",),
    ),
    Phase(
        7, "Context recipes and connectors", 2, "feature/context-recipes", (),
        (
            Symbol("skills/chorus-context/SKILL.md"),
            Symbol("chorus/models.py", "ContextBlock"),
            Symbol("chorus/context.py", "ContextProvider"),
        ),
        ("tests/test_context.py",),
    ),
    Phase(
        8, "Ask the episode", 2, "feature/ask", (),
        (Symbol("chorus/ask.py", "answer"),),
        ("tests/test_ask.py",),
    ),
    Phase(
        9, "Brief me now", 3, "feature/quick-take", (1,),
        (Symbol("chorus/quick_take.py", "quick_take"),),
        ("tests/test_quick_take.py",),
    ),
    Phase(
        10, "Personas as sources", 3, "feature/persona-sources", (2, 4),
        (Symbol("chorus/subscriptions.py", "PersonaSource"),),
        ("tests/test_persona_sources.py",),
    ),
)

GATE_COMMANDS: tuple[tuple[str, ...], ...] = (
    ("{python}", "-m", "pytest", "-q"),
    ("{python}", "-m", "ruff", "check", "chorus", "tests", "scripts"),
    ("{python}", "-m", "mypy", "chorus"),
    ("{python}", "scripts/golden_path.py"),
)


# --- Reports ---------------------------------------------------------------


class State(StrEnum):
    landed = "landed"
    merged = "merged"  # PR merged, but --root predates it
    review = "review"
    todo = "todo"


class PhaseReport(BaseModel):
    number: int
    title: str
    tier: int
    branch: str
    depends: list[int]
    state: State
    missing: list[str] = Field(default_factory=list, description="Contract items not found.")
    failing: list[str] = Field(default_factory=list, description="Tests or commands that failed.")
    pr_url: str | None = None


class NextStep(BaseModel):
    kind: Literal["build", "wait", "done"]
    phase: int | None = None
    title: str | None = None
    branch: str | None = None
    base: str | None = Field(default=None, description="Ref to branch the phase from.")
    reason: str


class GoalReport(BaseModel):
    target: Literal["review", "landed"]
    met: bool
    score: float = Field(description="Share of phases landed or in review, 0 to 1.")
    landed: int
    phases: list[PhaseReport]
    next: NextStep
    gate: dict[str, bool] | None = None


# --- Pure evaluation -------------------------------------------------------

_TOP_LEVEL = r"^(?:async\s+def|def|class)\s+{name}\b|^{name}\s*(?::[^=]+)?="


def defines(source: str, name: str) -> bool:
    """True if `source` defines `name` at module top level."""
    pattern = _TOP_LEVEL.format(name=re.escape(name))
    return re.search(pattern, source, flags=re.MULTILINE) is not None


def missing_contract(phase: Phase, root: Path) -> list[str]:
    """Contract items absent from `root`: files, then top-level names."""
    missing: list[str] = []
    for test in phase.tests:
        if not (root / test).is_file():
            missing.append(test)
    for symbol in phase.symbols:
        file = root / symbol.path
        if not file.is_file():
            missing.append(symbol.path)
        elif symbol.name and not defines(file.read_text(encoding="utf-8"), symbol.name):
            missing.append(f"{symbol.path}:{symbol.name}")
    return sorted(set(missing), key=missing.index)


def satisfied(state: State) -> bool:
    """A dependency in this state lets a dependent phase start."""
    return state in (State.landed, State.merged, State.review)


def next_step(reports: Sequence[PhaseReport]) -> NextStep:
    """The lowest-numbered buildable phase and the base it starts from."""
    by_number = {r.number: r for r in reports}
    pending = [r for r in reports if not satisfied(r.state)]
    if not pending:
        return NextStep(kind="done", reason="every phase is landed or in review")
    waiting: list[str] = []
    for report in pending:
        deps = [by_number[d] for d in report.depends if d in by_number]
        blocking = [d for d in deps if not satisfied(d.state)]
        if blocking:
            continue
        unmerged = [d for d in deps if d.state is State.review]
        branches = {d.branch for d in unmerged}
        if len(branches) > 1:
            waiting.append(
                f"phase {report.number} needs {', '.join(sorted(branches))} merged first"
            )
            continue
        base = f"origin/{branches.pop()}" if branches else BASE_REF
        stacked = f", stacked on phase {unmerged[0].number}" if unmerged else ""
        return NextStep(
            kind="build",
            phase=report.number,
            title=report.title,
            branch=report.branch,
            base=base,
            reason=f"lowest-numbered phase whose dependencies are met{stacked}",
        )
    reason = "; ".join(waiting) or "every remaining phase depends on an unbuilt phase"
    return NextStep(kind="wait", reason=reason)


def goal_met(
    reports: Sequence[PhaseReport], target: str, gate: dict[str, bool] | None
) -> bool:
    if target == "landed":
        on_main = all(r.state is State.landed for r in reports)
        return on_main and (gate is None or all(gate.values()))
    return all(satisfied(r.state) for r in reports)


def score(reports: Sequence[PhaseReport]) -> float:
    if not reports:
        return 1.0
    return round(sum(satisfied(r.state) for r in reports) / len(reports), 3)


# --- I/O: commands, git, gh -------------------------------------------------

Runner = Callable[[Sequence[str], Path], tuple[int, str]]


def run_command(argv: Sequence[str], cwd: Path) -> tuple[int, str]:
    try:
        done = subprocess.run(
            list(argv), cwd=cwd, capture_output=True, text=True,
            timeout=COMMAND_TIMEOUT_S, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as err:
        return 1, f"{type(err).__name__}: {err}"
    return done.returncode, (done.stdout + done.stderr)[-2000:]


def _argv(template: Sequence[str], python: str) -> list[str]:
    return [python if part == "{python}" else part for part in template]


def failing_checks(phase: Phase, root: Path, python: str, runner: Runner) -> list[str]:
    """Run the phase's tests, then its exit commands; name what failed."""
    failing: list[str] = []
    code, _ = runner([python, "-m", "pytest", "-q", *phase.tests], root)
    if code != 0:
        failing.append("pytest " + " ".join(phase.tests))
    for command in phase.commands:
        argv = _argv(command, python)
        code, _ = runner(argv, root)
        if code != 0:
            failing.append(" ".join(command).replace("{python}", "python"))
    return failing


@dataclass(frozen=True)
class PullRequest:
    state: Literal["OPEN", "MERGED", "CLOSED"]
    url: str


PrLookup = Callable[[str], PullRequest | None]


def gh_pull_request(branch: str) -> PullRequest | None:
    """The most recent pull request whose head is `branch`, via `gh`."""
    if shutil.which("gh") is None:
        return None
    try:
        done = subprocess.run(
            ["gh", "pr", "list", "--head", branch, "--state", "all", "--limit", "1",
             "--json", "state,url"],
            cwd=ROOT, capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT_S, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if done.returncode != 0:
        return None
    rows = json.loads(done.stdout or "[]")
    if not rows:
        return None
    return PullRequest(state=rows[0]["state"], url=rows[0]["url"])


def evaluate_phase(
    phase: Phase,
    root: Path,
    *,
    python: str = sys.executable,
    run_tests: bool = True,
    runner: Runner = run_command,
    pr_lookup: PrLookup | None = gh_pull_request,
) -> PhaseReport:
    missing = missing_contract(phase, root)
    failing: list[str] = []
    if not missing and run_tests:
        failing = failing_checks(phase, root, python, runner)
    pr = pr_lookup(phase.branch) if pr_lookup else None

    if not missing and not failing:
        state = State.landed
    elif pr is not None and pr.state == "MERGED":
        state = State.merged
    elif pr is not None and pr.state == "OPEN":
        state = State.review
    else:
        state = State.todo
    return PhaseReport(
        number=phase.number,
        title=phase.title,
        tier=phase.tier,
        branch=phase.branch,
        depends=list(phase.depends),
        state=state,
        missing=missing,
        failing=failing,
        pr_url=pr.url if pr else None,
    )


def run_gate(root: Path, python: str, runner: Runner = run_command) -> dict[str, bool]:
    results: dict[str, bool] = {}
    for command in GATE_COMMANDS:
        argv = _argv(command, python)
        code, _ = runner(argv, root)
        results[" ".join(command).replace("{python}", "python")] = code == 0
    return results


def evaluate(
    root: Path,
    *,
    target: Literal["review", "landed"] = "review",
    only: int | None = None,
    python: str = sys.executable,
    run_tests: bool = True,
    gate: bool = False,
    runner: Runner = run_command,
    pr_lookup: PrLookup | None = gh_pull_request,
) -> GoalReport:
    phases = [p for p in PHASES if only is None or p.number == only]
    reports = [
        evaluate_phase(
            p, root, python=python, run_tests=run_tests, runner=runner, pr_lookup=pr_lookup
        )
        for p in phases
    ]
    gate_results = run_gate(root, python, runner) if gate else None
    return GoalReport(
        target=target,
        met=goal_met(reports, target, gate_results),
        score=score(reports),
        landed=sum(r.state is State.landed for r in reports),
        phases=reports,
        next=next_step(reports),
        gate=gate_results,
    )


# --- CLI -------------------------------------------------------------------


def render_table(report: GoalReport) -> str:
    lines = [f"{'#':>2}  {'tier':<4}  {'state':<6}  phase"]
    for r in report.phases:
        detail = ""
        if r.state is State.todo and (r.missing or r.failing):
            detail = f"  (missing {len(r.missing)}, failing {len(r.failing)})"
        elif r.pr_url and r.state is not State.landed:
            detail = f"  {r.pr_url}"
        lines.append(f"{r.number:>2}  {r.tier:<4}  {r.state.value:<6}  {r.title}{detail}")
    if report.gate is not None:
        lines.append("")
        lines += [f"gate  {'pass' if ok else 'FAIL'}  {cmd}" for cmd, ok in report.gate.items()]
    nxt = report.next
    lines.append("")
    if nxt.kind == "build":
        lines.append(f"next: phase {nxt.phase} ({nxt.title}) on {nxt.branch} from {nxt.base}")
    else:
        lines.append(f"next: {nxt.kind} ({nxt.reason})")
    lines.append(
        f"goal ({report.target}): {'MET' if report.met else 'not met'}"
        f"  score {report.score:.0%}  landed {report.landed}/{len(report.phases)}"
    )
    return "\n".join(lines)


def exit_code(report: GoalReport) -> int:
    if report.met:
        return EXIT_MET
    if report.next.kind == "wait":
        return EXIT_WAITING
    return EXIT_NOT_MET


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--root", type=Path, default=ROOT, help="checkout to evaluate")
    parser.add_argument("--target", choices=("review", "landed"), default="review")
    parser.add_argument("--phase", type=int, help="evaluate one phase only")
    parser.add_argument("--gate", action="store_true", help="also run the four repo checks")
    parser.add_argument("--no-tests", action="store_true", help="check contracts only")
    parser.add_argument("--offline", action="store_true", help="skip pull-request lookups")
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    parser.add_argument("--next", action="store_true", help="print only the next step")
    parser.add_argument("--python", default=sys.executable, help="interpreter for checks")
    args = parser.parse_args(argv)

    report = evaluate(
        args.root.resolve(),
        target=args.target,
        only=args.phase,
        python=args.python,
        run_tests=not args.no_tests,
        gate=args.gate,
        pr_lookup=None if args.offline else gh_pull_request,
    )
    if args.next:
        print(report.next.model_dump_json(indent=2) if args.json else render_table(report).splitlines()[-2])
    elif args.json:
        print(report.model_dump_json(indent=2))
    else:
        print(render_table(report))
    return exit_code(report)


if __name__ == "__main__":
    sys.exit(main())
