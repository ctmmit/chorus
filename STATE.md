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

Goal 3 — Voice. Exit: agent-authored opinionated script (claims traceable to
surfaced highlights); audio renders + downloadable. Spine floor: single-voice
monologue.

## Current task

Goal 2 curation COMPLETE (10 tests green incl. two-soul divergence eval +
ungrounded refusal; ruff + mypy clean). Next: 3.1 script synthesis (Sonnet,
traceable claims) behind LLM iface (mock now); 3.2 audio via
podcast-creator/ElevenLabs (mock); 3.3 async job + poll. Status: not-started.

## Health

- Passing tests: 10 (last iteration: 6)
- `path_test.ps1`: ingest+curation logic green via pytest; full path_test wired Goal 4
- Last verified-green commit: curation (see git log)
- Build/typecheck/lint: ruff + mypy + pytest green

## Open issues (IDs reference BUILD_LOG entries)

- [none yet]

## Pending Tier-A questions for human

- [none] Building autonomously per user directive (2026-06-18). Kill criterion
  $150/40h accepted as default. Real paid-API calls (ANTHROPIC/ELEVENLABS/
  TRANSCRIPT keys) deferred behind mock clients until .env.local is provided.

## Budget today

- Spent: $0 of $75 daily cap. Sessions: 0.
