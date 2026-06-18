#!/usr/bin/env bash
# path_test.sh — the golden path as an executable assertion.
# This script IS the v1 definition of done and the scope test the agent
# cannot lawyer. Green or not.
#
# Fill in the run_path function for your stack. The assertion structure
# below is the durable part. ~50 lines of insurance; runs every iteration.

set -euo pipefail
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FIXTURES="$PROJECT_DIR/fixtures"
LATENCY_BOUND_S=30   # from IDEA_DOC §6

fail() { echo "PATH_TEST FAIL: $1" >&2; exit 1; }

run_path() {
  # Adapt: invoke your app end-to-end on a fixture input, print output.
  # e.g.: node src/cli.js --input "$1" --question "$(cat "$1.question")"
  fail "run_path not implemented yet"
}

# --- Assertion 1: golden path runs on all clean fixtures, in time,
# and every factual claim carries a citation marker.
for f in "$FIXTURES"/clean_*; do
  [[ -e "$f" ]] || fail "no clean fixtures in /fixtures (need 3)"
  start=$(date +%s)
  out=$(run_path "$f")
  elapsed=$(( $(date +%s) - start ))
  [[ "$elapsed" -le "$LATENCY_BOUND_S" ]] || fail "$f exceeded ${LATENCY_BOUND_S}s ($elapsed s)"
  # Citation discipline: adapt the marker to your output format.
  echo "$out" | grep -qE '\[(src|cite):[^]]+\]' || fail "$f: output contains no citation markers"
  # Optional stronger check: every paragraph contains >=1 marker.
done

# --- Assertion 2: malformed fixture fails gracefully (exit 0 with a
# clear error message, not a crash). Robustness is inside the anchor.
out=$(run_path "$FIXTURES/malformed_input" 2>&1) || fail "malformed input crashed the path"
echo "$out" | grep -qiE 'could not parse|invalid input|unable to process' \
  || fail "malformed input did not produce a clear user-facing error"

# --- Assertion 3: the deliberately-ungrounded query returns an explicit
# refusal, not plausible-sounding text. (Refusal-when-ungrounded as a
# regression test, not a policy.)
out=$(run_path "$FIXTURES/ungrounded_query")
echo "$out" | grep -qiE 'cannot answer|not supported by|no source|insufficient grounding' \
  || fail "ungrounded query produced an answer instead of a refusal"

echo "PATH_TEST GREEN"
