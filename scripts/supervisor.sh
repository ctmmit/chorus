#!/usr/bin/env bash
# supervisor.sh — dumb scheduled monitor. Any scheduler — cron, launchd, systemd timer. Cron example:
#   0 */3 * * * /path/to/project/scripts/supervisor.sh
#
# Does NO building. Reads STATE.md, BUILD_LOG tail, git log; applies
# simple rules; restarts or escalates. All judgment was front-loaded into
# AUTOPILOT + ROADMAP exit criteria — this script needs none.
#
# Escalation design: the ONLY things that reach your phone are Tier-A
# items and watchdog trips. Everything else self-heals or self-defaults
# with a logged decision you audit in the morning. That's the difference
# between an autonomous agent and a babysat one.

set -euo pipefail
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$PROJECT_DIR/agent.env" 2>/dev/null || true
STALL_HOURS=4

notify() {
  # Adapt: ntfy.sh, Pushover, Slack webhook, macOS osascript, etc.
  echo "[supervisor NOTIFY] $1"
  # curl -s -d "$1" ntfy.sh/your-topic >/dev/null
}

last_tag() {
  grep -oP '^\#\# [0-9T:\-]+ \[\K[a-z]+' "$PROJECT_DIR/BUILD_LOG.md" | tail -1
}

last_tier() {
  # tier line of the most recent blocked entry
  tac "$PROJECT_DIR/BUILD_LOG.md" | grep -m1 -oP '^tier: \K\w+' || echo "unknown"
}

hours_since_commit() {
  local ts; ts=$(git -C "$PROJECT_DIR" log -1 --format=%ct 2>/dev/null) || { echo 999; return; }
  echo $(( ( $(date +%s) - ts ) / 3600 ))
}

loop_running() {
  pgrep -f "loop.sh" >/dev/null
}

# Paused-for-limit is not a stall: no restart-spam, no notification.
if [[ -f "$PROJECT_DIR/.paused_until" ]]; then
  if [[ "$(cat "$PROJECT_DIR/.paused_until")" -gt "$(date +%s)" ]]; then
    echo "[supervisor] run paused for usage limit; standing down."
    exit 0
  fi
  rm -f "$PROJECT_DIR/.paused_until"   # stale marker; fall through
fi

tag=$(last_tag)

case "$tag" in
  blocked)
    case "$(last_tier)" in
      A)
        notify "Tier-A block: human decision required. See STATE.md pending questions."
        ;;
      watchdog)
        notify "Watchdog trip: loop halted on no-progress. Morning audit needed before restart."
        ;;
      B)
        # Tier-B should have self-resolved per AUTOPILOT; a B-tagged block
        # means the builder forgot its own protocol. Nudge and restart.
        echo "[supervisor] Tier-B block found; restarting loop with conservative-default nudge."
        loop_running || nohup "$PROJECT_DIR/scripts/loop.sh" >/dev/null 2>&1 &
        ;;
      C|*)
        # Task-level block; loop should have moved on. Restart if dead.
        loop_running || nohup "$PROJECT_DIR/scripts/loop.sh" >/dev/null 2>&1 &
        ;;
    esac
    ;;
  paused)
    # The loop's backoff owns this. Supervisor intervenes only if the
    # loop died mid-pause (no marker, no process, stale commits).
    if ! loop_running && [[ "$(hours_since_commit)" -ge "$STALL_HOURS" ]]; then
      echo "[supervisor] paused entry but loop dead; restarting (re-probes limit)."
      nohup "$PROJECT_DIR/scripts/loop.sh" >/dev/null 2>&1 &
    fi
    ;;
  *)
    # No block. Is the system silently stalled?
    if ! loop_running && [[ "$(hours_since_commit)" -ge "$STALL_HOURS" ]]; then
      echo "[supervisor] No commits in ${STALL_HOURS}h, no block, loop dead. Restarting."
      nohup "$PROJECT_DIR/scripts/loop.sh" >/dev/null 2>&1 &
    fi
    ;;
esac

# Budget sanity: spend consumed without task progress is a page-worthy
# anomaly even if nothing is formally blocked.
spend=$(cat "$PROJECT_DIR/.spend_today" 2>/dev/null || echo 0)
tasks=$(grep -c '^\- \[x\]' "$PROJECT_DIR/ROADMAP.md" 2>/dev/null || echo 0)
state_tasks=$(grep -oP 'tasks_done_at_last_check: \K[0-9]+' "$PROJECT_DIR/.supervisor_memo" 2>/dev/null || echo "$tasks")
if (( $(echo "$spend > 40" | bc -l) )) && [[ "$tasks" -le "$state_tasks" ]]; then
  notify "Budget anomaly: \$$spend spent today with no task progress since last check."
fi
echo "tasks_done_at_last_check: $tasks" > "$PROJECT_DIR/.supervisor_memo"
