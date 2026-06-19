# STATE.md

> Mutable current-truth. The builder reads this FIRST every iteration and
> updates it LAST. Target: under 40 lines. History lives in git / BUILD_LOG.

## Phase (state-gated — transitions are conditions, never dates)

BUILD

- PLAN → BUILD when: ROADMAP exit criteria are all commands (done); the 5
  open questions in docs/DESIGN_DOC.md are resolved by /plan-eng-review;
  fixtures exist (3 clean, 1 malformed, 1 ungrounded — pre-transcribed);
  AUTOPILOT + agent.env approved.
- BUILD → SHIP when: path_test.sh green on all fixture sets.
- SHIP exit: cold-agent SKILL.md dogfood passes → demo recorded → pushed.
- Kill: spend exceeds IDEA_DOC §12 kill criterion → postmortem + archive.

## Current goal

Goal 5 — Layer 1: soul bootstrap + agent-as-curator. Exit: two distinct souls
over the same episode set produce demonstrably different digests (asserted in
path_test), and soul bootstrap recipes documented (source-agnostic ladder).

## Current task

Goal 4 COMPLETE: path_test GREEN — golden_path.py drives the real API
(latency ≤90s, citations resolve, missing skip, ungrounded refuse,
all-fail→failed). SKILL.md cold-agent contract written; Procfile + /artifacts
static serving. SPINE (Goals 1-4) done. Next: Goal 5 (Layer 1). Status:
not-started.

## Health

- Passing tests: 19 (last iteration: 19)
- `path_test.ps1`/.sh: GREEN — golden_path.py drives the API end-to-end
- Last verified-green commit: goal 4 e2e (see git log)
- Build/typecheck/lint: ruff + mypy + pytest green; path_test green

## Open issues (IDs reference BUILD_LOG entries)

- [none yet]

## Pending Tier-A questions for human

- [none] Building autonomously per user directive (2026-06-18). Kill criterion
  $150/40h accepted as default. Real paid-API calls (ANTHROPIC/ELEVENLABS/
  TRANSCRIPT keys) deferred behind mock clients until .env.local is provided.

## Budget today

- Spent: $0 of $75 daily cap. Sessions: 0.
