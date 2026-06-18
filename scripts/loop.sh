#!/usr/bin/env bash
# loop.sh — the external loop driver. The agent is the loop body.
# Owns: iteration, budget enforcement, progress watchdog, restarts.
# The agent inside never controls whether it runs again.
#
# Usage: ./scripts/loop.sh [max_iterations]
# Harness-neutral: the builder command lives in agent.env, never here.

set -euo pipefail

MAX_ITER="${1:-20}"
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SPEND_FILE="$PROJECT_DIR/.spend_today"   # reset by cron at midnight

# Source the adapter seam: BUILDER_CMD, ITERATION_PROMPT, caps.
source "$PROJECT_DIR/agent.env"
: "${BUILDER_CMD:?agent.env must define BUILDER_CMD}"
: "${DAILY_CAP_USD:=75}" "${WATCHDOG_N:=3}"
: "${LIMIT_SLEEP_S:=1800}" "${LIMIT_MAX_WAIT_S:=21600}"
: "${LIMIT_PATTERNS:=usage limit|rate limit|429}" "${FALLBACK_BUILDER_CMD:=}"
PAUSE_FILE="$PROJECT_DIR/.paused_until"

# One iteration = one fresh-context builder session executing AUTOPILOT's
# iteration procedure. Fresh context per iteration is a quality feature:
# no context rot, no sunk-cost reasoning accumulating across hours.

passing_count() {
  # Adapt to your stack. Must print an integer.
  grep -oP 'Passing tests: \K[0-9]+' "$PROJECT_DIR/STATE.md" 2>/dev/null || echo 0
}

tasks_done() {
  grep -c '^\- \[x\]' "$PROJECT_DIR/ROADMAP.md" 2>/dev/null || echo 0
}

spend_today() {
  cat "$SPEND_FILE" 2>/dev/null || echo 0
}

log_paused() {
  {
    echo ""
    echo "## $(date +%Y-%m-%dT%H:%M) [paused] ISS-000 Usage limit: $1"
    echo "Driver paused — limit signal detected, not a failure. Resuming via"
    echo "probe-and-backoff (sleep ${LIMIT_SLEEP_S}s, retry, max ${LIMIT_MAX_WAIT_S}s)."
    echo "No human action needed."
    echo ""
    echo "---"
  } >> "$PROJECT_DIR/BUILD_LOG.md"
}

log_blocked() {
  local reason="$1"
  cat >> "$PROJECT_DIR/BUILD_LOG.md" <<EOF

## $(date +%Y-%m-%dT%H:%M) [blocked] ISS-000 Driver halt: $reason
tier: watchdog
Halted by loop.sh. $reason
Passing tests: $(passing_count). Tasks done: $(tasks_done).
Proposed next action: human review of STATE.md and recent entries.

---
EOF
}

stale=0
prev_tests=$(passing_count)
prev_tasks=$(tasks_done)

for i in $(seq 1 "$MAX_ITER"); do
  # --- Budget gate (driver-enforced; agent's self-report is logged, not trusted)
  if (( $(echo "$(spend_today) >= $DAILY_CAP_USD" | bc -l) )); then
    log_blocked "daily budget cap (\$$DAILY_CAP_USD) reached"
    exit 1
  fi

  # --- Tier-A gate: pending human questions stall the run loudly
  if grep -qE '^\- [^[(n]' <(sed -n '/Pending Tier-A/,/^##/p' "$PROJECT_DIR/STATE.md" | grep '^-' | grep -v 'none') 2>/dev/null; then
    log_blocked "pending Tier-A question in STATE.md requires human"
    exit 1
  fi

  echo "[loop] iteration $i/$MAX_ITER  tests=$prev_tests tasks=$prev_tasks spend=\$$(spend_today)"

  # Run builder, capture output for limit classification.
  out_file=$(mktemp)
  if ! ( cd "$PROJECT_DIR" && eval "$BUILDER_CMD" ) >"$out_file" 2>&1; then
    if grep -qiE "$LIMIT_PATTERNS" "$out_file"; then
      # --- PAUSED: third terminal state. Not a crash, not a block.
      # Watchdog is suspended; time fixes this, not judgment.
      if [[ -n "$FALLBACK_BUILDER_CMD" ]]; then
        echo "[loop] limit on primary; failing over to FALLBACK_BUILDER_CMD"
        ( cd "$PROJECT_DIR" && eval "$FALLBACK_BUILDER_CMD" ) || true
      else
        log_paused "primary builder limited"
        echo $(( $(date +%s) + LIMIT_SLEEP_S )) > "$PAUSE_FILE"
        waited=0
        while (( waited < LIMIT_MAX_WAIT_S )); do
          sleep "$LIMIT_SLEEP_S"; waited=$(( waited + LIMIT_SLEEP_S ))
          probe=$(mktemp)
          if ( cd "$PROJECT_DIR" && eval "$BUILDER_CMD" ) >"$probe" 2>&1 \
             || ! grep -qiE "$LIMIT_PATTERNS" "$probe"; then
            rm -f "$PAUSE_FILE" "$probe"; break   # window reopened (or non-limit error; next cycle handles)
          fi
          rm -f "$probe"
          echo $(( $(date +%s) + LIMIT_SLEEP_S )) > "$PAUSE_FILE"
        done
        if (( waited >= LIMIT_MAX_WAIT_S )); then
          rm -f "$PAUSE_FILE"
          log_blocked "usage limit persisted past ${LIMIT_MAX_WAIT_S}s — check plan/account status"
          exit 1
        fi
        stale=$(( stale > 0 ? stale - 1 : 0 ))  # pause cycles never count toward watchdog
      fi
    else
      tail -5 "$out_file"
      echo "[loop] builder exited nonzero (non-limit); driver continues (crash != run death)"
    fi
  fi
  rm -f "$out_file"

  # --- Progress watchdog: motion without progress is the failure class
  # event-based stop conditions miss.
  cur_tests=$(passing_count); cur_tasks=$(tasks_done)
  if [[ "$cur_tests" -le "$prev_tests" && "$cur_tasks" -le "$prev_tasks" ]]; then
    stale=$((stale+1))
  else
    stale=0
  fi
  prev_tests=$cur_tests; prev_tasks=$cur_tasks

  if [[ "$stale" -ge "$WATCHDOG_N" ]]; then
    log_blocked "$WATCHDOG_N consecutive iterations with no test or task progress"
    exit 1
  fi

  # --- Done check
  if bash "$PROJECT_DIR/scripts/path_test.sh" >/dev/null 2>&1; then
    echo "[loop] path_test.sh fully green — v1 golden path complete."
    exit 0
  fi
done

echo "[loop] max iterations reached."
