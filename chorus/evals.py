"""Curation evals: labelled cases, pure metrics, and regression checks.

The golden path asserts structure and grounding, never taste. This module is
where taste gets a number, so changes to scoring can be told apart from
regressions (docs/PLAN_0.3.md, phase 3).

A case is a soul, a context, a transcript, and labels on the transcript's
scoring windows, written by a careful reader holding that soul:

- `must`: windows that reader would surface.
- `ok`: windows it would be reasonable to surface.
- `never`: windows that would be a mistake to surface.
- `expect_refusal`: nothing in the episode clears the bar.

Labels are window start times in seconds. A highlight falls in a labelled
window when its timestamp is in `[start, start + WINDOW_SECONDS)`, which
holds whether the highlight cites the window's opening or a verbatim excerpt
from inside it. Labelling is deliberately partial: a long episode has many
windows nobody needs to rule on, so precision is measured over the
highlights that land in labelled windows, and `coverage` says what share
that was.

Everything here is pure. `scripts/eval_curation.py` runs the cases and
compares against the committed baseline.
"""
from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from chorus.curation import WINDOW_SECONDS
from chorus.models import EpisodeDigest

# How far a metric may fall below the baseline before the run fails. The
# mock scorer is deterministic, so any drop is a real change; live models
# vary run to run, hence a tolerance rather than exact equality.
PRECISION_TOLERANCE = 0.05
RECALL_TOLERANCE = 0.05
DEFAULT_K = 4


class EvalCase(BaseModel):
    case_id: str = Field(description="Stable identifier, used to compare against the baseline.")
    soul: str = Field(description="Soul file, relative to fixtures/.")
    context: str | None = Field(default=None, description="Context file, relative to fixtures/.")
    transcript: str = Field(description="Transcript video id under fixtures/transcripts/.")
    k: int = Field(default=DEFAULT_K, ge=1, description="Highlights requested (highlight_count).")
    must: list[float] = Field(default_factory=list)
    ok: list[float] = Field(default_factory=list)
    never: list[float] = Field(default_factory=list)
    expect_refusal: bool = False
    note: str = ""


class CaseScore(BaseModel):
    case_id: str
    highlights: int = Field(description="Highlights the run surfaced.")
    labeled: int = Field(description="Of those, how many landed in a labelled window.")
    coverage: float | None = Field(description="labeled / highlights; None when nothing surfaced.")
    precision: float | None = Field(
        description="Share of labelled highlights in must or ok windows; None when none labelled."
    )
    must_recall: float | None = Field(
        description="Share of must windows that got a highlight; None when the case has none."
    )
    never_hits: int = Field(description="Highlights in never windows.")
    refusal_correct: bool = Field(
        description="The episode was refused exactly when the case expects a refusal."
    )


class EvalSummary(BaseModel):
    cases: int
    mean_precision: float | None
    mean_must_recall: float | None
    never_hits: int
    refusal_accuracy: float | None


class EvalRun(BaseModel):
    rubric: str = Field(description="Which scorer produced the run (mock, or the live model).")
    scores: list[CaseScore]
    summary: EvalSummary
    skipped: list[str] = Field(default_factory=list, description="Cases whose transcript is absent.")


def _in_window(timestamp: float, start: float) -> bool:
    return start <= timestamp < start + WINDOW_SECONDS


def _window_of(timestamp: float, starts: list[float]) -> float | None:
    return next((s for s in starts if _in_window(timestamp, s)), None)


def score_case(case: EvalCase, digest: EpisodeDigest) -> CaseScore:
    """Score one curated episode against its labels."""
    timestamps = [h.segment_timestamp for h in digest.highlights]
    relevant = case.must + case.ok
    labeled = [t for t in timestamps if _window_of(t, relevant + case.never) is not None]
    hits = [t for t in labeled if _window_of(t, relevant) is not None]
    never_hits = sum(1 for t in timestamps if _window_of(t, case.never) is not None)
    found_must = {w for t in timestamps if (w := _window_of(t, case.must)) is not None}
    return CaseScore(
        case_id=case.case_id,
        highlights=len(timestamps),
        labeled=len(labeled),
        coverage=round(len(labeled) / len(timestamps), 3) if timestamps else None,
        precision=round(len(hits) / len(labeled), 3) if labeled else None,
        must_recall=round(len(found_must) / len(case.must), 3) if case.must else None,
        never_hits=never_hits,
        refusal_correct=digest.refused == case.expect_refusal,
    )


def _mean(values: list[float | None]) -> float | None:
    present = [v for v in values if v is not None]
    return round(sum(present) / len(present), 3) if present else None


def summarize(scores: list[CaseScore]) -> EvalSummary:
    return EvalSummary(
        cases=len(scores),
        mean_precision=_mean([s.precision for s in scores]),
        mean_must_recall=_mean([s.must_recall for s in scores]),
        never_hits=sum(s.never_hits for s in scores),
        refusal_accuracy=_mean([1.0 if s.refusal_correct else 0.0 for s in scores]),
    )


def regressions(
    current: list[CaseScore],
    baseline: list[CaseScore],
    precision_tolerance: float = PRECISION_TOLERANCE,
    recall_tolerance: float = RECALL_TOLERANCE,
) -> list[str]:
    """Each way a case got worse than its baseline, as a sentence. Cases
    missing from either side (a skipped private transcript, a new case) are
    not compared."""
    before = {s.case_id: s for s in baseline}
    problems: list[str] = []
    for now in current:
        was = before.get(now.case_id)
        if was is None:
            continue
        if (
            was.precision is not None
            and (now.precision or 0.0) < was.precision - precision_tolerance
        ):
            problems.append(f"{now.case_id}: precision {was.precision} -> {now.precision}")
        if (
            was.must_recall is not None
            and (now.must_recall or 0.0) < was.must_recall - recall_tolerance
        ):
            problems.append(f"{now.case_id}: must recall {was.must_recall} -> {now.must_recall}")
        if now.never_hits > was.never_hits:
            problems.append(f"{now.case_id}: never hits {was.never_hits} -> {now.never_hits}")
        if was.refusal_correct and not now.refusal_correct:
            problems.append(f"{now.case_id}: refusal no longer matches the label")
    return problems


def load_cases(path: Path) -> list[EvalCase]:
    """Cases from a JSON array file."""
    from pydantic import TypeAdapter

    return TypeAdapter(list[EvalCase]).validate_json(path.read_text(encoding="utf-8"))


def load_baseline(path: Path) -> list[CaseScore]:
    if not path.is_file():
        return []
    return EvalRun.model_validate_json(path.read_text(encoding="utf-8")).scores
