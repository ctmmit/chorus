#requires -Version 5.1
# path_test.ps1 — the golden path as an executable assertion (PowerShell port
# of path_test.sh). This script IS the v1 definition of done and the scope test
# the agent cannot lawyer. Green or not.
#
# Wire Invoke-Path to the Chorus async API once Goal 1-4 exist. The assertion
# structure below is the durable part. Runs every iteration via loop.ps1.
#
# Usage:  powershell -File scripts\path_test.ps1 [-Through goalN]

[CmdletBinding()]
param([string]$Through = '')
$ErrorActionPreference = 'Continue'

$ProjectDir   = Split-Path -Parent $PSScriptRoot
$Fixtures     = Join-Path $ProjectDir 'fixtures'
$LatencyBound = 90    # digest bound in seconds, per ENGINEERING_REVIEW Q3

function Fail([string]$msg) { Write-Host "PATH_TEST FAIL: $msg"; exit 1 }

function Invoke-Path([string]$fixture) {
  # ADAPT (Goal 1-4): POST the fixture's {soul, context, episodes} to the running
  # Chorus service, poll GET /digest/{job_id} until status=digest_ready, and return
  # the digest text/JSON. Until the service exists, this stub fails loudly.
  Fail "Invoke-Path not implemented yet (wire to POST /digest + poll loop)"
}

# --- Assertion 1: clean fixtures run within the latency bound, and every
# surfaced highlight carries a citation marker resolving to a transcript timestamp.
$clean = @(Get-ChildItem -Path $Fixtures -Filter 'clean_*' -ErrorAction SilentlyContinue)
if ($clean.Count -lt 1) { Fail "no clean fixtures in /fixtures (need 3)" }
foreach ($f in $clean) {
  $sw = [System.Diagnostics.Stopwatch]::StartNew()
  $out = Invoke-Path $f.FullName
  $sw.Stop()
  if ($sw.Elapsed.TotalSeconds -gt $LatencyBound) {
    Fail "$($f.Name) exceeded ${LatencyBound}s ($([int]$sw.Elapsed.TotalSeconds)s)"
  }
  if ($out -notmatch '\[(src|cite|ts):[^\]]+\]') {
    Fail "$($f.Name): output contains no citation markers"
  }
}

# --- Assertion 2: malformed / missing-transcript fixture degrades gracefully
# (a clear handled error, not a crash). Robustness is inside the anchor.
$mal = Join-Path $Fixtures 'malformed_input'
$out = ''
try { $out = Invoke-Path $mal } catch { Fail "malformed input crashed the path" }
if ($out -notmatch '(?i)could not parse|invalid input|unable to process|skipped') {
  Fail "malformed input did not produce a clear handled error"
}

# --- Assertion 3: the deliberately-ungrounded fixture returns an explicit
# refusal, not plausible text (refusal-when-ungrounded as a regression test).
$ung = Join-Path $Fixtures 'ungrounded_query'
$out = Invoke-Path $ung
if ($out -notmatch '(?i)nothing cleared|cannot answer|not supported by|no source|insufficient grounding') {
  Fail "ungrounded query produced an answer instead of a refusal"
}

Write-Host "PATH_TEST GREEN"
exit 0
