# Build Log

> Append-only forensics. Write-mostly: the builder appends every
> iteration; humans and the supervisor read the tail. Current truth lives
> in STATE.md — if you're reading this file to figure out what's true
> now, the system has failed.
>
> Headers are machine-parseable — the supervisor greps them. Format is
> exact:
>
> `## 2026-06-12T14:30 [build] ISS-000 Title here`
>
> `[tag]` ∈ build | decision | issue | resolved | blocked | paused | review | spend
> `[paused]` is driver-written only: usage-limit backoff in progress.
> Requires no human action; resolves itself or escalates to [blocked].
> Issue entries get sequential IDs (ISS-001, ...); other tags use ISS-000.
> Blocked entries must include a `tier: A|B|C|watchdog` line in the body.

---

## 2026-06-12T00:00 [build] ISS-000 Example entry

Body. Files touched. Reasoning if non-obvious. For [decision]: the
conservative interpretation taken and the revert path. For [blocked]:
status, exact reason, proposed next action, tier. For [spend]: session
start/end usage in dollars or tokens.

---
