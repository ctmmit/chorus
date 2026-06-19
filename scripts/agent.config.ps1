# agent.config.ps1 — Windows (PowerShell) adapter seam.
# The ONLY place a coding harness is named on the Windows runtime.
# loop.ps1 and supervisor.ps1 dot-source this file. PowerShell mirror of
# agent.env (which remains the bash/WSL seam). Switching harnesses is a
# one-line edit here; no contract file (AUTOPILOT/ROADMAP/STATE) names a harness.
#
# Builder contract any harness must satisfy:
#   - reads files in the project dir
#   - runs shell commands (build/test/path_test.ps1)
#   - invokable non-interactively with a prompt
#   - exits when the iteration is done

$IterationPrompt = 'You are the Chorus autonomous builder. Read docs/AUTONOMOUS_BUILD.md (the mission brief), then STATE.md, ROADMAP.md (current goal), AUTOPILOT.md, and the last 3 BUILD_LOG.md entries. Execute ONE iteration per that brief and the AUTOPILOT procedure, then exit.'

# Builder/Reviewer are scriptblocks so the harness call lives in exactly one place.
# --- Default: Claude Code builds, Codex reviews ---
$BuilderCmd  = { claude -p $IterationPrompt --permission-mode acceptEdits }
$ReviewerCmd = { codex exec $IterationPrompt }

# --- Alternate: Codex builds, Claude Code reviews ---
# $BuilderCmd  = { codex exec $IterationPrompt }
# $ReviewerCmd = { claude -p $IterationPrompt }

# --- Budget ---
# 'api'  -> dollar caps are the binding constraint (metered API)
# 'plan' -> subscription usage windows are the real budget; caps mostly inert
$BudgetMode    = 'plan'
$SessionCapUsd = 25
$DailyCapUsd   = 75
$WatchdogN     = 3

# --- Usage limits (the third terminal state: paused) ---
# Case-insensitive regex matched against builder output to classify a failure
# as a LIMIT (pause + backoff) rather than a crash.
$LimitPatterns = 'usage limit|rate limit|429|too many requests|try again (later|in)|quota|capacity|overloaded|limit (reached|exceeded)'
$LimitSleepS   = 1800     # 30 min between probes
$LimitMaxWaitS = 21600    # stop pausing after 6h; then write [blocked]

# Optional failover builder while the primary is limited. $null = off (default).
# $FallbackBuilderCmd = { codex exec $IterationPrompt }
$FallbackBuilderCmd = $null
