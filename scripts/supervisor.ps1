#requires -Version 5.1
# supervisor.ps1 — dumb scheduled monitor (PowerShell port of supervisor.sh).
# Run by Windows Task Scheduler (see register-tasks.ps1), the cron replacement.
#
# Does NO building. Reads STATE.md, BUILD_LOG tail, and git log; applies simple
# rules; restarts a dead loop or escalates. All judgment was front-loaded into
# AUTOPILOT + ROADMAP exit criteria. The ONLY things that reach you are Tier-A
# items and watchdog trips; everything else self-heals.

$ErrorActionPreference = 'Continue'
$ProjectDir = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot 'agent.config.ps1')

$StallHours  = 4
$BuildLog    = Join-Path $ProjectDir 'BUILD_LOG.md'
$PauseFile   = Join-Path $ProjectDir '.paused_until'
$SpendFile   = Join-Path $ProjectDir '.spend_today'
$RoadmapFile = Join-Path $ProjectDir 'ROADMAP.md'
$MemoFile    = Join-Path $ProjectDir '.supervisor_memo'
$LoopScript  = Join-Path $PSScriptRoot 'loop.ps1'

function Get-Epoch { [long]([DateTimeOffset]::UtcNow.ToUnixTimeSeconds()) }

function Send-Notify([string]$msg) {
  # Adapt: ntfy / Pushover / Slack / Windows toast. Only Tier-A + watchdog reach here.
  Write-Host "[supervisor NOTIFY] $msg"
  # Invoke-RestMethod -Uri 'https://ntfy.sh/your-topic' -Method Post -Body $msg
  # if (Get-Module -ListAvailable BurntToast) { New-BurntToastNotification -Text 'Chorus', $msg }
}

function Get-LastTag {
  if (-not (Test-Path $BuildLog)) { return '' }
  $m = Select-String -Path $BuildLog -Pattern '^##\s[0-9T:\-]+\s\[([a-z]+)\]' -AllMatches
  if ($m -and $m.Count -gt 0) { return $m[-1].Matches[0].Groups[1].Value }
  return ''
}

function Get-LastTier {
  if (-not (Test-Path $BuildLog)) { return 'unknown' }
  $lines = Get-Content $BuildLog
  for ($i = $lines.Count - 1; $i -ge 0; $i--) {
    if ($lines[$i] -match '^tier:\s*(\w+)') { return $Matches[1] }
  }
  return 'unknown'
}

function Get-HoursSinceCommit {
  try {
    $ts = (git -C $ProjectDir log -1 --format=%ct 2>$null)
    if (-not $ts) { return 999 }
    return [int](((Get-Epoch) - [long]$ts) / 3600)
  } catch { return 999 }
}

function Test-LoopRunning {
  $procs = Get-CimInstance Win32_Process -Filter "Name='powershell.exe' OR Name='pwsh.exe'" -ErrorAction SilentlyContinue
  foreach ($p in $procs) {
    if ($p.CommandLine -and $p.CommandLine -like '*loop.ps1*') { return $true }
  }
  return $false
}

function Start-Loop {
  Start-Process -FilePath 'powershell.exe' `
    -ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-WindowStyle','Hidden','-File',$LoopScript `
    -WindowStyle Hidden
}

# Paused-for-limit is not a stall: no restart-spam, no notification.
if (Test-Path $PauseFile) {
  $until = 0L
  [void][long]::TryParse(((Get-Content $PauseFile -Raw).Trim()), [ref]$until)
  if ($until -gt (Get-Epoch)) { Write-Host "[supervisor] run paused for usage limit; standing down."; exit 0 }
  Remove-Item $PauseFile -ErrorAction SilentlyContinue   # stale marker; fall through
}

switch (Get-LastTag) {
  'blocked' {
    switch (Get-LastTier) {
      'A'        { Send-Notify "Tier-A block: human decision required. See STATE.md pending questions." }
      'watchdog' { Send-Notify "Watchdog trip: loop halted on no-progress. Morning audit needed before restart." }
      'B'        { Write-Host "[supervisor] Tier-B block; restarting loop."; if (-not (Test-LoopRunning)) { Start-Loop } }
      default    { if (-not (Test-LoopRunning)) { Start-Loop } }
    }
  }
  'paused' {
    if (-not (Test-LoopRunning) -and (Get-HoursSinceCommit) -ge $StallHours) {
      Write-Host "[supervisor] paused entry but loop dead; restarting (re-probes limit)."; Start-Loop
    }
  }
  default {
    if (-not (Test-LoopRunning) -and (Get-HoursSinceCommit) -ge $StallHours) {
      Write-Host "[supervisor] No commits in ${StallHours}h, no block, loop dead. Restarting."; Start-Loop
    }
  }
}

# Budget sanity: spend without task progress is page-worthy even if not formally blocked.
$spend = 0.0
if (Test-Path $SpendFile) {
  $raw = (Get-Content $SpendFile -Raw)
  if ($raw) {
    $parts = $raw.Trim() -split '\s+'
    if ($parts.Count -ge 2) { [void][double]::TryParse($parts[1], [ref]$spend) }
    else { [void][double]::TryParse($parts[0], [ref]$spend) }
  }
}
$tasks = 0
if (Test-Path $RoadmapFile) { $tasks = (Select-String -Path $RoadmapFile -Pattern '^\- \[x\]' -AllMatches | Measure-Object).Count }
$stateTasks = $tasks
if (Test-Path $MemoFile) {
  $mm = Select-String -Path $MemoFile -Pattern 'tasks_done_at_last_check:\s*(\d+)' | Select-Object -First 1
  if ($mm) { $stateTasks = [int]$mm.Matches[0].Groups[1].Value }
}
if ($spend -gt 40 -and $tasks -le $stateTasks) {
  Send-Notify "Budget anomaly: `$$spend spent today with no task progress since last check."
}
"tasks_done_at_last_check: $tasks" | Set-Content -Path $MemoFile -Encoding utf8
