#requires -Version 5.1
# sync_private_fixtures.ps1 — copy the real show transcripts from the private
# fixtures repo into fixtures/transcripts/ (gitignored here). Looks for a
# sibling checkout first, then clones via gh.
#
# Usage:  powershell -File scripts\sync_private_fixtures.ps1
$ErrorActionPreference = 'Stop'
$ProjectDir = Split-Path -Parent $PSScriptRoot
$Dest = Join-Path $ProjectDir 'fixtures\transcripts'
$Sibling = Join-Path (Split-Path -Parent $ProjectDir) 'chorus-private'

if (-not (Test-Path $Sibling)) {
  gh repo clone ctmmit/chorus-private $Sibling
}
Copy-Item (Join-Path $Sibling 'fixtures\transcripts\*.json') $Dest -Force
Write-Host "synced $(@(Get-ChildItem $Dest -Filter '*.json').Count) transcript(s) into fixtures/transcripts/"
