"""Curation evals (chorus/evals.py, scripts/eval_curation.py): the metrics,
the regression check, and the runner failing on a broken scorer."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

from chorus.evals import (
    CaseScore,
    EvalCase,
    load_baseline,
    load_cases,
    regressions,
    score_case,
    summarize,
)
from chorus.llm import ScoredWindow, TokenUsage
from chorus.models import EpisodeDigest, Highlight

ROOT = Path(__file__).resolve().parent.parent


def _load_runner() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "eval_curation", ROOT / "scripts" / "eval_curation.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["eval_curation"] = module
    spec.loader.exec_module(module)
    return module


runner = _load_runner()


def _digest(*timestamps: float, refused: bool = False) -> EpisodeDigest:
    return EpisodeDigest(
        episode_id="e",
        episode_title=None,
        refused=refused,
        highlights=[
            Highlight(
                episode_id="e", episode_title=None, segment_timestamp=t, quote="q",
                relevance_score=0.5, why_surface="w",
            )
            for t in timestamps
        ],
    )


CASE = EvalCase(
    case_id="c", soul="s", transcript="t", must=[0, 200], ok=[400], never=[600]
)


# --- metrics -------------------------------------------------------------------


def test_score_case_counts_hits_inside_labelled_windows() -> None:
    # 10 and 45 are both inside the must window at 0; 410 is ok; 610 is never;
    # 1000 is unlabelled.
    score = score_case(CASE, _digest(10, 45, 410, 610, 1000))
    assert score.highlights == 5
    assert score.labeled == 4
    assert score.coverage == 0.8
    assert score.precision == 0.75
    assert score.must_recall == 0.5  # only the window at 0 was found
    assert score.never_hits == 1
    assert score.refusal_correct


def test_window_bounds_are_half_open() -> None:
    assert score_case(CASE, _digest(89.9)).must_recall == 0.5
    assert score_case(CASE, _digest(90.0)).labeled == 0


def test_refusal_cases() -> None:
    refuse = EvalCase(case_id="r", soul="s", transcript="t", expect_refusal=True)
    assert score_case(refuse, _digest(refused=True)).refusal_correct
    assert not score_case(refuse, _digest(5)).refusal_correct
    assert not score_case(CASE, _digest(refused=True)).refusal_correct
    empty = score_case(refuse, _digest(refused=True))
    assert (empty.precision, empty.must_recall, empty.coverage) == (None, None, None)


def test_summarize_skips_undefined_metrics() -> None:
    a = score_case(CASE, _digest(10, 610))
    b = score_case(EvalCase(case_id="r", soul="s", transcript="t", expect_refusal=True),
                   _digest(refused=True))
    summary = summarize([a, b])
    assert summary.cases == 2
    assert summary.mean_precision == 0.5
    assert summary.mean_must_recall == 0.5
    assert summary.never_hits == 1
    assert summary.refusal_accuracy == 1.0


# --- regressions ---------------------------------------------------------------


def _score(**kw: object) -> CaseScore:
    base: dict[str, object] = {
        "case_id": "c", "highlights": 4, "labeled": 4, "coverage": 1.0, "precision": 1.0,
        "must_recall": 1.0, "never_hits": 0, "refusal_correct": True,
    }
    base.update(kw)
    return CaseScore.model_validate(base)


def test_regressions_name_each_drop() -> None:
    baseline = [_score()]
    assert regressions([_score(precision=0.97)], baseline) == []  # inside tolerance
    found = regressions(
        [_score(precision=0.5, must_recall=0.5, never_hits=2, refusal_correct=False)], baseline
    )
    assert found == [
        "c: precision 1.0 -> 0.5",
        "c: must recall 1.0 -> 0.5",
        "c: never hits 0 -> 2",
        "c: refusal no longer matches the label",
    ]


def test_cases_missing_from_either_side_are_not_compared() -> None:
    assert regressions([_score(case_id="new", precision=0.0)], [_score()]) == []
    assert regressions([], [_score()]) == []


def test_precision_lost_to_no_labelled_highlights_is_a_regression() -> None:
    assert regressions([_score(precision=None)], [_score()]) == ["c: precision 1.0 -> None"]


# --- the committed cases and baseline ---------------------------------------------


def test_committed_cases_and_baseline_agree() -> None:
    cases = load_cases(ROOT / "fixtures" / "evals" / "cases.json")
    ids = [c.case_id for c in cases]
    assert len(ids) == len(set(ids))
    assert any(c.expect_refusal for c in cases)
    for case in cases:
        assert (ROOT / "fixtures" / case.soul).is_file()
        labels = case.must + case.ok + case.never
        assert len(labels) == len(set(labels)), case.case_id
    baseline = {s.case_id for s in load_baseline(ROOT / "fixtures" / "evals" / "baseline.json")}
    assert baseline == set(ids)


# --- the runner ---------------------------------------------------------------------


def test_runner_passes_against_the_committed_baseline(capsys: pytest.CaptureFixture[str]) -> None:
    assert runner.main([]) == 0
    assert "no regression" in capsys.readouterr().out


class _EverythingMatters:
    """A broken scorer: every window is maximally relevant."""

    def score_segment(self, text: str, soul: str, context: str) -> tuple[float, str]:
        return 1.0, "everything"

    def score_windows(
        self, windows: list[str], soul: str, context: str, meter: TokenUsage | None = None
    ) -> list[ScoredWindow]:
        return [(1.0, "everything") for _ in windows]


def test_runner_fails_on_a_broken_scorer(capsys: pytest.CaptureFixture[str]) -> None:
    assert runner.main([], client=_EverythingMatters()) == 1
    out = capsys.readouterr().out
    assert "REGRESSION" in out
    assert "sample-investor: never hits 0 -> 1" in out


def test_baseline_refuses_to_be_written_from_a_live_run(tmp_path: Path) -> None:
    target = tmp_path / "baseline.json"
    code = runner.main(["--live", "--update-baseline", "--baseline", str(target)],
                       client=_EverythingMatters())
    assert code == 1 and not target.exists()
