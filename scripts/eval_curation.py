"""Run the curation evals and compare against the committed baseline.

Every case in fixtures/evals/cases.json is curated with the mock scorer by
default (deterministic, offline, what CI runs) or, with `--live`, with the
configured model. Each case is scored against its labels (chorus/evals.py),
the run is written to artifacts/evals/<rubric>.json, and the run fails if
any case fell below fixtures/evals/baseline.json by more than the named
tolerance. Cases whose transcript is absent (the private fixtures, in CI)
are skipped and listed, never counted as failures.

    python scripts/eval_curation.py                    # mock; exit 1 on regression
    python scripts/eval_curation.py --live             # the real scorer (billed)
    python scripts/eval_curation.py --update-baseline  # accept the current mock numbers

Before merging a change to scoring prompts, run `--live` on main and on the
branch and quote both tables in the pull request.
"""
from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

# Make `chorus` importable when run as a standalone script (not via pytest).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from chorus.curation import curate_episode  # noqa: E402
from chorus.evals import (  # noqa: E402
    CaseScore,
    EvalCase,
    EvalRun,
    load_baseline,
    load_cases,
    regressions,
    score_case,
    summarize,
)
from chorus.llm import LLMClient, MockLLMClient, get_llm_client  # noqa: E402
from chorus.models import EpisodeInput, ResolvedEpisode, Transcript  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "fixtures"
CASES = FIXTURES / "evals" / "cases.json"
BASELINE = FIXTURES / "evals" / "baseline.json"
OUT_DIR = ROOT / "artifacts" / "evals"
MOCK_RUBRIC = "mock"


def _read(relative: str | None) -> str:
    return (FIXTURES / relative).read_text(encoding="utf-8") if relative else ""


def run_case(case: EvalCase, client: LLMClient) -> CaseScore | None:
    """Curate the case's episode and score it, or None if its transcript is absent."""
    path = FIXTURES / "transcripts" / f"{case.transcript}.json"
    if not path.is_file():
        return None
    transcript = Transcript.model_validate_json(path.read_text(encoding="utf-8"))
    resolved = ResolvedEpisode(episode=EpisodeInput(video_id=case.transcript), transcript=transcript)
    digest = curate_episode(
        resolved, _read(case.soul), _read(case.context), client, max_highlights=case.k
    )
    return score_case(case, digest)


def run(cases: list[EvalCase], client: LLMClient, rubric: str) -> EvalRun:
    scores: list[CaseScore] = []
    skipped: list[str] = []
    for case in cases:
        score = run_case(case, client)
        if score is None:
            skipped.append(case.case_id)
        else:
            scores.append(score)
    return EvalRun(rubric=rubric, scores=scores, summary=summarize(scores), skipped=skipped)


def _fmt(value: float | None) -> str:
    return "    -" if value is None else f"{value:5.2f}"


def render(run_: EvalRun) -> str:
    lines = [f"{'case':<26} {'n':>2} {'cov':>5} {'prec':>5} {'must':>5} {'never':>5}  refusal"]
    for s in run_.scores:
        lines.append(
            f"{s.case_id:<26} {s.highlights:>2} {_fmt(s.coverage)} {_fmt(s.precision)} "
            f"{_fmt(s.must_recall)} {s.never_hits:>5}  {'ok' if s.refusal_correct else 'WRONG'}"
        )
    m = run_.summary
    lines.append(
        f"{'mean':<26} {'':>2} {'':>5} {_fmt(m.mean_precision)} {_fmt(m.mean_must_recall)} "
        f"{m.never_hits:>5}  {_fmt(m.refusal_accuracy)}"
    )
    if run_.skipped:
        lines.append(f"skipped (transcript absent): {', '.join(run_.skipped)}")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None, client: LLMClient | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--live", action="store_true", help="use the configured model (billed)")
    parser.add_argument("--update-baseline", action="store_true", help="write the mock baseline")
    parser.add_argument("--cases", type=Path, default=CASES)
    parser.add_argument("--baseline", type=Path, default=BASELINE)
    args = parser.parse_args(argv)

    if client is None:
        client = get_llm_client() if args.live else MockLLMClient()
    rubric = type(client).__name__ if args.live else MOCK_RUBRIC
    result = run(load_cases(args.cases), client, rubric)
    print(render(result))

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / f"{rubric}.json").write_text(result.model_dump_json(indent=2), encoding="utf-8")

    if args.update_baseline:
        if args.live:
            print("refusing to write a live run as the baseline; the baseline is the mock's")
            return 1
        if result.skipped:
            print("refusing to write a baseline with skipped cases; add the private fixtures")
            return 1
        args.baseline.write_text(result.model_dump_json(indent=2) + "\n", encoding="utf-8")
        print(f"baseline written to {args.baseline}")
        return 0

    problems = regressions(result.scores, load_baseline(args.baseline))
    if problems:
        print("\nREGRESSION against the baseline:")
        for problem in problems:
            print(f"  {problem}")
        return 1
    print("\nno regression against the baseline")
    return 0


if __name__ == "__main__":
    sys.exit(main())
