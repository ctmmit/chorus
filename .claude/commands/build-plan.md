---
description: Build docs/PLAN_0.3.md phase by phase until the goal function is met
argument-hint: "[--one] [--phase N] [--target review|landed]"
---

You are running the **0.3 plan build** for Chorus. The goal function
`scripts/plan_goal.py` decides what is done and what is next; you are the
builder. Keep iterating until it reports the goal met, unless `$ARGUMENTS`
contains `--one` (build one phase, then stop). `--phase N` builds that phase
instead of the next one. `--target landed` is only meaningful after the
principal has merged everything; the default target is `review`.

Read once: `docs/PLAN_0.3.md` (the phase you build, plus §0 and §2),
`AGENTS.md` (repository map, conventions, the parallel-agent rules).

`$PY` is `C:/dev/chorus/.venv/Scripts/python.exe` unless the repository
says otherwise.

## Each iteration

1. **Ask the goal function.** From the main checkout, run
   `git fetch -q origin` and then
   `& $PY scripts/plan_goal.py --root <a checkout of origin/main> --next --json`.
   The main checkout stays on `main`; if it is behind `origin/main`, evaluate
   a clean worktree of `origin/main` instead of pulling under running dev
   servers.
   - `kind: done` → go to **Done**.
   - `kind: wait` (exit 2) → report which pull requests must merge and stop.
   - `kind: build` → build `phase` on `branch` from `base`.

2. **Make the worktree.**
   `git worktree add .claude/worktrees/<short-name> -b <branch> <base>`, and
   do all work there. Copy the private transcripts in
   (`fixtures/transcripts/*.json` from the main checkout, or
   `scripts/sync_private_fixtures`); the golden path fails without them.
   If the branch already exists on origin (a previous attempt), check it out
   instead and continue from its last commit.

3. **Build to the contract.** Implement the phase as the plan describes it.
   The names in §2's table are the contract: define them at module top
   level, under those exact names. Follow the repository conventions:
   a `Protocol` with SQLite and Postgres implementations for any store, pure
   functions for scoring and formatting, named constants, deterministic mocks
   so tests never call live providers, and MCP tools registered in the
   feature's own `register_*_tools` function. Update `CHANGELOG.md`
   (Unreleased), and update `SKILL.md` *and* `skills/chorus/SKILL.md`
   together (a test keeps them byte-identical) whenever the agent-facing
   contract changes.

4. **Prove it.** In the worktree:
   - `& $PY scripts/plan_goal.py --root . --phase N --offline` must report
     the phase `landed`.
   - The four repository checks must all pass: `pytest -q`,
     `ruff check chorus tests scripts`, `mypy chorus`,
     `scripts/golden_path.py`.
   If a check fails, fix the cause. Never weaken, skip or delete an existing
   test or golden-path assertion to get green. When the phase genuinely
   changes behavior an existing test pinned, change that assertion to the new
   intended behavior and say so in the pull request.

5. **Ship the phase.** Commit (imperative subject under 72 characters, with
   the attribution trailer the session specifies), `git push -u origin
   <branch>`, and open a pull request against `main`. The body says which
   phase it is, what changes for a listener or an agent, how it works, any
   existing test whose assertion changed and why, and that the four checks
   passed locally with private fixtures present. A stacked phase says which
   pull request it builds on.

6. **Checkpoint.** Rerun the goal function and give a short status: the
   phase shipped, its pull request link, the score, and what is next. Then
   continue with the next iteration without waiting for a reply (unless
   `--one`).

## Guardrails

- **Never** merge a pull request, push to `main`, deploy, send email, or add
  a paid service or new credential. Merging is the principal's call; the
  `review` target exists for this reason.
- **Must ask, then stop:** deleting more than 50 lines of existing code or
  any existing file; a new third-party dependency; changing the v1 golden
  path; anything the plan leaves genuinely ambiguous and hard to reverse.
- **Reversible ambiguity:** choose the conservative reading, note it under
  "Decisions" in the pull request, and continue.
- **Watchdog:** if two serious attempts at a phase still leave a check red,
  push the branch as a draft pull request titled `WIP: phase N ...` with the
  failure in the body, and move to the next buildable phase. If two phases
  in a row end this way, stop and report.
- **Shell gotcha (Windows Git Bash here):** heredocs in the Bash tool can
  turn backslash escapes into raw bytes. Write source files with the Write
  and Edit tools, not with heredoc-generated Python that contains
  backslashes.
- **Context:** the goal function and the pull requests are the record. After
  a compaction, rerun step 1; it reconstructs where the build stands.

## Done

When the goal function exits 0 for the target, print its table, list every
pull request in phase order with any that stack on another, and stop. For
`--target review`, end by telling the principal the order to merge them in
(lowest phase first; a stacked pull request retargets to `main` once its
base merges).
