#requires -Version 5.1
# loop.ps1 — the external loop driver (PowerShell port of loop.sh).
# The agent is the loop BODY; this script is the loop. It owns iteration,
# budget enforcement, the progress watchdog, usage-limit backoff, and the
# done-check. The agent inside never controls whether it runs again.
#
# Usage:  powershell -NoProfile -ExecutionPolicy Bypass -File scripts\loop.ps1 [maxIterations]
# Harness-neutral: the builder command lives in agent.config.ps1, never here.

[CmdletBinding()]
param([int]$MaxIter = 20)
$ErrorActionPreference = 'Continue'

$ProjectDir = Split-Path -Parent $PSScriptRoot          # scripts\.. = project root
. (Join-Path $PSScriptRoot 'agent.config.ps1')          # adapter seam

$SpendFile   = Join-Path $ProjectDir '.spend_today'
$PauseFile   = Join-Path $ProjectDir '.paused_until'
$StateFile   = Join-Path $ProjectDir 'STATE.md'
$RoadmapFile = Join-Path $ProjectDir 'ROADMAP.md'
$BuildLog    = Join-Path $ProjectDir 'BUILD_LOG.md'
$PathTest    = Join-Path $PSScriptRoot 'path_test.ps1'

function Get-Epoch { [long]([DateTimeOffset]::UtcNow.ToUnixTimeSeconds()) }

function Get-PassingCount {
  if (-not (Test-Path $StateFile)) { return 0 }
  $m = Select-String -Path $StateFile -Pattern 'Passing tests:\s*(\d+)' | Select-Object -First 1
  if ($m) { return [int]$m.Matches[0].Groups[1].Value }
  return 0
}

function Get-TasksDone {
  if (-not (Test-Path $RoadmapFile)) { return 0 }
  return (Select-String -Path $RoadmapFile -Pattern '^\- \[x\]' -AllMatches | Measure-Object).Count
}

function Get-SpendToday {
  # .spend_today format: "YYYY-MM-DD <amount>" (self-resets on date change),
  # or a bare number (assumed today). The builder logs [spend]; the driver enforces.
  if (-not (Test-Path $SpendFile)) { return [double]0 }
  $raw = (Get-Content $SpendFile -Raw)
  if (-not $raw) { return [double]0 }
  $raw = $raw.Trim()
  $parts = $raw -split '\s+'
  if ($parts.Count -ge 2) {
    if ($parts[0] -eq (Get-Date -Format 'yyyy-MM-dd')) { return [double]$parts[1] }
    return [double]0
  }
  $val = 0.0
  if ([double]::TryParse($raw, [ref]$val)) { return [double]$val }
  return [double]0
}

function Test-TierABlock {
  # True if the "Pending Tier-A" section of STATE.md has a real bullet
  # (anything that is not "none" and not a parenthetical/resolved note).
  if (-not (Test-Path $StateFile)) { return $false }
  $inSection = $false
  foreach ($line in (Get-Content $StateFile)) {
    if ($line -match '^##\s') {
      if ($inSection) { break }
      if ($line -match 'Pending Tier-A') { $inSection = $true }
      continue
    }
    if ($inSection -and $line -match '^\-\s') {
      if ($line -notmatch '(?i)none' -and $line -notmatch '^\-\s*\(') { return $true }
    }
  }
  return $false
}

function Write-PausedLog([string]$reason) {
  $ts = Get-Date -Format 'yyyy-MM-ddTHH:mm'
  $body = @"

## $ts [paused] ISS-000 Usage limit: $reason
Driver paused - limit signal detected, not a failure. Resuming via
probe-and-backoff (sleep ${LimitSleepS}s, retry, max ${LimitMaxWaitS}s).
No human action needed.

---
"@
  Add-Content -Path $BuildLog -Value $body -Encoding utf8
}

