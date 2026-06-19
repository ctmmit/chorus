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

BUILD complete — ROADMAP Goals 1-6 done, path_test GREEN. Goal 7 (charts)
deferred post-July-11. Remaining: human SHIP gate (3 manual walks + cold-agent
dogfood + demo) and provisioning real API keys to swap mocks for live providers.

## Current task

Goal 6 Layer 2 COMPLETE: catalog.py (fixture-backed) + GET /shows +
POST /digest/select; 30 tests; path_test green through Goal 6. Autonomous build
target met. Status: done (awaiting human SHIP gate + real keys).

## Health

- Passing tests: 33 (last iteration: 30)
- `path_test.ps1`/.sh: GREEN through Goal 6 (6 assertions incl. divergence + selection)
- Last verified-green commit: goal 6 layer-2 (see git log)
- Build/typecheck/lint: ruff + mypy + pytest green; path_test green

## Open issues (IDs reference BUILD_LOG entries)

- [none] ISS-001 RESOLVED: real ElevenLabs single-voice audio (httpx) — full
  golden path verified live end-to-end (Haiku → Sonnet → 2.2MB mp3).

## Pending Tier-A questions for human

- [none] Building autonomously per user directive (2026-06-18). Kill criterion
  $150/40h accepted as default. Real paid-API calls (ANTHROPIC/ELEVENLABS/
  TRANSCRIPT keys) deferred behind mock clients until .env.local is provided.

## Budget today

- Spent: $0 of $75 daily cap. Sessions: 0.
