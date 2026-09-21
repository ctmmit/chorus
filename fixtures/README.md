# Fixtures

The golden-path inputs. `path_test` runs against these every iteration. Per
ENGINEERING_REVIEW Q2, transcripts are **pre-transcribed** so tests never hit a
live third party.

## Public vs private

The real show transcripts are captions of commercial podcasts and are **not**
committed to this repo (`fixtures/transcripts/*.json` is gitignored). They live
in the private repo `ctmmit/chorus-private`. To work with them locally:

```bash
scripts/sync_private_fixtures.sh      # or scripts\sync_private_fixtures.ps1
```

Without them, tests that need real transcripts skip (see `tests/conftest.py`)
and the synthetic `sample_public.json` transcript, which is committed, still
exercises the whole spine.

## Contents (complete)

```
fixtures/
├── souls/
│   ├── soul_investor.md      # lens A — incl. Curation Guidance
│   └── soul_popculture.md    # lens B — incl. Curation Guidance (visibility contrast)
├── context.md                # principal-context blob (AI/finance focus)
├── episodes.json             # the episode pool, sourced from Readwise saves
└── transcripts/              # one pre-transcribed .json per episode
    ├── gs39QFYIbBY.json       # CLEAN  Ed Thorp (Tim Ferriss)
    ├── c4tvVKDhpiY.json       # CLEAN  Marc Andreessen (20VC)
    ├── wAnDWfEIwoE.json       # CLEAN  Josh Waitzkin (Huberman)
    ├── xKZ_8ULR91Y.json       # CLEAN  Jane Street (Dwarkesh)
    ├── 2Ryr95iiYNk.json       # CLEAN  Gavin Baker (Sohn)
    └── IAgmW_gTxls.json       # UNGROUNDED  scrambled eggs (off-topic)
# MISSING-TRANSCRIPT case: Goldman "The New AI Trades" (KhZfxZ-C-2g) has no
# captions on purpose — see episodes.json.missing_transcript.
```

Categories present: **5 clean**, **1 missing-transcript** (graceful skip),
**1 ungrounded** (honest refusal). Two souls give the visibility contrast.

## Mapping to path_test

`path_test` currently expects `clean_*` / `malformed_input` / `ungrounded_query`
file names (template placeholders). When `Invoke-Path`/`run_path` is wired to the
service in Goal 1-4, path_test is rewritten to consume this real fixture model
({soul, context, episodes.json, transcripts/}) instead of the placeholder names.

## The assignment — DONE (2026-06-18)

Ran the curation pass over the Marc Andreessen episode with `soul_investor` then
`soul_popculture`. Highlight sets had ~zero overlap; the same 58-61 min passage
yielded opposite highlights (investor: Schumpeterian value-capture economics;
pop-culture: "your girlfriend's a junior lawyer… you're gone"). Thesis validated.
Re-run on a real Readwise-derived soul once that bootstrap lands (Goal 5).
