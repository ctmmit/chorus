# Windows runtime (PowerShell)

The `.ps1` scripts are the Windows-native runtime for the vibe-code-framework
driver. The `.sh` scripts are kept for harness/OS neutrality (bash/WSL/Linux/Mac).
They are behavior-for-behavior ports.

| Bash (Linux/WSL) | PowerShell (Windows) | Role |
|---|---|---|
| `agent.env` | `agent.config.ps1` | adapter seam — the only place a harness is named |
| `loop.sh` | `loop.ps1` | external loop driver: budget, Tier-A gate, watchdog, limit-backoff, done-check |
| `path_test.sh` | `path_test.ps1` | golden-path executable assertions |
| `supervisor.sh` | `supervisor.ps1` | monitor: restart dead loop / escalate Tier-A + watchdog |
| cron | `register-tasks.ps1` (Task Scheduler) | schedules the supervisor |

## One-time setup

```powershell
# Allow local scripts for your user (or pass -ExecutionPolicy Bypass per run)
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned

# Register the supervisor (cron replacement, every 3h). Elevated if you want it
# to run while logged out.
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\register-tasks.ps1
```

## Run the build loop

```powershell
# Foreground, 20 iterations max (default)
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\loop.ps1

# A specific iteration cap
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\loop.ps1 50
```

The loop sources `agent.config.ps1` for the builder command (`claude -p ...` by
default). `claude` must be on PATH. The loop exits 0 when `path_test.ps1` is
green, and writes `[blocked]` / `[paused]` entries to `BUILD_LOG.md` otherwise.

## Notes / differences from the bash version

- **Budget self-reset:** `.spend_today` may be `YYYY-MM-DD <amount>`; the driver
  treats a stale date as 0, so no midnight cron reset is needed.
- **Limit backoff** uses `Start-Sleep`; the run is paused, not failed.
- **Tier-A gate** halts the loop while `STATE.md` has an unresolved Pending
  Tier-A bullet (clear those before an unattended run).
- **Notifications** are stubbed in `supervisor.ps1` (`Send-Notify`); wire ntfy /
  Pushover / Slack / BurntToast there.
- `path_test.ps1`'s `Invoke-Path` is a stub until the service exists (Goal 1-4),
  exactly like the bash original's `run_path`.
