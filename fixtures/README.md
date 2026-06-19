# Fixtures

The golden-path inputs, checked in and real. `path_test.sh` runs against
these every iteration. Per ENGINEERING_REVIEW Q2, transcripts are
**pre-transcribed** here so tests never hit a live third party.

## Taxonomy (5 sets across 3 categories)

```
fixtures/
├── souls/
│   ├── soul_investor.md      # lens A (drafted)
│   └── soul_popculture.md    # lens B (drafted) — the visibility contrast
├── context.md                # principal-context blob (drafted)
├── episodes.json             # [TODO] 5-6 real episodes as YouTube URLs
├── transcripts/              # [TODO] one pre-transcribed .json per episode
│                             #        (segments with timestamps for citation)
├── clean_1 .. clean_3        # [TODO] 3 clean sets {soul, context, episodes}
├── malformed_input           # [TODO] a removed/private video → graceful skip
└── ungrounded_query          # [TODO] an episode irrelevant to context →
                              #        "nothing cleared the relevance bar"
```

## What's still needed (your input)

1. **episodes.json** — pick one real week of 5-6 shows you actually follow,
   as YouTube URLs. This is the only input only you have.
2. **transcripts/** — pre-transcribe each (the chosen managed API, or any
   one-off method now, since these are checked in once).
3. **malformed** — one removed/private video URL to prove graceful skip.
4. **ungrounded** — one episode with no bearing on context.md to prove the
   honest refusal.

## The assignment (do this before any code)

Run the curation prompt over the SAME episodes with `soul_investor.md` then
`soul_popculture.md`. If the surfaced highlights are obviously different, the
core thesis is real and this is your demo's best moment. If they're not, fix
the curation before building the service around it.
