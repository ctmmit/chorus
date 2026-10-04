#requires -Version 5.1
# plan_loop.ps1 - the external driver for docs/PLAN_0.3.md.
# scripts/plan_goal.py is the goal function; each iteration runs one fresh,
# non-interactive Claude Code session that builds exactly one phase
# (`/build-plan --one`), then asks the goal function again. Budget caps,
# usage-limit backoff and the no-progress watchdog come from
# agent.config.ps1, the same seam loop.ps1 uses.
#
# Usage:
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\plan_loop.ps1 [-MaxIter 10] [-Merge]
# -Merge lets each run merge its own phase's pull request once CI is green
# (target becomes landed); without it every phase stops at an open PR.
#
# Exit codes: 0 goal met, 1 halted (budget, watchdog, limit), 2 waiting on a merge.

[CmdletBinding()]
param(
  [int]$MaxIter = 12,
  [switch]$Merge
)
$Target = if ($Merge) { 'landed' } else { 'review' }
$ErrorActionPreference = 'Continue'

$ProjectDir = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot 'agent.config.ps1')   # $DailyCapUsd, $LimitPatterns, $LimitSleepS, $LimitMaxWaitS, $WatchdogN

$venvPy   = Join-Path $ProjectDir '.venv\Scripts\python.exe'
$Py       = if (Test-Path $venvPy) { $venvPy } else { 'python' }
$Goal     = Join-Path $PSScriptRoot 'plan_goal.py'
$EvalTree = Join-Path $ProjectDir '.claude\worktrees\plan-goal-main'
$SpendFile = Join-Path $ProjectDir '.spend_today'
$LogFile  = Join-Path $ProjectDir 'artifacts\plan_loop.log'

# What a headless phase build may do: edit files, run the toolchain, manage
# its own worktree and branch, push that branch, and open or read pull
# requests. Pushing main and force-pushing are always denied; merging is
# allowed only under -Merge.
$Allowed = @(
  'Read', 'Edit', 'Write', 'Glob', 'Grep',
  'Bash(git:*)', 'Bash(gh pr create:*)', 'Bash(gh pr list:*)', 'Bash(gh pr view:*)',
  'Bash(gh pr checks:*)', 'Bash(gh pr edit:*)', 'Bash(gh run view:*)',
  'Bash(python:*)', 'Bash(*python.exe:*)', 'Bash(cp:*)', 'Bash(ls:*)', 'Bash(mkdir:*)',
  'Bash(powershell:*)'
) + $(if ($Merge) { @('Bash(gh pr merge:*)') } else { @() }) -join ','
$Denied = @(
  'Bash(git push origin main:*)', 'Bash(git push -f:*)', 'Bash(gh pr merge --admin:*)',
  'Bash(git push --force:*)', 'Bash(rm -rf:*)'
) + $(if ($Merge) { @() } else { @('Bash(gh pr merge:*)') }) -join ','
$Prompt = if ($Merge) { "/build-plan --one --merge" } else { "/build-plan --one" }

function Write-Log([string]$msg) {
  $line = "$(Get-Date -Format 'yyyy-MM-ddTHH:mm:ss') $msg"
  Write-Host "[plan] $msg"
  New-Item -ItemType Directory -Force -Path (Split-Path $LogFile) | Out-Null
  Add-Content -Path $LogFile -Value $line -Encoding utf8
}

function Get-SpendToday {
  if (-not (Test-Path $SpendFile)) { return [double]0 }
  $parts = ((Get-Content $SpendFile -Raw) -as [string]).Trim() -split '\s+'
  if ($parts.Count -ge 2 -and $parts[0] -eq (Get-Date -Format 'yyyy-MM-dd')) { return [double]$parts[1] }
  return [double]0
}

function Sync-EvalTree {
  # A clean checkout of origin/main to evaluate, so the main checkout (where
  # dev servers run) is never switched or pulled under them.
  git -C $ProjectDir fetch -q origin
  if (-not (Test-Path $EvalTree)) {
    git -C $ProjectDir worktree add -q --detach $EvalTree origin/main | Out-Null
  } else {
    git -C $EvalTree checkout -q --detach origin/main
  }
  $private = Join-Path $ProjectDir 'fixtures\transcripts'
  Copy-Item (Join-Path $private '*.json') (Join-Path $EvalTree 'fixtures\transcripts') -ErrorAction SilentlyContinue
}

function Get-Goal {
  Sync-EvalTree
  $json = & $Py $Goal --root $EvalTree --target $Target --json 2>$null | Out-String
  $code = $LASTEXITCODE
  try { $report = $json | ConvertFrom-Json } catch { $report = $null }
  return @{ Code = $code; Report = $report }
}

function Get-Progress($report) {
  if ($null -eq $report) { return 0 }
  return [int](($report.phases | Where-Object { $_.state -ne 'todo' }) | Measure-Object).Count
}

$stale = 0
$goal = Get-Goal
$progress = Get-Progress $goal.Report

for ($i = 1; $i -le $MaxIter; $i++) {
  if ($goal.Code -eq 0) { Write-Log "goal met (target $Target)."; & $Py $Goal --root $EvalTree --target $Target; exit 0 }
  if ($goal.Code -eq 2) { Write-Log "waiting: $($goal.Report.next.reason)"; exit 2 }
  if ($null -eq $goal.Report) { Write-Log 'goal function produced no report; halting.'; exit 1 }
  if ((Get-SpendToday) -ge $DailyCapUsd) { Write-Log "daily budget cap (`$$DailyCapUsd) reached; halting."; exit 1 }

  $next = $goal.Report.next
  Write-Log "iteration $i/$MaxIter  score $($goal.Report.score)  building phase $($next.phase) ($($next.title)) on $($next.branch) from $($next.base)"

  Push-Location $ProjectDir
  $global:LASTEXITCODE = 0
  $out = (& claude -p $Prompt --allowedTools $Allowed --disallowedTools $Denied 2>&1 | Out-String)
  $failed = $LASTEXITCODE -ne 0
  Pop-Location
  Add-Content -Path $LogFile -Value $out -Encoding utf8

  if ($failed -and $out -match $LimitPatterns) {
    Write-Log "usage limit; backing off $LimitSleepS s (max $LimitMaxWaitS s)."
    $waited = 0
    while ($waited -lt $LimitMaxWaitS) {
      Start-Sleep -Seconds $LimitSleepS
      $waited += $LimitSleepS
      $probe = (& claude -p 'Reply with OK.' 2>&1 | Out-String)
      if ($probe -notmatch $LimitPatterns) { break }
    }
    if ($waited -ge $LimitMaxWaitS) { Write-Log 'usage limit persisted; halting.'; exit 1 }
    $i -= 1   # a limited iteration does not count
    continue
  } elseif ($failed) {
    Write-Log 'builder exited nonzero (not a limit); the goal function decides what happened.'
  }

  # Watchdog: an iteration must move some phase out of todo.
  $goal = Get-Goal
  $now = Get-Progress $goal.Report
  if ($now -le $progress) { $stale += 1 } else { $stale = 0 }
  $progress = $now
  if ($stale -ge $WatchdogN) { Write-Log "$WatchdogN iterations without a phase moving; halting."; exit 1 }
}

Write-Log 'max iterations reached.'
exit 1
