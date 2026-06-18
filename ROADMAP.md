# ROADMAP.md

> Goals → ordered tasks. Goals are segments of the IDEA_DOC §6 golden path,
> as revised in docs/DESIGN_DOC.md. Every exit criterion is a COMMAND.
> Task selection is deterministic: lowest-numbered incomplete task in the
> lowest-numbered incomplete goal.
>
> Structure (from DESIGN_DOC.md): Goals 1-4 are the SPINE (v1 golden path,
> must go green first). Goals 5-6 are LAYERS, ordered most-thesis-first,
> begun only after the spine is green. Goal 7 is post-July-11 stretch.
> Latency bounds [N]s / [M]min and the audio engine are set by
> /plan-eng-review before BUILD opens.

---

## Goal 1 — Ingest: episodes load, transcripts resolve, graceful skip

**Exit criterion:** `scripts/path_test.sh --through goal1` green — the 5
fixture sets load; episodes resolve to transcripts (from pre-transcribed
fixtures, per DESIGN_DOC Open Q2); the malformed / missing-transcript
fixture skips with a structured logged reason and does not crash.

- [ ] 1.1 Define request schema `{soul, context, episodes[], highlight_count?}`
- [ ] 1.2 Transcript + metadata resolution from fixtures; graceful skip path
- [ ] 1.3 Ingest tests + malformed-fixture handling

## Goal 2 — Curation: soul-conditioned digest with resolving citations

**Exit criterion:** `scripts/path_test.sh --through goal2` green — for each
highlight `{episode, segment_timestamp, relevance_score, one_line_reason}`;
every citation resolves (timestamp ± window contains the quoted span, fuzzy
match); below-threshold episodes return explicit "nothing cleared the bar."

- [ ] 2.1 Per-segment scoring `f(segment, soul, context) → score + reason + citation`
- [ ] 2.2 Citation-resolution check + below-threshold refusal
- [ ] 2.3 Digest assembly + curation tests

## Goal 3 — Voice: agent-authored opinionated script → audio episode

**Exit criterion:** `scripts/path_test.sh --through goal3` green — the agent
authors a script with a point of view (claims traceable to surfaced
highlights); the audio job renders a downloadable file. Spine floor:
single-voice opinionated monologue. (Engine: podcast-creator + ElevenLabs
via esperanto, per ENGINEERING_REVIEW Q1.)

- [ ] 3.1 Script synthesis with point of view (traceable to highlights)
- [ ] 3.2 Audio generation call + downloadable artifact
- [ ] 3.3 Async job + `GET /digest/{job_id}` poll (`pending|done|failed`)

## Goal 4 — End-to-end + SKILL.md cold-agent success

**Exit criterion:** `scripts/path_test.sh` fully green on all 5 fixture
sets, end-to-end within the latency bounds (digest ≤ 90s, audio ≤ 5 min,
per ENGINEERING_REVIEW Q3), AND a second fresh agent completes the golden
path against the live endpoint using only SKILL.md, no human help.

- [ ] 4.1 `{digest, script, audio_url}` response object + hosting (Railway/Render/Fly)
- [ ] 4.2 SKILL.md — cold-agent contract (schema, auth, polling, errors)
- [ ] 4.3 Cold-agent dogfood pass + latency assertion

---
> SPINE COMPLETE at Goal 4 green. Layers begin only here.
---

## Goal 5 — Layer 1: Agent-interview curation (the thesis layer)

**Exit criterion:** two distinct souls over the same episode set produce
demonstrably different digests (asserted by a fixture comparison in
`path_test.sh`). Doubles as the Premise-2 visibility fixture.

- [ ] 5.1 Interview flow that builds/refines `soul.md`
- [ ] 5.2 Independent curation + soul-version provenance on output

## Goal 6 — Layer 2: Episode/show pick-and-choose

**Exit criterion:** `scripts/path_test.sh --through goal6` green — caller
selects specific shows/episodes; selection resolves and runs the spine.

- [ ] 6.1 Selection endpoint + resolution to episode set

## Goal 7 — Layer 3 (post-July-11 stretch): taste presets + charts

**Exit criterion:** a preset (top 10 / business / tech / pop culture)
resolves to a real episode set and runs the spine green. Deferred past the
finale; adds a podcast-charts data source (DESIGN_DOC Open Q4).

- [ ] 7.1 Charts data source + preset resolution

---

> Cross-model review runs at each goal boundary. Open `[issue]` IDs from
> review are cleared before the next goal's tasks begin.
