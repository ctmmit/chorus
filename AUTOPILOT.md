# AUTOPILOT.md

> The autonomy contract. Read by the builder on every iteration.
> The builder is the loop BODY. `scripts/loop.sh` is the loop — it owns
> iteration, budget enforcement, the progress watchdog, and restarts.
> Each iteration starts with fresh context: read STATE.md, do one unit of
> work, update STATE.md, append to BUILD_LOG.md, exit.

## Mission

Build the approved v1 golden path from ROADMAP.md with minimal human
interruption. The objective is one condition: `path_test.sh` green on
three fixture sets. Not a perfect startup, and not a calendar — phases
gate on state, never dates.

## Read on every iteration (hot)

- `STATE.md` — current goal, current task, open issues, last green commit
- `ROADMAP.md` — current goal's task list and exit criterion only
- This file
- Last 3 `BUILD_LOG.md` entries

Read once (cold): `IDEA_DOC.md`, `docs/` design artifacts.

## Ambiguity protocol (replaces "default: ask")

- **Tier A — must-ask list below.** Hard stop. `[blocked]` entry, exit.
- **Tier B — ambiguous but reversible.** Take the most conservative
  interpretation, log `[decision]` with an explicit revert path, continue.
  Overnight runs do not stall on reversible judgment calls.
- **Tier C — cannot proceed on this task.** `[blocked]` entry on the task,
  then attempt the NEXT task in ROADMAP order rather than halting the run.
  One blocked task must not zero out a night of compute.

## Allowed without asking

- Create/edit files within the approved architecture
- Install ordinary open-source packages from the approved stack
- Components, routes, handlers, migrations, tests, docs
- Refactors needed for the current task
- Local build, lint, typecheck, test, dev commands; run `path_test.sh`
- Git commits after coherent task completion
- Fix bugs surfaced by tests, `path_test.sh`, or QA passes
- Mock data when real data is unavailable (`[decision]`)
- Reasonable v1 tradeoffs (`[decision]`)
- Mark ROADMAP tasks complete when their verification command passes
- Update `STATE.md` (this is mandatory, not just allowed)

## Must ask (Tier A)

- Deleting >50 lines or any full file
- Changing the golden path locked in IDEA_DOC §6
- Adding paid services or APIs requiring new credentials
- Changing the approved tech stack
- Disabling tests, linting, type checks, or any `path_test.sh` assertion
- Introducing a major framework or dependency
- Storing secrets in files (always `.env.local`)
- Irreversible database changes
- Deploying publicly under my account
- Sending emails or contacting external parties

## Default decisions

- Simple over clever; mock data over blocking; local-first over
  production complexity; one working path segment over many partial features
- Boring, agent-buildable architecture; explicit files over hidden magic
- Conventional Commits (`feat|fix|refactor|build|ci|chore|docs|style|perf|test`)
- UI: finance/research/portfolio projects → apply the Fulcrum design
  system (deep navy #0F2340, claim-based chart titles, data-ink
  discipline; in Claude Code this is the `fulcrum-style` skill);
  otherwise follow the app's existing design system
- One builder per worktree. Before any edit: `git status` + `git diff`.
  Small commits so parallel worktrees don't collide.

## Iteration procedure

1. Read hot files. If STATE.md and reality disagree (e.g., tests STATE
   claims green are red), fixing STATE.md is the first task.
2. Pick task: lowest-numbered incomplete task in lowest-numbered
   incomplete goal in ROADMAP.md. If open `[issue]` entries exist with
   IDs listed in STATE.md, clear those before new feature work.
3. Implement smallest reasonable change.
4. Run checks: build, typecheck, lint, unit tests, `path_test.sh`.
5. Fix failures (two serious attempts max — then Tier C).
6. Commit.
7. Update STATE.md: current task status, passing-test count, open issues.
8. Append `[build]` to BUILD_LOG.md; `[decision]` / `[issue]` as needed;
   `[spend]` with session token/API usage.
9. Exit. The driver decides whether another iteration runs.

## Output requirements (enforced, not aspirational)

`path_test.sh` asserts on every iteration:
1. Every factual claim in AI output carries a citation marker resolving
   to a real source location.
2. The deliberately-ungrounded fixture query returns an explicit refusal,
   not plausible text.
3. End-to-end latency within the IDEA_DOC §6 bound.

The SHIP gate still includes manual QA — three real input walks before
any demo recording — but it is the last line of defense, not the first.

## Stop conditions (builder-side)

`[blocked]` and exit if: tests fail after two serious fix attempts
(Tier C — move to next task first; block the run only if no tasks remain
attemptable); a required secret is missing; a destructive action is
needed; implementation would violate ENGINEERING_REVIEW or the locked
golden path; proceeding would create major rework (Tier A ambiguity).

`[blocked]` entries must contain: current status, exact reason, proposed
next action, and a `tier:` field so the supervisor can classify.

## Budget (driver-enforced)

Per-session cap **$25**; per-day cap **$75**. The builder logs `[spend]`;
`loop.sh` enforces. The agent never throttles itself — self-reported
budget compliance is not compliance.

## Progress invariant (driver-enforced)

If 3 consecutive iterations produce no increase in passing-test count and
no ROADMAP task completion, the driver halts and writes `[blocked]
tier:watchdog`. Motion without progress is the failure mode event-based
stop conditions miss.

## Review trigger

At each goal boundary (not just milestones): review in a clean worktree
using the REVIEWER_CMD harness from `agent.env` — a different harness
than the builder. Same-model review inherits the same blind spots; if
only one harness is available, use a fresh-context session in a clean
worktree as the fallback. Findings →
`[issue]` entries with IDs, registered in STATE.md, cleared before the
next goal's feature work.

## Goal-complete output

Append `[review]` to BUILD_LOG.md: what was built, how to run locally,
`path_test.sh` status per fixture set, what remains broken, suggested
next iteration.
