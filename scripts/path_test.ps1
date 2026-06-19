#requires -Version 5.1
# path_test.ps1 — the golden path as an executable assertion. Delegates to the
# Python golden-path eval (scripts/golden_path.py), which drives the real API
# over the fixture model (POST /digest -> poll) and asserts latency, citation
# resolution, graceful skip, and refusal. Green or not. Runs every loop iteration.
#
# Usage:  powershell -File scripts\path_test.ps1 [-Through goalN]
# (-Through is accepted for ROADMAP parity; the eval runs the full golden path.)

[CmdletBinding()]
param([string]$Through = '')

$ProjectDir = Split-Path -Parent $PSScriptRoot
$venvPy = Join-Path $ProjectDir '.venv\Scripts\python.exe'
$py = if (Test-Path $venvPy) { $venvPy } else { 'python' }

& $py (Join-Path $PSScriptRoot 'golden_path.py')
exit $LASTEXITCODE
