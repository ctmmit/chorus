"""The plan goal function (scripts/plan_goal.py): contract checks, phase
states, the next-phase choice and its base, and the goal condition."""
from __future__ import annotations

import importlib.util
import sys
from collections.abc import Sequence
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("plan_goal", ROOT / "scripts" / "plan_goal.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["plan_goal"] = module
    spec.loader.exec_module(module)
    return module


pg = _load()


def _report(number: int, state: str, depends: Sequence[int] = (), branch: str | None = None):  # type: ignore[no-untyped-def]
    return pg.PhaseReport(
        number=number,
        title=f"phase {number}",
        tier=1,
        branch=branch or f"feature/p{number}",
        depends=list(depends),
        state=pg.State(state),
    )


def _ok(argv: Sequence[str], cwd: Path) -> tuple[int, str]:
    return 0, ""


def _fail(argv: Sequence[str], cwd: Path) -> tuple[int, str]:
    return 1, "boom"


# --- the plan itself --------------------------------------------------------


def test_phases_are_numbered_in_order_and_depend_backwards() -> None:
    numbers = [p.number for p in pg.PHASES]
    assert numbers == list(range(1, len(pg.PHASES) + 1))
    for phase in pg.PHASES:
        assert all(d < phase.number for d in phase.depends)
        assert phase.branch.startswith(("feature/", "fix/", "refactor/"))
        assert phase.tests and all(t.startswith("tests/test_") for t in phase.tests)


def test_plan_document_names_every_contract() -> None:
    plan = (ROOT / "docs" / "PLAN_0.3.md").read_text(encoding="utf-8")
    for phase in pg.PHASES:
        assert phase.branch in plan
        for test in phase.tests:
            assert test in plan
        for symbol in phase.symbols:
            assert (symbol.name or symbol.path) in plan, (phase.number, symbol)


# --- contracts ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("source", "found"),
    [
        ("def answer(q):\n    pass\n", True),
        ("async def answer(q):\n    pass\n", True),
        ("class answer:\n    pass\n", True),
        ("answer = 1\n", True),
        ("answer: int = 1\n", True),
        ("    def answer(self):\n        pass\n", False),
        ("def answers():\n    pass\n", False),
        ("# answer = 1\n", False),
    ],
)
def test_defines_only_top_level_names(source: str, found: bool) -> None:
    assert pg.defines(source, "answer") is found


def test_missing_contract_lists_files_then_names(tmp_path: Path) -> None:
    phase = pg.Phase(
        8, "Ask", 2, "feature/ask", (),
        (pg.Symbol("chorus/ask.py", "answer"), pg.Symbol("skills/x/SKILL.md")),
        ("tests/test_ask.py",),
    )
    assert pg.missing_contract(phase, tmp_path) == [
        "tests/test_ask.py", "chorus/ask.py", "skills/x/SKILL.md",
    ]
    (tmp_path / "chorus").mkdir()
    (tmp_path / "chorus" / "ask.py").write_text("def other():\n    pass\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_ask.py").write_text("", encoding="utf-8")
    (tmp_path / "skills" / "x").mkdir(parents=True)
    (tmp_path / "skills" / "x" / "SKILL.md").write_text("", encoding="utf-8")
    assert pg.missing_contract(phase, tmp_path) == ["chorus/ask.py:answer"]


# --- phase states --------------------------------------------------------------


def _ask_checkout(root: Path) -> None:
    (root / "chorus").mkdir()
    (root / "chorus" / "ask.py").write_text("def answer():\n    pass\n", encoding="utf-8")
    (root / "tests").mkdir()
    (root / "tests" / "test_ask.py").write_text("", encoding="utf-8")


ASK = next(p for p in pg.PHASES if p.number == 8)


def test_present_and_passing_is_landed(tmp_path: Path) -> None:
    _ask_checkout(tmp_path)
    report = pg.evaluate_phase(ASK, tmp_path, runner=_ok, pr_lookup=None)
    assert report.state is pg.State.landed


def test_failing_tests_are_not_landed(tmp_path: Path) -> None:
    _ask_checkout(tmp_path)
    report = pg.evaluate_phase(ASK, tmp_path, runner=_fail, pr_lookup=None)
    assert report.state is pg.State.todo
    assert report.failing == ["pytest tests/test_ask.py"]


