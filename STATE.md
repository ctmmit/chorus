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

Goal 4 — End-to-end + SKILL.md. Exit: golden path green via path_test
(digest ≤90s, citations resolve, missing skips, ungrounded refuses); SKILL.md
cold-agent contract; hosting config.

## Current task

Goal 3 voice COMPLETE (19 tests: script traceability, audio artifact, full
async lifecycle POST→poll→done, all-fail→failed, 404; ruff+mypy clean). Next:
4.1 hosting + response object; 4.2 SKILL.md; 4.3 wire path_test to the API via
scripts/golden_path.py + latency assertion. Status: not-started.

## Health

- Passing tests: 19 (last iteration: 10)
- `path_test.ps1`: assertions covered by pytest; wired to the API in Goal 4
- Last verified-green commit: goal 3 voice (see git log)
- Build/typecheck/lint: ruff + mypy + pytest green

## Open issues (IDs reference BUILD_LOG entries)

- [none yet]

## Pending Tier-A questions for human

- [none] Building autonomously per user directive (2026-06-18). Kill criterion
  $150/40h accepted as default. Real paid-API calls (ANTHROPIC/ELEVENLABS/
  TRANSCRIPT keys) deferred behind mock clients until .env.local is provided.

## Budget today

- Spent: $0 of $75 daily cap. Sessions: 0.