function Write-BlockedLog([string]$reason) {
  $ts = Get-Date -Format 'yyyy-MM-ddTHH:mm'
  $tests = Get-PassingCount; $tasks = Get-TasksDone
  $body = @"

## $ts [blocked] ISS-000 Driver halt: $reason
tier: watchdog
Halted by loop.ps1. $reason
Passing tests: $tests. Tasks done: $tasks.
Proposed next action: human review of STATE.md and recent entries.

---
"@
  Add-Content -Path $BuildLog -Value $body -Encoding utf8
}

function Invoke-Builder([scriptblock]$cmd) {
  # Returns @{ Out = <string>; Failed = <bool> }
  $out = ''; $failed = $false
  $global:LASTEXITCODE = 0
  try {
    Push-Location $ProjectDir
    $out = (& $cmd 2>&1 | Out-String)
    if ($LASTEXITCODE -ne 0) { $failed = $true }
  } catch {
    $failed = $true; $out = ($_ | Out-String)
  } finally { Pop-Location }
  return @{ Out = $out; Failed = $failed }
}

$stale = 0
$prevTests = Get-PassingCount
$prevTasks = Get-TasksDone

for ($i = 1; $i -le $MaxIter; $i++) {

  # --- Budget gate (driver-enforced; the agent's self-report is logged, not trusted)
  if ((Get-SpendToday) -ge $DailyCapUsd) {
    Write-BlockedLog "daily budget cap (`$$DailyCapUsd) reached"
    exit 1
  }

  # --- Tier-A gate: pending human questions stall the run loudly
  if (Test-TierABlock) {
    Write-BlockedLog "pending Tier-A question in STATE.md requires human"
    exit 1
  }

  Write-Host "[loop] iteration $i/$MaxIter  tests=$prevTests tasks=$prevTasks spend=`$$(Get-SpendToday)"

  # --- Run builder; classify limit vs crash
  $r = Invoke-Builder $BuilderCmd
  if ($r.Failed) {
    if ($r.Out -match $LimitPatterns) {
      # PAUSED: third terminal state. Watchdog suspended; time fixes this.
      if ($null -ne $FallbackBuilderCmd) {
        Write-Host "[loop] limit on primary; failing over to FallbackBuilderCmd"
        [void](Invoke-Builder $FallbackBuilderCmd)
      } else {
        Write-PausedLog "primary builder limited"
        ((Get-Epoch) + $LimitSleepS) | Set-Content -Path $PauseFile -Encoding ascii
        $waited = 0
        while ($waited -lt $LimitMaxWaitS) {
          Start-Sleep -Seconds $LimitSleepS
          $waited += $LimitSleepS
          $probe = Invoke-Builder $BuilderCmd
          if ($probe.Out -notmatch $LimitPatterns) {
            Remove-Item $PauseFile -ErrorAction SilentlyContinue
            break
          }
          ((Get-Epoch) + $LimitSleepS) | Set-Content -Path $PauseFile -Encoding ascii
        }
        if ($waited -ge $LimitMaxWaitS) {
          Remove-Item $PauseFile -ErrorAction SilentlyContinue
          Write-BlockedLog "usage limit persisted past ${LimitMaxWaitS}s - check plan/account status"
          exit 1
        }
        if ($stale -gt 0) { $stale -= 1 }   # pause cycles never count toward watchdog
      }
    } else {
      Write-Host "[loop] builder exited nonzero (non-limit); driver continues (crash != run death)"
      ($r.Out -split "`n" | Select-Object -Last 5) | ForEach-Object { Write-Host $_ }
    }
  }

  # --- Progress watchdog: motion without progress is the failure class
  # event-based stop conditions miss.
  $curTests = Get-PassingCount; $curTasks = Get-TasksDone
  if ($curTests -le $prevTests -and $curTasks -le $prevTasks) { $stale += 1 } else { $stale = 0 }
  $prevTests = $curTests; $prevTasks = $curTasks
  if ($stale -ge $WatchdogN) {
    Write-BlockedLog "$WatchdogN consecutive iterations with no test or task progress"
    exit 1
  }

  # --- Done check
  & $PathTest *> $null
  if ($LASTEXITCODE -eq 0) {
    Write-Host "[loop] path_test.ps1 fully green - v1 golden path complete."
    exit 0
  }
}

Write-Host "[loop] max iterations reached."