def test_pull_request_state_decides_when_root_lacks_the_phase(tmp_path: Path) -> None:
    def lookup(state: str):  # type: ignore[no-untyped-def]
        return lambda branch: pg.PullRequest(state=state, url=f"https://x/{branch}")

    review = pg.evaluate_phase(ASK, tmp_path, runner=_ok, pr_lookup=lookup("OPEN"))
    merged = pg.evaluate_phase(ASK, tmp_path, runner=_ok, pr_lookup=lookup("MERGED"))
    closed = pg.evaluate_phase(ASK, tmp_path, runner=_ok, pr_lookup=lookup("CLOSED"))
    assert (review.state, merged.state, closed.state) == (
        pg.State.review, pg.State.merged, pg.State.todo,
    )
    assert review.pr_url == "https://x/feature/ask"


def test_exit_commands_run_with_the_interpreter(tmp_path: Path) -> None:
    seen: list[list[str]] = []

    def record(argv: Sequence[str], cwd: Path) -> tuple[int, str]:
        seen.append(list(argv))
        return 0, ""

    phase = pg.Phase(3, "Evals", 2, "feature/e", (), (), ("tests/test_e.py",),
                     (("{python}", "scripts/eval_curation.py"),))
    assert pg.failing_checks(phase, tmp_path, "py", record) == []
    assert seen == [["py", "-m", "pytest", "-q", "tests/test_e.py"],
                    ["py", "scripts/eval_curation.py"]]


# --- next step -----------------------------------------------------------------


def test_next_is_lowest_buildable_from_main() -> None:
    reports = [_report(1, "landed"), _report(2, "todo", [1]), _report(3, "todo")]
    step = pg.next_step(reports)
    assert (step.kind, step.phase, step.base) == ("build", 2, "origin/main")


def test_next_stacks_on_a_dependency_in_review() -> None:
    reports = [_report(1, "review"), _report(2, "todo", [1])]
    step = pg.next_step(reports)
    assert (step.phase, step.base) == (2, "origin/feature/p1")
    assert "stacked on phase 1" in step.reason


def test_next_skips_a_phase_whose_dependency_is_unbuilt() -> None:
    reports = [_report(1, "todo"), _report(2, "todo", [1]), _report(3, "todo")]
    assert pg.next_step(reports).phase == 1
    reports = [_report(1, "review"), _report(2, "todo"), _report(3, "todo", [2]),
               _report(4, "todo")]
    assert pg.next_step(reports).phase == 2


def test_two_dependencies_in_review_on_different_branches_wait() -> None:
    reports = [_report(2, "review"), _report(4, "review"), _report(10, "todo", [2, 4])]
    step = pg.next_step(reports)
    assert step.kind == "wait"
    assert "feature/p2" in step.reason and "feature/p4" in step.reason


def test_merged_dependency_starts_from_main() -> None:
    reports = [_report(1, "merged"), _report(2, "todo", [1])]
    assert pg.next_step(reports).base == "origin/main"


def test_done_when_nothing_is_pending() -> None:
    assert pg.next_step([_report(1, "landed"), _report(2, "review")]).kind == "done"


# --- the goal ------------------------------------------------------------------


def test_review_target_counts_open_pull_requests() -> None:
    reports = [_report(1, "landed"), _report(2, "review")]
    assert pg.goal_met(reports, "review", None)
    assert not pg.goal_met(reports, "landed", None)
    assert pg.score(reports) == 1.0


def test_landed_target_requires_a_green_gate() -> None:
    reports = [_report(1, "landed")]
    assert pg.goal_met(reports, "landed", {"pytest": True})
    assert not pg.goal_met(reports, "landed", {"pytest": True, "mypy": False})


def test_exit_codes() -> None:
    met = pg.GoalReport(target="review", met=True, score=1, landed=1, phases=[],
                        next=pg.NextStep(kind="done", reason=""))
    wait = met.model_copy(update={"met": False, "next": pg.NextStep(kind="wait", reason="")})
    build = met.model_copy(update={"met": False, "next": pg.NextStep(kind="build", reason="")})
    assert [pg.exit_code(r) for r in (met, wait, build)] == [0, 2, 1]


def test_evaluate_offline_without_tests_on_this_checkout() -> None:
    report = pg.evaluate(ROOT, run_tests=False, pr_lookup=None)
    assert len(report.phases) == len(pg.PHASES)
    assert 0.0 <= report.score <= 1.0
    assert report.next.kind in ("build", "wait", "done")
