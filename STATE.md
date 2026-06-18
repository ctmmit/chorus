# STATE.md

> Mutable current-truth. The builder reads this FIRST every iteration and
> updates it LAST. Target: under 40 lines. History lives in git / BUILD_LOG.

## Phase (state-gated — transitions are conditions, never dates)

PLAN

- PLAN → BUILD when: ROADMAP exit criteria are all commands (done); the 5
  open questions in docs/DESIGN_DOC.md are resolved by /plan-eng-review;
  fixtures exist (3 clean, 1 malformed, 1 ungrounded — pre-transcribed);
  AUTOPILOT + agent.env approved.
- BUILD → SHIP when: path_test.sh green on all fixture sets.
- SHIP exit: cold-agent SKILL.md dogfood passes → demo recorded → pushed.
- Kill: spend exceeds IDEA_DOC §12 kill criterion → postmortem + archive.

## Current goal

Pre-BUILD. Goal 1 (Ingest) is next once eng review + fixtures land.

## Current task

Eng review complete (docs/ENGINEERING_REVIEW.md). Next: assemble fixtures
(3 clean, 1 malformed, 1 ungrounded; pre-transcribed) including the two-soul
visibility pair, and provision secrets. Then PLAN→BUILD. Status: not-started.

## Health

- Passing tests: 0 (no code yet)
- `path_test.sh`: not yet runnable (no fixtures / no implementation)
- Last verified-green commit: none
- Build/typecheck/lint: n/a

## Open issues (IDs reference BUILD_LOG entries)

- [none yet]

## Pending Tier-A questions for human

- Confirm kill criterion: $150 spend / 40h compute (proposed in eng review)
- Provision secrets in .env.local before BUILD: ANTHROPIC_API_KEY,
  ELEVENLABS_API_KEY, TRANSCRIPT_API_KEY
- (Resolved by eng review: audio engine, transcripts, latency, stack)

## Budget today

- Spent: $0 of $75 daily cap. Sessions: 0.
