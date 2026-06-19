---
description: Run the full Chorus autonomous build in one session (loop + body)
---

You are running the **full Chorus autonomous build in a single Claude Code
session**. You are BOTH the loop and the body: keep iterating until the done
condition is met. Do not stop after one iteration, and do not ask me for
permission between iterations.

Read `docs/AUTONOMOUS_BUILD.md` now for the complete mission brief — objective,
north star (reproduce the validated two-soul visibility divergence), locked
stack, per-iteration procedure, AUTOPILOT-tier guardrails, and done condition.
Follow it exactly. Then:

## 0. Preflight (once)
Check the Pending Tier-A list in `STATE.md` and whether `.env.local` exists.
- If keys are missing or the kill criterion is unconfirmed, ask me **once**
  (batch the questions). Then write `.env.local` from `.env.local.example`,
  confirm the kill criterion in IDEA_DOC §12, clear the Pending Tier-A list,
  and set `STATE.md` Phase to `BUILD`.
- If a key is genuinely unavailable, develop against `fixtures/transcripts/`
  with a mocked LLM/TTS client and log a `[decision]`; only stop if a real call
  is unavoidable for the current task.

## 1. Loop (repeat until done)
Execute one iteration of the per-iteration procedure from the brief: pick the
lowest-numbered incomplete task in the lowest-numbered incomplete goal, make the
smallest coherent change, run `ruff`/`mypy`/`pytest` and `scripts/path_test.ps1`,
commit (Conventional Commits), and update `STATE.md` + `BUILD_LOG.md`. Then
immediately begin the next iteration.

## 2. Self-governance (you are replacing loop.ps1's driver guarantees)
- **Watchdog:** if 3 consecutive iterations add no passing tests and complete no
  task, STOP and report — do not spin.
- **Tier-A:** on any must-ask item, STOP, write `[blocked] tier:A`, and ask me.
- **Tier-C:** if a task is still failing after 2 serious attempts, `[issue]` it
  and move to the next task.
- **Budget:** after each goal completes (and any time you sense you are
  approaching a sensible usage ceiling), pause and give me a one-screen status
  checkpoint, then continue.
- **Context:** `STATE.md` and `BUILD_LOG.md` are the source of truth — keep them
  current every iteration so an automatic compaction never loses the thread.

## 3. Done
When `scripts/path_test.ps1` is green through ROADMAP **Goal 6**, append a
`[review]` entry (what was built, how to run locally, path_test status per
fixture, what remains) and stop. Do NOT build Goal 7, do NOT `git push`, do NOT
deploy, and do NOT self-certify SHIP — the 3 manual golden-path walks + demo are
mine.

Begin with preflight now.
