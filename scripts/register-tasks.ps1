#requires -Version 5.1
# register-tasks.ps1 — register the Chorus supervisor as a Windows Scheduled
# Task (the cron replacement for supervisor.sh's "0 */3 * * *").
#
# Run once:  powershell -NoProfile -ExecutionPolicy Bypass -File scripts\register-tasks.ps1
# Elevated (admin) if you want it to run when you are not logged in.

$ErrorActionPreference = 'Stop'
$Supervisor = Join-Path $PSScriptRoot 'supervisor.ps1'
$TaskName   = 'Chorus-Supervisor'

$action = New-ScheduledTaskAction -Execute 'powershell.exe' `
  -Argument ("-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"{0}`"" -f $Supervisor)

# Every 3 hours, effectively indefinitely (10 years).
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) `
  -RepetitionInterval (New-TimeSpan -Hours 3) `
  -RepetitionDuration (New-TimeSpan -Days 3650)

$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
  -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
  -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
  -Settings $settings -Force `
  -Description 'Chorus vibe-code-framework supervisor: monitors loop.ps1, restarts or escalates.' | Out-Null

Write-Host "Registered scheduled task '$TaskName' (runs supervisor.ps1 every 3h)."
Write-Host "Inspect:  Get-ScheduledTask -TaskName '$TaskName'"
Write-Host "Run now:  Start-ScheduledTask -TaskName '$TaskName'"
Write-Host "Remove:   Unregister-ScheduledTask -TaskName '$TaskName' -Confirm:`$false"
